"""The ``jira.sync-export`` runner (294, JSYNC-05).

Export copies one Core schema into an EMPTY Jira schema. It shares the push's
PASS-1 create machinery through the recorder/include seam but deliberately keeps
every destination id in run-time dicts:

* a mandatory live emptiness check runs before any write (D-07); a non-empty
  destination ends the run FAILED with zero POST/PUT requests;
* no plugin link, baseline or conflict row is written — only audit rows on the
  Target connection;
* per D-06 an isolated create failure is audited, its dependents are skipped and
  the run ends PARTIAL_SUCCESS; 401/403/transport faults end it FAILED; every
  attempted unit failing ends it FAILED (A8);
* per D-07 nothing is ever deleted or resumed — a later run is refused by the
  emptiness check.

Imports only ``indramesh.sdk``, third-party/stdlib and sibling plugin modules
(the AST gate in ``tests/test_plugin_discovery.py``).
"""

import json
from typing import Any

import httpx
from sqlalchemy import text

from indramesh.sdk import PluginTaskError

from .admission import source_run_lock
from .client import JiraPullApiError, JiraPullAuthError, JiraPullClient
from .conflicts import write_sync_audit
from .link_guard import normalize_url
from .links import LinkStore
from .outbound import assert_target_allowed
from .pull import _load_connection
from .push import (
    PushCounters,
    _skip_category,
    classify_write_failure,
    finish_outcome,
    pass1_create_missing,
    record_skip_audit,
)
from .value_merge import canonical_repr
from .wire import attribute_entry, jira_reference_values, jira_scalar_values

# --- SQL (plugin-owned reads + Core reads, never a plugin link write) --------

_TARGET_SQL = text(
    """
    SELECT id, target_url, connector_type, is_active, schema_id
    FROM targets WHERE id = CAST(:target_id AS uuid)
    """
)
_TARGET_CONNECTION_SQL = text(
    """
    SELECT id, base_url FROM plugin_jira_connections
    WHERE target_id = CAST(:target_id AS uuid)
    """
)
_TARGET_ACCOUNT_SQL = text(
    """
    SELECT 1 FROM service_accounts
    WHERE id = CAST(:account_id AS uuid)
      AND target_id = CAST(:target_id AS uuid)
      AND is_active = true
    LIMIT 1
    """
)
_TYPE_DIRECTION_SQL = text(
    """
    SELECT jira_type_id, sync_direction
    FROM plugin_jira_object_type_links
    WHERE object_type_id = CAST(:id AS uuid)
      AND source_id = CAST(:source_id AS uuid)
    """
)
_ATTRIBUTE_LINK_SQL = text(
    """
    SELECT jira_type_attribute_id FROM plugin_jira_attribute_links
    WHERE object_type_attribute_id = CAST(:id AS uuid)
      AND source_id = CAST(:source_id AS uuid)
    """
)
_ATTRIBUTE_OWNER_SQL = text(
    "SELECT object_type_id FROM object_type_attributes WHERE id = CAST(:id AS uuid)"
)
_OBJECT_LINK_SQL = text(
    """
    SELECT o.object_type_id, l.is_detached
    FROM objects o
    LEFT JOIN plugin_jira_object_links l
      ON l.object_id = o.id AND l.source_id = CAST(:source_id AS uuid)
    WHERE o.id = CAST(:id AS uuid)
    """
)
_OBJECT_VALUES_SQL = text(
    """
    SELECT v.id, v.object_type_attribute_id, at.data_type, ota.structure,
           v.value_simple, v.value_integer, v.value_float, v.value_boolean,
           v.value_datetime, v.value_struct
    FROM object_values v
    JOIN object_type_attributes ota ON ota.id = v.object_type_attribute_id
    JOIN attribute_types at ON at.id = ota.attribute_type_id
    WHERE v.object_id = CAST(:id AS uuid)
    ORDER BY v.id
    """
)
_REFERENCE_ATTRIBUTES_SQL = text(
    """
    SELECT ota.id, ota.name
    FROM object_type_attributes ota
    JOIN attribute_types at ON at.id = ota.attribute_type_id
    WHERE ota.object_type_id = CAST(:id AS uuid) AND at.data_type = 'reference'
    """
)
_OBJECT_REFS_SQL = text(
    """
    SELECT id, to_object_id FROM object_refs
    WHERE source_id = CAST(:source_id AS uuid)
      AND from_object_id = CAST(:object_id AS uuid)
      AND from_attribute_name = :attribute_name
    ORDER BY id
    """
)

_TYPED_COLUMNS = (
    "value_simple",
    "value_integer",
    "value_float",
    "value_boolean",
    "value_datetime",
    "value_struct",
)


class RuntimeRecorder:
    """A run-scoped link recorder: in-memory id maps, nothing persisted.

    ``lookup`` returns only ids created in THIS run, so PASS-1 never adopts a
    pre-existing destination entity. ``record_baseline`` is the push's
    create-time baseline hook and is deliberately a no-op: the export writes no
    baseline (293 JSYNC-05).
    """

    _KINDS = ("status", "reference_type", "object_type", "attribute", "object")

    def __init__(self) -> None:
        self._ids: dict[str, dict[str, str]] = {kind: {} for kind in self._KINDS}

    async def lookup(self, kind, core_id) -> str | None:
        if core_id is None:
            return None
        return self._ids[kind].get(str(core_id))

    async def record(self, kind, core_id, jira_id, **context) -> None:
        self._ids[kind][str(core_id)] = str(jira_id)

    async def record_baseline(self, object_value_id, canonical) -> None:  # noqa: ARG002
        return None

    def status_jira_ids(self) -> dict:
        return self.items("status")

    def items(self, kind) -> dict[str, str]:
        return dict(self._ids[kind])


def _make_transport():
    """HTTP transport for this run; ``None`` selects the real network.

    Module-level so a test can inject an ``httpx`` mock transport for the run.
    """
    return None


def _export_include(store, session, schema_id):
    """The export ``include(kind, core_id)`` predicate (D-10/A5).

    Unlike the push include, a ``pull`` direction and an unlinked native type are
    exported too — only an effective direction of ``none`` and a detached object
    are excluded (a full copy otherwise). With no Source-bound link store every
    element is in scope.
    """
    if store is None:

        async def _include_all(_kind, _core_id):
            return True

        return _include_all

    async def _type_jira_id(object_type_id):
        row = (
            await session.execute(
                _TYPE_DIRECTION_SQL,
                {"id": str(object_type_id), "source_id": str(store.source_id)},
            )
        ).mappings().first()
        return (
            str(row["jira_type_id"]) if row and row["jira_type_id"] else "__unlinked__"
        )

    async def include(kind, core_id):
        if kind == "object_type":
            direction, _origin = store.effective_type_direction(
                schema_id=schema_id,
                jira_type_id=await _type_jira_id(core_id),
            )
            return direction != "none"
        if kind == "attribute":
            owner = (
                await session.execute(_ATTRIBUTE_OWNER_SQL, {"id": str(core_id)})
            ).mappings().first()
            if owner is None:
                return False
            link = (
                await session.execute(
                    _ATTRIBUTE_LINK_SQL,
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).first()
            jira_attr_id = None if link is None else str(link[0])
            direction, _origin = store.effective_attribute_direction(
                schema_id=schema_id,
                jira_type_id=await _type_jira_id(owner["object_type_id"]),
                defining_object_type_id=owner["object_type_id"],
                jira_attribute_id=jira_attr_id or "__unlinked__",
            )
            return direction != "none"
        if kind == "object":
            row = (
                await session.execute(
                    _OBJECT_LINK_SQL,
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).mappings().first()
            if row is None or row["is_detached"]:
                return False
            direction, _origin = store.effective_type_direction(
                schema_id=schema_id,
                jira_type_id=await _type_jira_id(row["object_type_id"]),
            )
            return direction != "none"
        return False

    return include


async def _load_export_scope(session, *, source_id, schema_id, target_id):
    """Validate the Target/connection guard and return the run scope (T-294-26)."""
    target = (
        await session.execute(_TARGET_SQL, {"target_id": str(target_id)})
    ).mappings().first()
    if target is None:
        raise PluginTaskError("The export Target does not exist.")
    if target["connector_type"] != "jira":
        raise PluginTaskError("The export Target is not a Jira connector.")
    if not target["is_active"]:
        raise PluginTaskError("The export Target is inactive.")
    if target["schema_id"] is not None and str(target["schema_id"]) != str(schema_id):
        raise PluginTaskError("The Target is bound to a different Core schema.")

    target_connection = (
        await session.execute(
            _TARGET_CONNECTION_SQL, {"target_id": str(target_id)}
        )
    ).mappings().first()
    if target_connection is None:
        raise PluginTaskError(
            "No Target-bound Jira connection exists for this Target."
        )
    target_url = normalize_url(target["target_url"])
    if target_url is None or target_url != normalize_url(target_connection["base_url"]):
        raise PluginTaskError(
            "The Target URL changed after this Jira connection was bound."
        )

    source_connection = await _load_connection(session, source_id)
    return {
        "source_id": source_id,
        "schema_id": schema_id,
        "target_id": target_id,
        "object_type_id": None,
        "target_url": target["target_url"],
        "target_connection_id": target_connection["id"],
        "source_connection_id": (
            None if source_connection is None else source_connection["connection_id"]
        ),
    }


async def _build_store(session, source_id, scope):
    """The Source-bound LinkStore, or ``None`` for a native/unlinked source."""
    source_connection = await _load_connection(session, source_id)
    if source_connection is None:
        return None
    store = LinkStore(
        session,
        source_id=source_id,
        connection_id=source_connection["connection_id"],
        connection_revision=source_connection["connection_revision"],
        core_source_url=source_connection["core_source_url"],
        connection_url=source_connection["connection_base_url"],
    )
    await store.preload()
    return store


def _typed_column_value(value):
    for column in _TYPED_COLUMNS:
        if value[column] is not None:
            return column, value[column]
    return None, None


async def _object_put_entries(session, recorder, object_id, source_id):
    """Every attribute entry of one exported object: scalars + references.

    A Jira ``PUT /object/{id}`` replaces the whole attribute set, so the
    reference pass re-sends the object's scalar values alongside the resolved
    references.
    """
    entries: dict[int, dict] = {}
    order: list[int] = []

    for value in (
        await session.execute(_OBJECT_VALUES_SQL, {"id": str(object_id)})
    ).mappings():
        if value["data_type"] == "reference":
            continue
        jira_attr_id = await recorder.lookup(
            "attribute", value["object_type_attribute_id"]
        )
        if jira_attr_id is None:
            continue
        typed_column, raw = _typed_column_value(value)
        if typed_column is None:
            continue
        canonical = canonical_repr(typed_column, raw)
        attr_id = int(jira_attr_id)
        if attr_id not in entries:
            entries[attr_id] = {
                "data_type": value["data_type"],
                "structure": value["structure"],
                "reprs": [],
            }
            order.append(attr_id)
        entries[attr_id]["reprs"].append(canonical)

    result = [
        attribute_entry(
            attr_id,
            jira_scalar_values(
                entries[attr_id]["data_type"],
                entries[attr_id]["reprs"],
                structure=entries[attr_id]["structure"],
                status_jira_ids=recorder.items("status"),
            ),
        )
        for attr_id in order
    ]

    for attr in (
        await session.execute(
            _REFERENCE_ATTRIBUTES_SQL,
            {"id": str((await _object_type_of(session, object_id)))},
        )
    ).mappings():
        jira_attr_id = await recorder.lookup("attribute", attr["id"])
        if jira_attr_id is None:
            continue
        jira_object_ids = []
        for ref in (
            await session.execute(
                _OBJECT_REFS_SQL,
                {
                    "source_id": str(source_id),
                    "object_id": str(object_id),
                    "attribute_name": attr["name"],
                },
            )
        ).mappings():
            jira_target = await recorder.lookup("object", ref["to_object_id"])
            if jira_target is not None:
                jira_object_ids.append(jira_target)
        if jira_object_ids:
            result.append(
                attribute_entry(
                    int(jira_attr_id), jira_reference_values(jira_object_ids)
                )
            )
    return result


async def _object_type_of(session, object_id):
    row = (
        await session.execute(
            text("SELECT object_type_id FROM objects WHERE id = CAST(:id AS uuid)"),
            {"id": str(object_id)},
        )
    ).first()
    return None if row is None else row[0]


async def _set_references_pass(session, client, scope, recorder, counters):
    """One PUT per created object: resolve references from the run-time id map."""
    for core_id, jira_id in recorder.items("object").items():
        entries = await _object_put_entries(session, recorder, core_id, scope["source_id"])
        if not entries:
            continue
        object_type_id = await _object_type_of(session, core_id)
        type_jira = await recorder.lookup("object_type", object_type_id)
        if type_jira is None:
            continue
        payload = {"objectTypeId": int(type_jira), "attributes": entries}
        try:
            await client.update_object(str(jira_id), payload)
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            if classify_write_failure(exc) == "fatal":
                raise
            await record_skip_audit(
                session,
                connection_id=scope["target_connection_id"],
                direction="export",
                entity_type="object",
                entity_id=core_id,
                category=_skip_category(exc),
            )
            counters.skipped += 1
            continue
        counters.pushed += 1
        await write_sync_audit(
            session,
            connection_id=scope["target_connection_id"],
            direction="export",
            status="success",
            entity_type="object",
            entity_id=core_id,
            details="export reference pass",
        )
        await session.commit()


async def run_export(ctx, task_id, **kwargs: Any) -> dict:  # noqa: ARG001
    """Execute one ``jira.sync-export`` into the task's destination Jira schema."""
    task = ctx.task
    source_id = task.source_id
    schema_id = task.schema_id
    target_id = task.target_id
    jira_schema_id = kwargs.get("jira_schema_id")

    if source_id is None:
        raise PluginTaskError("A Jira sync export requires a Source scope.")
    if schema_id is None:
        raise PluginTaskError("A Jira sync export requires a selected schema.")
    if target_id is None:
        raise PluginTaskError("A Jira sync export requires a Target.")
    if jira_schema_id is None:
        raise PluginTaskError(
            "A Jira sync export requires a destination jira_schema_id."
        )

    async with ctx.db() as session:
        account_id = task.service_account_id
        if account_id is None:
            raise PluginTaskError(
                "A Jira sync export requires a bound service account."
            )
        account_row = (
            await session.execute(
                _TARGET_ACCOUNT_SQL,
                {"account_id": str(account_id), "target_id": str(target_id)},
            )
        ).first()
        if account_row is None:
            raise PluginTaskError(
                "The task's service account is not bound to this Target."
            )

        scope = await _load_export_scope(
            session, source_id=source_id, schema_id=schema_id, target_id=target_id
        )
        scope["jira_schema_id"] = str(jira_schema_id)

        async with source_run_lock(session, source_id) as acquired:
            if not acquired:
                raise PluginTaskError(
                    "Another Jira sync is already running for this Source."
                )

            credentials = await ctx.credentials()
            pat = credentials.target_bearer_token
            if not pat:
                raise PluginTaskError(
                    "The bound service account has no Jira token."
                )

            # SESS-07 (D-06): the Target URL about to receive the PAT is re-vetted
            # against a fresh answer on every export, reading the Target row's own
            # current URL and its flag. It runs before ``client`` exists, so a
            # refused export dials nothing and writes nothing.
            await assert_target_allowed(
                session, kind="target", endpoint_id=target_id
            )

            client = JiraPullClient(
                base_url=scope["target_url"],
                pat=pat,
                transport=_make_transport(),
            )
            counters = PushCounters()
            try:
                existing = await client.get_object_types(scope["jira_schema_id"])
                if existing:
                    raise PluginTaskError(
                        "The destination Jira schema is not empty; clean it "
                        "manually or choose another empty schema."
                    )

                store = await _build_store(session, source_id, scope)
                recorder = RuntimeRecorder()
                include = _export_include(store, session, schema_id)
                await pass1_create_missing(
                    ctx,
                    session,
                    client,
                    scope,
                    recorder=recorder,
                    include=include,
                    counters=counters,
                )
                await _set_references_pass(
                    session, client, scope, recorder, counters
                )
            except JiraPullAuthError as exc:
                raise PluginTaskError(str(exc)) from None
            except httpx.TransportError:
                raise PluginTaskError(
                    "The Jira connection failed while writing."
                ) from None
            except JiraPullApiError as exc:
                raise PluginTaskError(exc.message) from None
            finally:
                await client.aclose()

    result = await finish_outcome(ctx, counters, tasktype="jira.sync-export")
    result["failed"] = counters.skipped
    return result
