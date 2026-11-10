"""The ``jira.sync-push`` runner (294, JSYNC-04/14).

Push takes local changes of a Core schema that is linked to a Jira schema and
writes them back to Jira. It is deliberately conservative:

* the Source, its connection and the Target must all normalize to the SAME Jira
  instance (the Phase-172 class guard, T-294-14) — re-checked at run start
  because the URL can change between admission and execution;
* PASS-1 creates what the linked schema lacks (statuses, reference types, object
  types, attributes, objects), committing one link row per create so a crash can
  never lose a Jira id (T-294-15);
* PASS-2 sends each locally changed single-value scalar attribute in ONE PUT per
  object and stores the new baseline on the SOURCE link connection (never the
  Target connection — Pitfall 4);
* every decision routes through ``LinkStore.effective_*_direction`` (JSYNC-14):
  detached objects and pull/none directions are never written, and push never
  issues a DELETE (A9/D-10);
* 401/403 and connection-wide transport faults fail the run; an isolated type /
  attribute / object fault is audited on the Target connection, its dependents
  are skipped and the run ends PARTIAL_SUCCESS (D-06).

Imports only ``indramesh.sdk``, third-party/stdlib and sibling plugin modules
(the AST gate in ``tests/test_plugin_discovery.py``).
"""

import json
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from indramesh.sdk import CoreDataError, PluginTaskError, TaskStatus

from .admission import source_run_lock, sync_task_family  # noqa: F401
from .checkpoints import (
    load_attribute_baselines,
    upsert_attribute_baseline,
    upsert_value_baseline,
)
from .client import (
    JiraPullApiError,
    JiraPullAuthError,
    JiraPullClient,
    JiraPullItemError,
)
from .conflicts import (
    apply_remote_set_to_core,
    build_snapshot,
    close_conflict,
    load_pending_conflicts,
    upsert_pending,
    write_sync_audit,
)
from .link_guard import normalize_url
from .links import LinkStore, load_source_object_links
from .outbound import assert_target_allowed
from .system_attributes import JIRA_METADATA, is_jira_metadata
from .pull import (
    _load_connection,
    _load_existing_values,
    _load_ota_meta,
    _validate_source_binding,
)
from .value_merge import (
    CONFLICT,
    CONVERGE,
    KEEP_LOCAL,
    NO_OP,
    canonical_repr,
    canonical_set,
    merge_value,
)
from .wire import (
    attribute_create_payload,
    attribute_entry,
    build_update_payload,
    jira_reference_values,
    jira_scalar_values,
    object_create_payload,
    object_type_create_payload,
    reference_type_create_payload,
    remote_reference_ids,
    remote_scalar_items,
    status_create_payload,
)

# --- SQL (plugin-owned tables + Core reads, never a Core write) --------------

_SCHEMA_LINK_SQL = text(
    """
    SELECT l.source_id, l.jira_schema_id
    FROM plugin_jira_schema_links l
    WHERE l.schema_id = CAST(:schema_id AS uuid)
    """
)

_TARGET_SQL = text(
    """
    SELECT id, target_url, connector_type, is_active, schema_id
    FROM targets WHERE id = CAST(:target_id AS uuid)
    """
)

_TARGET_CONNECTION_SQL = text(
    """
    SELECT id, base_url, target_id, default_conflict_strategy
    FROM plugin_jira_connections
    WHERE target_id = CAST(:target_id AS uuid)
    """
)

_SOURCE_ROW_SQL = text(
    "SELECT source_url, source_url_revision FROM sources "
    "WHERE id = CAST(:source_id AS uuid)"
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

# Linked single-value scalar attributes of ONE Core ObjectType (push candidates
# for PASS-2). References and multi-value groups are plan 07.
_LINKED_ATTRIBUTES_SQL = text(
    """
    WITH RECURSIVE ancestors AS (
        SELECT id, parent_id, 0 AS depth FROM object_types
        WHERE id = CAST(:object_type_id AS uuid)
        UNION ALL
        SELECT parent.id, parent.parent_id, a.depth + 1
        FROM ancestors a JOIN object_types parent ON parent.id = a.parent_id
        WHERE parent.inherits_attributes = true AND a.depth < 100
    )
    SELECT l.object_type_attribute_id AS ota_id,
           l.jira_type_attribute_id  AS jira_attr_id,
           ota.object_type_id AS defining_object_type_id, ota.structure
    FROM plugin_jira_attribute_links l
    JOIN object_type_attributes ota ON ota.id = l.object_type_attribute_id
    JOIN ancestors a ON a.id = ota.object_type_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_type_attribute_id IS NOT NULL
      AND (a.depth = 0 OR (ota.is_system = true AND ota.name = 'Name')
           OR ota.structure ->> 'jira_managed_field' IN ('Key', 'Created', 'Updated'))
    """
)

# Every ObjectType of the schema that has a Jira type binding (the PASS-2 scope).
_LINKED_SCHEMA_TYPES_SQL = text(
    """
    SELECT l.object_type_id AS object_type_id, l.jira_type_id AS jira_type_id
    FROM plugin_jira_object_type_links l
    JOIN object_types ot ON ot.id = l.object_type_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_type_id IS NOT NULL
      AND ot.schema_id = CAST(:schema_id AS uuid)
    """
)

_OBJECT_TYPE_EXISTS_SQL = text(
    "SELECT 1 FROM object_types WHERE id = CAST(:object_type_id AS uuid) "
    "AND schema_id = CAST(:schema_id AS uuid)"
)

# --- PASS-1 reads and link lookups (294-05, JSYNC-04) ------------------------

_SCHEMA_STATUSES_SQL = text(
    "SELECT id, name, status_category FROM statuses "
    "WHERE schema_id = CAST(:schema_id AS uuid)"
)
_SCHEMA_REFERENCE_TYPES_SQL = text(
    "SELECT id, reference, description FROM reference_types "
    "WHERE source_id = CAST(:source_id AS uuid)"
)
_SCHEMA_OBJECT_TYPES_SQL = text(
    "SELECT id, name, parent_id, is_abstract, label_attribute_id "
    "FROM object_types WHERE schema_id = CAST(:schema_id AS uuid)"
)
_TYPE_ATTRIBUTES_SQL = text(
    """
    SELECT ota.id, ota.name, ota.maximum_cardinality, ota.structure,
           ota.referenced_object_type_id, ota.reference_type_id,
           at.data_type
    FROM object_type_attributes ota
    JOIN attribute_types at ON at.id = ota.attribute_type_id
    WHERE ota.object_type_id = CAST(:object_type_id AS uuid)
    """
)
_TYPE_OBJECTS_SQL = text(
    "SELECT id, name, object_type_id FROM objects "
    "WHERE schema_id = CAST(:schema_id AS uuid) "
    "AND object_type_id = CAST(:object_type_id AS uuid)"
)
_OBJECT_VALUES_FOR_CREATE_SQL = text(
    """
    SELECT id, object_type_attribute_id, value_simple, value_integer,
           value_float, value_boolean, value_datetime, value_struct
    FROM object_values WHERE object_id = CAST(:object_id AS uuid)
    """
)

# The Core ObjectRefs of one (from-object, reference attribute) group of this
# Source (the local side of a reference set merge, plan 07).
_OBJECT_REFS_SQL = text(
    """
    SELECT id, to_object_id
    FROM object_refs
    WHERE source_id = CAST(:source_id AS uuid)
      AND from_object_id = CAST(:object_id AS uuid)
      AND from_attribute_name = :attribute_name
    ORDER BY id
    """
)

_STATUS_LINK_LOOKUP_SQL = text(
    "SELECT jira_status_id FROM plugin_jira_status_links "
    "WHERE status_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND jira_status_id IS NOT NULL"
)
_REFERENCE_LINK_LOOKUP_SQL = text(
    "SELECT jira_reference_type_id FROM plugin_jira_reference_type_links "
    "WHERE reference_type_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND jira_reference_type_id IS NOT NULL"
)
_TYPE_LINK_LOOKUP_SQL = text(
    "SELECT jira_type_id FROM plugin_jira_object_type_links "
    "WHERE object_type_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND jira_type_id IS NOT NULL"
)
_ATTRIBUTE_LINK_LOOKUP_SQL = text(
    "SELECT jira_type_attribute_id FROM plugin_jira_attribute_links "
    "WHERE object_type_attribute_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND jira_type_attribute_id IS NOT NULL"
)
_OBJECT_LINK_LOOKUP_SQL = text(
    "SELECT jira_object_id FROM plugin_jira_object_links "
    "WHERE object_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND jira_object_id IS NOT NULL"
)

# Bind an object: fill a NULL jira id, never overwrite a bound one (T-294-15).
_BIND_OBJECT_SQL = text(
    """
    INSERT INTO plugin_jira_object_links
        (object_id, source_id, connection_id, jira_object_id, is_detached,
         bound_source_url_revision)
    VALUES (CAST(:object_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_object_id, false,
            CAST(:revision AS bigint))
    ON CONFLICT (object_id) DO UPDATE
        SET jira_object_id = EXCLUDED.jira_object_id,
            bound_source_url_revision = EXCLUDED.bound_source_url_revision
        WHERE plugin_jira_object_links.jira_object_id IS NULL
    """
)

_SYSTEM_ATTR_NAMES = {"Name", "Key", "Created", "Updated"}


class LinkRecorder:
    """Push link recorder: lookup existing Source-bound links, write one per create.

    ``record`` writes exactly one link row (or object bind) and COMMITS it
    immediately, re-checking the live Source binding first, so a crash right
    after a Jira create can never lose the created id (T-294-15).
    """

    def __init__(self, session, store, *, source_id, connection_id, revision):
        self._session = session
        self._store = store
        self._source_id = source_id
        self._connection_id = connection_id
        self._revision = revision

    async def lookup(self, kind, core_id) -> str | None:
        if core_id is None:
            return None
        sql = {
            "status": _STATUS_LINK_LOOKUP_SQL,
            "reference_type": _REFERENCE_LINK_LOOKUP_SQL,
            "object_type": _TYPE_LINK_LOOKUP_SQL,
            "attribute": _ATTRIBUTE_LINK_LOOKUP_SQL,
            "object": _OBJECT_LINK_LOOKUP_SQL,
        }[kind]
        row = (
            await self._session.execute(
                sql, {"id": str(core_id), "source_id": str(self._source_id)}
            )
        ).first()
        return None if row is None else str(row[0])

    async def record_baseline(self, object_value_id, canonical) -> None:
        """Persist one create-time ObjectValue baseline (the export overrides it)."""
        await upsert_value_baseline(
            self._session,
            object_value_id=object_value_id,
            connection_id=self._connection_id,
            canonical=canonical,
        )

    def status_jira_ids(self) -> dict:
        """The Core status id -> Jira status id map this run has linked."""
        return dict(self._store.status_jira_by_id)

    async def record(self, kind, core_id, jira_id, **context) -> None:
        if kind == "status":
            await self._store.link_status(
                status_id=core_id,
                schema_id=context["schema_id"],
                jira_status_id=jira_id,
            )
        elif kind == "reference_type":
            await self._store.link_reference_type(
                reference_type_id=core_id,
                schema_id=None,
                jira_reference_type_id=jira_id,
            )
        elif kind == "object_type":
            await self._store.link_object_type(
                object_type_id=core_id,
                schema_id=context["schema_id"],
                jira_type_id=jira_id,
            )
        elif kind == "attribute":
            await self._store.link_attribute(
                attribute_id=core_id,
                object_type_id=context["object_type_id"],
                jira_attribute_id=jira_id,
            )
        elif kind == "object":
            await self._session.execute(
                _BIND_OBJECT_SQL,
                {
                    "object_id": str(core_id),
                    "source_id": str(self._source_id),
                    "connection_id": str(self._connection_id),
                    "jira_object_id": str(jira_id),
                    "revision": int(self._revision),
                },
            )
        else:
            raise ValueError(f"unknown link kind {kind!r}")
        await self._store.commit()


def _push_include(store, session, schema_id, jira_schema_id):
    """The push ``include(kind, core_id)`` predicate (JSYNC-14).

    Only effective direction push/both passes; a detached object never does.
    Routes through the ONE ``LinkStore.effective_*_direction`` authority.
    """

    async def _type_direction(object_type_id, jira_type_id):
        direction, _origin = store.effective_type_direction(
            schema_id=schema_id, jira_type_id=str(jira_type_id)
        )
        return direction in ("push", "both")

    async def include(kind, core_id):
        if kind == "object_type":
            row = (
                await session.execute(
                    _TYPE_LINK_LOOKUP_SQL,
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).first()
            jira_type_id = row[0] if row else None
            if jira_type_id is None:
                # An unlinked type inherits its schema direction.
                direction, _o = store.effective_type_direction(
                    schema_id=schema_id, jira_type_id="__unlinked__"
                )
                return direction in ("push", "both")
            return await _type_direction(core_id, jira_type_id)
        if kind == "attribute":
            row = (
                await session.execute(
                    text(
                        "SELECT ot.id AS object_type_id, otl.jira_type_id "
                        "FROM object_type_attributes ota "
                        "JOIN object_types ot ON ot.id = ota.object_type_id "
                        "LEFT JOIN plugin_jira_object_type_links otl "
                        "  ON otl.object_type_id = ot.id "
                        " AND otl.source_id = CAST(:source_id AS uuid) "
                        "WHERE ota.id = CAST(:id AS uuid)"
                    ),
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).mappings().first()
            if row is None or row["jira_type_id"] is None:
                return False
            link = (
                await session.execute(
                    _ATTRIBUTE_LINK_LOOKUP_SQL,
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).first()
            attr_jira_id = None if link is None else str(link[0])
            direction, _o = store.effective_attribute_direction(
                schema_id=schema_id,
                jira_type_id=str(row["jira_type_id"]),
                defining_object_type_id=row["object_type_id"],
                jira_attribute_id=attr_jira_id or "__unlinked__",
            )
            return direction in ("push", "both")
        if kind == "object":
            row = (
                await session.execute(
                    text(
                        "SELECT o.object_type_id, otl.jira_type_id, "
                        "       l.is_detached "
                        "FROM objects o "
                        "LEFT JOIN plugin_jira_object_links l "
                        "  ON l.object_id = o.id "
                        " AND l.source_id = CAST(:source_id AS uuid) "
                        "LEFT JOIN plugin_jira_object_type_links otl "
                        "  ON otl.object_type_id = o.object_type_id "
                        " AND otl.source_id = CAST(:source_id AS uuid) "
                        "WHERE o.id = CAST(:id AS uuid)"
                    ),
                    {"id": str(core_id), "source_id": str(store.source_id)},
                )
            ).mappings().first()
            if row is None or row["jira_type_id"] is None:
                return False
            if row["is_detached"]:
                return False
            return await _type_direction(
                row["object_type_id"], row["jira_type_id"]
            )
        return False

    return include


def _type_depth(object_types, object_type_id) -> int:
    depth = 0
    seen: set = set()
    current = object_types.get(object_type_id)
    while current is not None and current["parent_id"] is not None:
        parent = current["parent_id"]
        if parent in seen:
            break
        seen.add(parent)
        current = object_types.get(parent)
        depth += 1
    return depth


async def pass1_create_missing(ctx, session, client, scope, *, recorder, include, counters):
    """Create in Jira what the linked schema lacks, parent-first, committing one
    link row per create (294-RESEARCH Pattern 5)."""
    source_id = scope["source_id"]
    schema_id = scope["schema_id"]
    jira_schema_id = scope["jira_schema_id"]
    object_type_id = scope["object_type_id"]

    # --- Statuses: reuse an existing Jira entry by EXACT name, else create ---
    jira_statuses_by_name: dict[str, str] = {}
    for status in (
        await client.get_status_types(jira_schema_id)
    ) + (await client.get_all_status_types()):
        if isinstance(status, dict) and status.get("name"):
            jira_statuses_by_name.setdefault(status["name"], str(status.get("id")))
    for row in (
        await session.execute(_SCHEMA_STATUSES_SQL, {"schema_id": str(schema_id)})
    ).mappings():
        if await recorder.lookup("status", row["id"]):
            continue
        jira_id = jira_statuses_by_name.get(row["name"])
        if jira_id is None:
            created = await _create_or_skip(
                lambda: client.create_status_type(
                    jira_schema_id,
                    status_create_payload(row["name"], row["status_category"]),
                ),
                session=session,
                scope=scope,
                counters=counters,
                entity_type="status",
                entity_id=row["id"],
            )
            if created is None:
                continue
            jira_id = str(created["id"])
        await recorder.record("status", row["id"], jira_id, schema_id=schema_id)
        counters.created += 1

    # --- Reference types: same exact-name reuse, else create -----------------
    jira_refs_by_name: dict[str, str] = {}
    for reference in await client.get_reference_types(jira_schema_id):
        if isinstance(reference, dict) and reference.get("name"):
            jira_refs_by_name[reference["name"]] = str(reference.get("id"))
    for row in (
        await session.execute(
            _SCHEMA_REFERENCE_TYPES_SQL, {"source_id": str(source_id)}
        )
    ).mappings():
        if await recorder.lookup("reference_type", row["id"]):
            continue
        jira_id = jira_refs_by_name.get(row["reference"])
        if jira_id is None:
            created = await _create_or_skip(
                lambda: client.create_reference_type(
                    jira_schema_id,
                    reference_type_create_payload(row["reference"], row["description"]),
                ),
                session=session,
                scope=scope,
                counters=counters,
                entity_type="reference_type",
                entity_id=row["id"],
            )
            if created is None:
                continue
            jira_id = str(created["id"])
        await recorder.record("reference_type", row["id"], jira_id)
        counters.created += 1

    # --- ObjectTypes: never adopt by name, parents first ---------------------
    object_types = {
        row["id"]: row
        for row in (
            await session.execute(
                _SCHEMA_OBJECT_TYPES_SQL, {"schema_id": str(schema_id)}
            )
        ).mappings()
    }
    if object_type_id is not None:
        # Restrict to the requested type and its ancestors (still created).
        scope_ids = set()
        current = object_type_id
        while current is not None:
            scope_ids.add(current)
            row = object_types.get(current)
            current = None if row is None else row["parent_id"]
        object_types = {
            key: row for key, row in object_types.items() if key in scope_ids
        }
    failed_types: set = set()
    ordered_types = sorted(
        object_types.values(),
        key=lambda row: _type_depth(object_types, row["id"]),
    )
    for row in ordered_types:
        ot_id = row["id"]
        if await recorder.lookup("object_type", ot_id):
            continue
        if not await include("object_type", ot_id):
            continue
        if row["parent_id"] in failed_types:
            failed_types.add(ot_id)
            continue
        parent_jira = (
            await recorder.lookup("object_type", row["parent_id"])
            if row["parent_id"] is not None
            else None
        )
        if row["parent_id"] is not None and parent_jira is None:
            failed_types.add(ot_id)
            continue
        created = await _create_or_skip(
            lambda: client.create_object_type(
                jira_schema_id,
                object_type_create_payload(
                    row["name"],
                    parent_jira_type_id=parent_jira,
                    is_abstract=row["is_abstract"],
                ),
            ),
            session=session,
            scope=scope,
            counters=counters,
            entity_type="object_type",
            entity_id=ot_id,
        )
        if created is None:
            failed_types.add(ot_id)
            continue
        await recorder.record(
            "object_type", ot_id, str(created["id"]), schema_id=schema_id
        )
        counters.created += 1

    # --- Attributes: linked types only, system names excluded ----------------
    for row in object_types.values():
        ot_id = row["id"]
        ot_jira = await recorder.lookup("object_type", ot_id)
        if ot_jira is None or not await include("object_type", ot_id):
            continue
        for attr in (
            await session.execute(
                _TYPE_ATTRIBUTES_SQL, {"object_type_id": str(ot_id)}
            )
        ).mappings():
            if attr["name"] in _SYSTEM_ATTR_NAMES or is_jira_metadata(attr["structure"]):
                continue
            if await recorder.lookup("attribute", attr["id"]):
                continue
            if not await include("attribute", attr["id"]):
                continue
            is_label = str(attr["id"]) == str(row["label_attribute_id"])
            if attr["data_type"] == "reference":
                target_jira = (
                    await recorder.lookup(
                        "object_type", attr["referenced_object_type_id"]
                    )
                    if attr["referenced_object_type_id"] is not None
                    else None
                )
                if target_jira is None:
                    # Unresolved reference: plan 07/09 handles the target.
                    continue
                ref_type_jira = (
                    await recorder.lookup("reference_type", attr["reference_type_id"])
                    if attr["reference_type_id"] is not None
                    else None
                )
                payload = attribute_create_payload(
                    name=attr["name"],
                    data_type="reference",
                    reference_jira_type_id=target_jira,
                    reference_type_jira_id=ref_type_jira,
                    is_label=is_label,
                )
            elif attr["data_type"] == "status":
                status_jira_ids = []
                for value in (
                    await session.execute(
                        text(
                            "SELECT value_struct FROM object_values "
                            "WHERE object_type_attribute_id = CAST(:id AS uuid)"
                        ),
                        {"id": str(attr["id"])},
                    )
                ).mappings():
                    struct = value["value_struct"] or {}
                    status_id = struct.get("id") if isinstance(struct, dict) else None
                    if status_id is None:
                        continue
                    jira_status = await recorder.lookup("status", status_id)
                    if jira_status:
                        status_jira_ids.append(jira_status)
                payload = attribute_create_payload(
                    name=attr["name"],
                    data_type="status",
                    status_jira_ids=status_jira_ids,
                    is_label=is_label,
                )
            else:
                payload = attribute_create_payload(
                    name=attr["name"],
                    data_type=attr["data_type"],
                    structure=attr["structure"],
                    is_label=is_label,
                )
            created = await _create_or_skip(
                lambda: client.create_object_type_attribute(ot_jira, payload),
                session=session,
                scope=scope,
                counters=counters,
                entity_type="attribute",
                entity_id=attr["id"],
            )
            if created is None:
                continue
            await recorder.record(
                "attribute",
                attr["id"],
                str(created["id"]),
                object_type_id=ot_id,
            )
            counters.created += 1

    # --- Objects: unlinked, attached, push/both types ------------------------
    for row in object_types.values():
        ot_id = row["id"]
        ot_jira = await recorder.lookup("object_type", ot_id)
        if ot_jira is None:
            continue
        if not await include("object_type", ot_id):
            continue
        await _ensure_label_binding(
            client,
            recorder,
            object_type_id=ot_id,
            jira_type_id=str(ot_jira),
            label_attribute_id=row["label_attribute_id"],
        )
        for obj in (
            await session.execute(
                _TYPE_OBJECTS_SQL,
                {"schema_id": str(schema_id), "object_type_id": str(ot_id)},
            )
        ).mappings():
            if await recorder.lookup("object", obj["id"]):
                continue
            if not await include("object", obj["id"]):
                continue
            label_jira = await recorder.lookup("attribute", row["label_attribute_id"])
            extra_entries, baselines = await _object_create_entries(
                session, recorder, obj["id"]
            )
            payload = object_create_payload(
                jira_type_id=ot_jira,
                label_jira_attribute_id=label_jira,
                label_value=obj["name"],
                extra_entries=extra_entries,
            )
            created = await _create_or_skip(
                lambda: client.create_object(payload),
                session=session,
                scope=scope,
                counters=counters,
                entity_type="object",
                entity_id=obj["id"],
            )
            if created is None:
                continue
            for value_id, canonical in baselines:
                await recorder.record_baseline(value_id, canonical)
            await recorder.record("object", obj["id"], str(created["id"]))
            counters.created += 1


async def _ensure_label_binding(
    client, recorder, *, object_type_id, jira_type_id, label_attribute_id
) -> None:
    """Adopt Jira's own label attribute onto the Core label when unbound.

    Jira creates every ObjectType's label attribute ("Name") itself and REQUIRES
    a value for it on ``object/create``: the phase-309 live roundtrip proved this
    dev Jira answers ``400`` (``rlabs-insight-attribute-<label id>: At least one
    value must be set``) to a create whose payload carries no label entry. The
    attribute loop never creates a system-named label, so without this binding
    every object create for a fresh push-created type is recorded as an isolated
    skip and no object is ever created. Binding here mirrors the pull's
    ``_link_label_name`` contract (Jira label -> Core label attribute) and also
    heals types linked by an earlier run. Nothing is persisted for the export
    recorder, whose links are deliberately run-scoped.
    """
    if label_attribute_id is None:
        return
    if await recorder.lookup("attribute", label_attribute_id) is not None:
        return
    for attr in await client.get_object_type_attributes(jira_type_id):
        if (
            isinstance(attr, dict)
            and attr.get("label")
            and attr.get("id") is not None
        ):
            await recorder.record(
                "attribute",
                label_attribute_id,
                str(attr["id"]),
                object_type_id=object_type_id,
            )
            return


async def _object_create_entries(session, recorder, object_id):
    """Create entries + create-time baselines for the values actually sent.

    Only attributes whose Jira id is already known (a bound attribute link) can
    be sent; reference values and unresolved attributes are left to PASS-2
    (plan 07) or the export reference pass (plan 09).
    """
    grouped: dict = {}
    order: list = []
    baselines = []
    for value in (
        await session.execute(
            _OBJECT_VALUES_FOR_CREATE_SQL, {"object_id": str(object_id)}
        )
    ).mappings():
        ota_id = value["object_type_attribute_id"]
        jira_attr_id = await recorder.lookup("attribute", ota_id)
        if jira_attr_id is None:
            continue
        meta = (
            await session.execute(
                text(
                    "SELECT at.data_type, ota.structure "
                    "FROM object_type_attributes ota "
                    "JOIN attribute_types at ON at.id = ota.attribute_type_id "
                    "WHERE ota.id = CAST(:id AS uuid)"
                ),
                {"id": str(ota_id)},
            )
        ).mappings().first()
        if meta is None or meta["data_type"] == "reference" or is_jira_metadata(meta["structure"]):
            continue
        typed_column, raw = _typed_column_value(value)
        if typed_column is None:
            continue
        canonical = canonical_repr(typed_column, raw)
        if ota_id not in grouped:
            grouped[ota_id] = {
                "jira_attr_id": jira_attr_id,
                "data_type": meta["data_type"],
                "structure": meta["structure"],
                "reprs": [],
            }
            order.append(ota_id)
        grouped[ota_id]["reprs"].append(canonical)
        baselines.append((value["id"], canonical))
    # Bug 8: exactly one entry per attribute, holding ALL of its values.
    entries = [
        attribute_entry(
            int(grouped[ota_id]["jira_attr_id"]),
            jira_scalar_values(
                grouped[ota_id]["data_type"],
                grouped[ota_id]["reprs"],
                structure=grouped[ota_id]["structure"],
                status_jira_ids=recorder.status_jira_ids(),
            ),
        )
        for ota_id in order
    ]
    return entries, baselines


def _typed_column_value(value):
    for column in (
        "value_simple",
        "value_integer",
        "value_float",
        "value_boolean",
        "value_datetime",
        "value_struct",
    ):
        if value[column] is not None:
            return column, value[column]
    return None, None


def classify_write_failure(exc: BaseException) -> str:
    """Classify one failed Jira write as ``fatal`` or ``isolated`` (D-06).

    401/403 (auth) and connection-wide transport faults are fatal: the whole run
    ends FAILED. A bounded API or per-item fault for one create/PUT is isolated:
    it is audited, its dependents are skipped and independent work continues.
    """
    if isinstance(exc, (JiraPullAuthError, httpx.TransportError)):
        return "fatal"
    if isinstance(exc, (JiraPullApiError, JiraPullItemError)):
        return "isolated"
    return "fatal"


def _skip_category(exc: BaseException) -> str:
    """The bounded, non-secret category persisted for a skipped write (D-06)."""
    if isinstance(exc, JiraPullItemError):
        return "jira_item_error"
    if isinstance(exc, JiraPullApiError):
        return f"jira_api_{exc.status_code}"
    return "jira_write_failed"


async def record_skip_audit(
    session, *, connection_id, direction, entity_type, entity_id, category
) -> None:
    """Persist ONE bounded skip audit row and commit it independently (D-06).

    Only a fixed category lands in ``details`` — never a response body or the
    PAT. Committing alone means a later failure can never erase the evidence.
    """
    await write_sync_audit(
        session,
        connection_id=connection_id,
        direction=direction,
        status="skipped",
        entity_type=str(entity_type)[:50],
        entity_id=entity_id,
        details=f"category={category}"[:1000],
    )
    await session.commit()


async def _create_or_skip(call, *, session, scope, counters, entity_type, entity_id):
    """Run one Jira create; a fatal error re-raises, an isolated one is audited.

    Returns the created payload, or ``None`` after an isolated failure has been
    recorded as a skip on the Target connection (D-06) so the caller can skip
    dependents and continue.
    """
    try:
        return await call()
    except Exception as exc:  # noqa: BLE001 - classified immediately below
        if classify_write_failure(exc) == "fatal":
            raise
        await record_skip_audit(
            session,
            connection_id=scope["target_connection_id"],
            direction="push",
            entity_type=entity_type,
            entity_id=entity_id,
            category=_skip_category(exc),
        )
        counters.skipped += 1
        return None


def _make_transport():
    """HTTP transport for this run; ``None`` selects the real network.

    Module-level so a test can inject an ``httpx`` mock transport for the run.
    """
    return None


class PushCounters:
    """Monotone counters for one push run (reused by plan 09)."""

    def __init__(self) -> None:
        self.pushed = 0
        self.created = 0
        self.skipped = 0
        self.conflicts = 0

    @property
    def succeeded(self) -> int:
        return self.pushed + self.created

    @property
    def attempted(self) -> int:
        return self.succeeded + self.skipped

    def as_result(self) -> dict:
        return {
            "pushed": self.pushed,
            "created": self.created,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
        }


async def _fetch_schema_link(session, schema_id):
    return (
        await session.execute(_SCHEMA_LINK_SQL, {"schema_id": str(schema_id)})
    ).mappings().first()


async def _load_push_scope(
    session, *, source_id, schema_id, target_id, jira_schema_id, object_type_id
):
    """Validate the Target/source/Source instance guard and return the scope.

    Re-run at execution start because the URL can change after admission. A
    mismatch is fatal with zero write requests (T-294-14).
    """
    target = (
        await session.execute(_TARGET_SQL, {"target_id": str(target_id)})
    ).mappings().first()
    if target is None:
        raise PluginTaskError("The push Target does not exist.")
    if target["connector_type"] != "jira":
        raise PluginTaskError("The push Target is not a Jira connector.")
    if not target["is_active"]:
        raise PluginTaskError("The push Target is inactive.")
    if target["schema_id"] is not None and str(target["schema_id"]) != str(
        schema_id
    ):
        raise PluginTaskError("The Target is bound to a different Core schema.")

    source = (
        await session.execute(_SOURCE_ROW_SQL, {"source_id": str(source_id)})
    ).mappings().first()
    if source is None:
        raise PluginTaskError("The push Source does not exist.")

    source_connection = await _load_connection(session, source_id)
    if source_connection is None:
        raise PluginTaskError(
            "No Source-bound Jira connection exists for this Source."
        )
    target_connection = (
        await session.execute(
            _TARGET_CONNECTION_SQL, {"target_id": str(target_id)}
        )
    ).mappings().first()
    if target_connection is None:
        raise PluginTaskError(
            "No Target-bound Jira connection exists for this Target."
        )

    instances = {
        normalize_url(target["target_url"]),
        normalize_url(target_connection["base_url"]),
        normalize_url(source_connection["connection_base_url"]),
        normalize_url(source["source_url"]),
    }
    if None in instances or len(instances) != 1:
        raise PluginTaskError(
            "The push Target is on a different Jira instance than this "
            "schema's Source."
        )

    if object_type_id:
        scope_row = (
            await session.execute(
                _OBJECT_TYPE_EXISTS_SQL,
                {
                    "object_type_id": str(object_type_id),
                    "schema_id": str(schema_id),
                },
            )
        ).first()
        if scope_row is None:
            raise PluginTaskError(
                "The requested ObjectType is not part of the pushed schema."
            )

    return {
        "source_id": source_id,
        "schema_id": schema_id,
        "jira_schema_id": jira_schema_id,
        "target_id": target_id,
        "target_url": source_connection["core_source_url"],
        "target_connection_id": target_connection["id"],
        "source_connection_id": source_connection["connection_id"],
        "target_strategy": target_connection["default_conflict_strategy"],
        "object_type_id": object_type_id,
    }


def _push_decision(direction, decision, *, local_empty) -> str:
    """Map a three-way merge decision onto the push's effective direction (JSYNC-14).

    Returns ``send``, ``skip``, ``converge`` or ``conflict``. At ``push`` Core is
    the source of truth: a divergent or remote-only change is overwritten with the
    local side and never conflicts. At ``both`` the three-way result stands
    (``keep_local`` sends, ``apply``/``no_op`` skip, ``converge`` advances the
    baseline, ``conflict`` stages one pending row). An empty local side is a native
    deletion and is never propagated (A9/D-10).
    """
    if local_empty:
        return "skip"
    if direction == "push":
        if decision == CONVERGE:
            return "converge"
        if decision == NO_OP:
            return "skip"
        return "send"
    if decision == KEEP_LOCAL:
        return "send"
    if decision == CONFLICT:
        return "conflict"
    if decision == CONVERGE:
        return "converge"
    return "skip"


def _single_value_action(direction, local_repr, remote_repr, baseline_repr) -> str:
    """The push action for one single-value attribute (JSYNC-14, A9/D-10)."""
    return _push_decision(
        direction,
        merge_value(local_repr, remote_repr, baseline_repr),
        local_empty=local_repr is None,
    )


def _attribute_baseline_set(existing, attribute_baseline):
    """The whole-attribute baseline set, or ``None`` when none can be trusted.

    An explicit attribute baseline wins; otherwise the set is derived from the
    per-row baselines only when EVERY local row has one (never invented).
    """
    if attribute_baseline is not None:
        return attribute_baseline
    if existing and all(row["baseline"] is not None for row in existing):
        return canonical_set(row["baseline"] for row in existing)
    return None


async def _local_reference_pairs(
    session, *, source_id, object_id, attribute_name, jira_by_core
):
    """The local reference group as ``(linked-jira-id-or-core-id, core-id)`` pairs.

    An unlinked Core target counts as its Core id, so it can never equal a Jira
    id (same representation the pull's reference merge uses, plan 06).
    """
    rows = (
        await session.execute(
            _OBJECT_REFS_SQL,
            {
                "source_id": str(source_id),
                "object_id": str(object_id),
                "attribute_name": attribute_name,
            },
        )
    ).mappings().all()
    pairs = []
    for row in rows:
        core_id = str(row["to_object_id"])
        pairs.append((jira_by_core.get(core_id, core_id), core_id))
    return pairs


def _apply_target_strategy(strategy, *, inserted) -> str:
    """The action the Target's default strategy dictates for a NEW conflict (D-05).

    Only a freshly inserted row is eligible: a re-observation never triggers the
    strategy (D-03/D-05). An unset strategy (or an unknown value) leaves the row
    pending for manual resolution.
    """
    if not inserted or strategy not in ("prefer_local", "prefer_remote"):
        return "pending"
    return "send_local" if strategy == "prefer_local" else "apply_remote"


def _is_multi(meta) -> bool:
    max_card = meta.get("maximum_cardinality")
    return max_card == -1 or (max_card is not None and max_card > 1)


def _remote_attribute(entry, jira_attr_id):
    for attribute in entry.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        if str(attribute.get("objectTypeAttributeId")) == str(jira_attr_id):
            return attribute
    return None


def _baseline_from_response(
    response, jira_attr_id, data_type, structure, fallback, status_resolver=None
) -> str:
    """The normalized repr of a sent attribute from the PUT response, or fallback."""
    if isinstance(response, dict):
        attribute = _remote_attribute(response, jira_attr_id)
        if attribute is not None:
            items = remote_scalar_items(
                data_type, attribute, structure=structure,
                status_resolver=status_resolver,
            )
            if items:
                return items[0]
    return fallback


async def _stage_pending_conflict(
    session, *, descriptor, connection_id, task_id, strategy, direction="push"
):
    """Upsert ONE pending push conflict and apply the Target default to a new row.

    Returns ``(action, PendingConflict)`` with ``action`` in ``pending`` /
    ``send_local`` / ``apply_remote`` (D-05). Only a freshly inserted row is
    eligible for the strategy; a re-observation never triggers it.
    """
    result = await upsert_pending(
        session,
        scope=descriptor["scope"],
        object_id=descriptor["object_id"],
        object_type_attribute_id=descriptor["ota_id"],
        object_value_id=descriptor["object_value_id"],
        connection_id=connection_id,
        direction=direction,
        task_id=task_id,
        local_snapshot=descriptor["local_snapshot"],
        remote_snapshot=descriptor["remote_snapshot"],
    )
    return _apply_target_strategy(strategy, inserted=result.inserted), result


async def _decide_attribute(
    ctx, session, *, store, scope, object_id, object_type_id, jira_type_id,
    entry, attribute_row, meta_cache, local_by_ota, attribute_baselines,
    pending_any, jira_by_core, core_by_jira, target_strategy, task_id, counters,
):
    """Decide the push action for ONE linked attribute of one Object (D-02/D-04/D-05).

    Handles single- and multi-value scalars and reference groups uniformly: the
    local and remote sides become canonical REPRs (scalars) or linked Jira ids
    (references), the three-way merge runs through ``_push_decision``, a pending
    conflict follows D-04/A7, and a NEW conflict is offered to the Target default
    strategy. The returned outcome dict drives the object's single PUT and its
    post-PUT baselines/closures.
    """
    source_id = scope["source_id"]
    schema_id = scope["schema_id"]
    target_connection_id = scope["target_connection_id"]
    ota_id = attribute_row["ota_id"]
    jira_attr_id = str(attribute_row["jira_attr_id"])
    meta = await _load_ota_meta(session, ota_id, meta_cache)
    data_type = meta["data_type"]
    structure = meta["structure"]
    status_resolver = lambda jira_status_id: store.find_status(  # noqa: E731
        schema_id, jira_status_id
    )
    attr_direction, _attr_origin = store.effective_attribute_direction(
        schema_id=schema_id,
        jira_type_id=jira_type_id,
        defining_object_type_id=meta["object_type_id"],
        jira_attribute_id=jira_attr_id,
    )
    base = {
        "ota_id": ota_id,
        "jira_attr_id": jira_attr_id,
        "data_type": data_type,
        "structure": structure,
    }
    if is_jira_metadata(structure):
        # Never send Jira-owned metadata, not even unchanged passthrough values.
        return {**base, "action": "skip", "omit": True}
    if attr_direction in ("pull", "none"):
        return {**base, "action": "skip", "omit": False}

    existing = local_by_ota.get(ota_id, [])
    remote_attribute = _remote_attribute(entry, jira_attr_id)
    is_reference = data_type == "reference"
    is_multi = _is_multi(meta)

    if is_reference:
        attribute_name = meta["name"]
        pairs = await _local_reference_pairs(
            session,
            source_id=source_id,
            object_id=object_id,
            attribute_name=attribute_name,
            jira_by_core=jira_by_core,
        )
        local_items = [
            {"object_id": core, "jira_object_id": jira} for jira, core in pairs
        ]
        local_reps = [jira for jira, _core in pairs]
        unlinked = [
            jira for jira, core in pairs if jira_by_core.get(core) is None
        ]
        remote_reps = remote_reference_ids(remote_attribute)
        remote_items = [
            {"object_id": core_by_jira.get(jira), "jira_object_id": jira}
            for jira in remote_reps
        ]
        encoding, cardinality = "reference", "multi"
        baseline_set = attribute_baselines.get(str(ota_id))
    elif is_multi:
        local_items = [row["repr"] for row in existing]
        local_reps = list(local_items)
        remote_reps = (
            remote_scalar_items(
                data_type, remote_attribute, structure=structure,
                status_resolver=status_resolver,
            )
            if remote_attribute is not None
            else []
        )
        remote_items = list(remote_reps)
        unlinked = []
        encoding, cardinality = "scalar", "multi"
        baseline_set = _attribute_baseline_set(
            existing, attribute_baselines.get(str(ota_id))
        )
    else:
        local = existing[0] if existing else None
        local_items = [local["repr"]] if local is not None else []
        local_reps = list(local_items)
        remote_reps = (
            remote_scalar_items(
                data_type, remote_attribute, structure=structure,
                status_resolver=status_resolver,
            )
            if remote_attribute is not None
            else []
        )[:1]
        remote_items = list(remote_reps)
        unlinked = []
        encoding, cardinality = "scalar", "single"
        baseline_set = None

    local_set = canonical_set(local_reps) if local_reps else None
    remote_set = canonical_set(remote_reps) if remote_reps else None
    pending_row = (
        pending_any.get(existing[0]["id"])
        if (not is_multi and not is_reference and existing)
        else pending_any.get(ota_id)
    )
    object_value_id = (
        existing[0]["id"]
        if (not is_multi and not is_reference and existing)
        else None
    )

    def _descriptor(local_snap, remote_snap):
        scope_kind = "value" if object_value_id is not None else "attribute"
        return {
            "scope": scope_kind,
            "object_id": object_id,
            "ota_id": ota_id,
            "object_value_id": object_value_id,
            "local_snapshot": build_snapshot(
                encoding, cardinality, data_type, local_snap
            ),
            "remote_snapshot": build_snapshot(
                encoding, cardinality, data_type, remote_snap
            ),
        }

    # --- D-04 / A7: an already pending conflict --------------------------------
    if pending_row is not None:
        if remote_set is not None and local_set == remote_set:
            if object_value_id is not None and remote_reps:
                return {
                    **base,
                    "action": "converge",
                    "omit": False,
                    "converged_value": (object_value_id, remote_reps[0]),
                    "close": (pending_row["id"], object_id, "converged"),
                }
            if object_value_id is None:
                return {
                    **base,
                    "action": "converge",
                    "omit": False,
                    "attribute_baseline": remote_set,
                    "close": (pending_row["id"], object_id, "converged"),
                }
            return {**base, "action": "skip", "omit": False}
        if attr_direction == "none":
            return {**base, "action": "skip", "omit": True}
        if attr_direction == "push":
            if not local_reps:
                return {**base, "action": "skip", "omit": True}
            return {
                **base,
                "action": "send",
                "omit": False,
                "encoding": encoding,
                "cardinality": cardinality,
                "send_items": local_reps,
                "value_id": object_value_id,
                "attribute_baseline": None if object_value_id else local_set,
                "close": (pending_row["id"], object_id, "direction push"),
            }
        # both still divergent: refresh the one pending row (D-03).
        await upsert_pending(
            session,
            scope="attribute" if object_value_id is None else "value",
            object_id=object_id,
            object_type_attribute_id=ota_id,
            object_value_id=object_value_id,
            connection_id=target_connection_id,
            direction="push",
            task_id=task_id,
            local_snapshot=build_snapshot(encoding, cardinality, data_type, local_items),
            remote_snapshot=build_snapshot(encoding, cardinality, data_type, remote_items),
        )
        return {
            **base,
            "action": "skip",
            "omit": True,
            "refreshed": True,
            "staged": True,
        }

    # --- fresh decision --------------------------------------------------------
    if is_reference or is_multi:
        if baseline_set is None and attr_direction == "both":
            action = "converge" if local_set == remote_set else "conflict"
        elif baseline_set is None:
            action = "converge" if local_set == remote_set else "send"
        else:
            action = _push_decision(
                attr_direction,
                merge_value(local_set, remote_set, baseline_set),
                local_empty=not local_reps,
            )
    else:
        local = existing[0] if existing else None
        action = _single_value_action(
            attr_direction,
            local["repr"] if local is not None else None,
            remote_reps[0] if remote_reps else None,
            local["baseline"] if local is not None else None,
        )

    if action == "skip":
        return {**base, "action": "skip", "omit": False}
    if action == "converge":
        if object_value_id is not None:
            if remote_reps:
                return {
                    **base,
                    "action": "converge",
                    "omit": False,
                    "converged_value": (object_value_id, remote_reps[0]),
                }
            return {**base, "action": "skip", "omit": False}
        if remote_set is None:
            return {**base, "action": "skip", "omit": False}
        return {
            **base,
            "action": "converge",
            "omit": False,
            "attribute_baseline": remote_set,
        }
    if action == "send":
        if is_reference and unlinked:
            # A local target with no Jira link is never sent and is audited.
            return {
                **base,
                "action": "skip",
                "omit": False,
                "audit_skip": "reference_target_unlinked",
            }
        return {
            **base,
            "action": "send",
            "omit": False,
            "encoding": encoding,
            "cardinality": cardinality,
            "send_items": local_reps,
            "value_id": object_value_id,
            "attribute_baseline": None if object_value_id else local_set,
        }

    # conflict: stage (or refresh) then apply the Target default to a NEW row.
    resolution, result = await _stage_pending_conflict(
        session,
        descriptor=_descriptor(local_items, remote_items),
        connection_id=target_connection_id,
        task_id=task_id,
        strategy=target_strategy,
    )
    counters.conflicts += 1
    if resolution == "send_local":
        return {
            **base,
            "action": "send",
            "omit": False,
            "encoding": encoding,
            "cardinality": cardinality,
            "send_items": local_reps,
            "value_id": object_value_id,
            "attribute_baseline": None if object_value_id else local_set,
            "close": (result.id, object_id, "resolved_auto"),
        }
    if resolution == "apply_remote":
        return {
            **base,
            "action": "apply_remote",
            "omit": True,
            "close": (result.id, object_id, "resolved_auto"),
            "attribute_baseline": (
                remote_set if object_value_id is None else None
            ),
            "converged_value": (
                (object_value_id, remote_reps[0])
                if object_value_id is not None and remote_reps
                else None
            ),
            "apply": {
                "object_value_id": object_value_id,
                "remote_items": remote_items,
            },
        }
    return {**base, "action": "conflict", "omit": True, "staged": True}


async def _apply_remote_outcome(
    ctx, session, *, outcome, scope, object_id, meta_cache, counters
):
    """Write Jira's side into Core via the SDK for one ``prefer_remote`` conflict.

    A raise leaves the conflict pending (T-294-20); success is committed by the
    caller together with the baseline and the ``resolved_auto`` closure.
    """
    meta = await _load_ota_meta(session, outcome["ota_id"], meta_cache)
    apply_info = outcome["apply"]
    remote_items = apply_info["remote_items"]
    apply_scope = "value" if apply_info["object_value_id"] is not None else "attribute"
    try:
        await apply_remote_set_to_core(
            ctx,
            session,
            scope=apply_scope,
            object_id=object_id,
            ota_id=outcome["ota_id"],
            data_type=outcome["data_type"],
            structure=meta["structure"],
            remote_items=remote_items,
            object_value_id=apply_info["object_value_id"],
            object_type_id=meta["object_type_id"],
            source_id=scope["source_id"],
            attribute_name=meta["name"],
        )
    except (CoreDataError, HTTPException) as exc:  # noqa: BLE001 - audited below
        await record_skip_audit(
            session,
            connection_id=scope["target_connection_id"],
            direction="push",
            entity_type="attribute",
            entity_id=outcome["ota_id"],
            category=_skip_category(exc),
        )
        counters.skipped += 1
        return False
    return True


async def pass2_push_values(
    ctx, session, client, store, scope, *, counters, task_id
):
    """Send every locally changed attribute kind to Jira in whole groups (D-02).

    One PUT per object; multi-value scalars and reference groups are merged as
    sets, a divergence stages exactly ONE attribute-scope conflict, a pending
    conflict follows D-04/A7 and a NEW conflict is offered to the Target's default
    strategy (D-05). Baselines land on the SOURCE link connection, audit rows on
    the TARGET connection, all in one plugin transaction after the PUT.
    """
    source_id = scope["source_id"]
    connection_id = scope["source_connection_id"]
    target_connection_id = scope["target_connection_id"]
    schema_id = scope["schema_id"]
    object_type_id = scope["object_type_id"]
    target_strategy = scope.get("target_strategy")

    source_links = await load_source_object_links(
        session, source_id=source_id, connection_id=connection_id
    )
    core_by_jira = dict(source_links)
    jira_by_core = {str(core): jira for jira, core in source_links.items()}

    type_rows = (
        await session.execute(
            _LINKED_SCHEMA_TYPES_SQL,
            {"source_id": str(source_id), "connection_id": str(connection_id),
             "schema_id": str(schema_id)},
        )
    ).mappings().all()
    if object_type_id:
        type_rows = [
            row
            for row in type_rows
            if str(row["object_type_id"]) == str(object_type_id)
        ]

    for type_row in type_rows:
        core_type_id = type_row["object_type_id"]
        jira_type_id = str(type_row["jira_type_id"])
        direction, _origin = store.effective_type_direction(
            schema_id=schema_id, jira_type_id=jira_type_id
        )
        if direction in ("pull", "none"):
            continue

        linked_objects = await _load_type_links(session, scope, core_type_id)
        if not linked_objects:
            continue

        remote_entries = await _load_remote_objects(client, jira_type_id)
        attribute_rows = (
            await session.execute(
                _LINKED_ATTRIBUTES_SQL,
                {
                    "source_id": str(source_id),
                    "connection_id": str(connection_id),
                    "object_type_id": str(core_type_id),
                },
            )
        ).mappings().all()
        if not attribute_rows:
            continue

        metadata_jira_ids = {
            str(row["jira_attr_id"]) for row in attribute_rows
            if is_jira_metadata(row["structure"])
        }
        if any(is_jira_metadata(row["structure"])
               and row["defining_object_type_id"] != core_type_id for row in attribute_rows):
            # Inherited system fields can have type-local Jira IDs. Read their
            # descriptors rather than sending an unknown server-owned field.
            metadata_jira_ids.update(
                str(attr["id"]) for attr in await client.get_object_type_attributes(jira_type_id)
                if attr.get("name") in JIRA_METADATA and attr.get("id") is not None
            )

        meta_cache: dict = {}
        for object_id, jira_object_id in linked_objects:
            entry = remote_entries.get(str(jira_object_id))
            if entry is None:
                continue
            local_by_ota = await _load_existing_values(
                session, object_id, connection_id
            )
            attribute_baselines = {
                str(key): value
                for key, value in (
                    await load_attribute_baselines(session, object_id)
                ).items()
            }
            pending_rows = await load_pending_conflicts(
                session, object_ids=[object_id]
            )
            pending_any = {
                anchor: row for (_oid, anchor), row in (pending_rows or {}).items()
            }

            replaced = []
            response_baselines = []
            converged = []
            attribute_baseline_writes = []
            closures = []
            applies = []
            omitted_ids = set(metadata_jira_ids)
            staged_conflicts = False

            for attribute_row in attribute_rows:
                outcome = await _decide_attribute(
                    ctx,
                    session,
                    store=store,
                    scope=scope,
                    object_id=object_id,
                    object_type_id=core_type_id,
                    jira_type_id=jira_type_id,
                    entry=entry,
                    attribute_row=attribute_row,
                    meta_cache=meta_cache,
                    local_by_ota=local_by_ota,
                    attribute_baselines=attribute_baselines,
                    pending_any=pending_any,
                    jira_by_core=jira_by_core,
                    core_by_jira=core_by_jira,
                    target_strategy=target_strategy,
                    task_id=task_id,
                    counters=counters,
                )
                action = outcome["action"]
                if outcome.get("staged"):
                    staged_conflicts = True
                if outcome.get("omit"):
                    omitted_ids.add(str(outcome["jira_attr_id"]))
                if outcome.get("audit_skip"):
                    await record_skip_audit(
                        session,
                        connection_id=target_connection_id,
                        direction="push",
                        entity_type="attribute",
                        entity_id=outcome["ota_id"],
                        category=outcome["audit_skip"],
                    )
                    counters.skipped += 1
                if action != "apply_remote" and outcome.get("close"):
                    closures.append(outcome["close"])
                if action == "converge":
                    if outcome.get("converged_value"):
                        converged.append(outcome["converged_value"])
                    if outcome.get("attribute_baseline") is not None:
                        attribute_baseline_writes.append(
                            (object_id, outcome["ota_id"], outcome["attribute_baseline"])
                        )
                    continue
                if action == "apply_remote":
                    applies.append(outcome)
                    continue
                if action != "send":
                    continue
                if outcome["encoding"] == "reference":
                    values = jira_reference_values(outcome["send_items"])
                else:
                    values = jira_scalar_values(
                        outcome["data_type"],
                        outcome["send_items"],
                        structure=outcome["structure"],
                        status_jira_ids=store.status_jira_by_id,
                    )
                replaced.append(
                    attribute_entry(int(outcome["jira_attr_id"]), values)
                )
                if outcome.get("value_id") is not None:
                    response_baselines.append(
                        (
                            outcome["value_id"],
                            outcome["jira_attr_id"],
                            outcome["data_type"],
                            outcome["structure"],
                            outcome["send_items"][0],
                        )
                    )
                if outcome.get("attribute_baseline") is not None:
                    attribute_baseline_writes.append(
                        (object_id, outcome["ota_id"], outcome["attribute_baseline"])
                    )

            if (
                not replaced
                and not converged
                and not closures
                and not applies
                and not staged_conflicts
            ):
                continue

            for outcome in applies:
                applied = await _apply_remote_outcome(
                    ctx,
                    session,
                    outcome=outcome,
                    scope=scope,
                    object_id=object_id,
                    meta_cache=meta_cache,
                    counters=counters,
                )
                if not applied:
                    continue
                if outcome.get("close"):
                    closures.append(outcome["close"])
                if outcome.get("converged_value"):
                    converged.append(outcome["converged_value"])
                if outcome.get("attribute_baseline") is not None:
                    attribute_baseline_writes.append(
                        (object_id, outcome["ota_id"], outcome["attribute_baseline"])
                    )

            response = None
            if replaced:
                passthrough = [
                    attribute
                    for attribute in (entry.get("attributes") or [])
                    if str(attribute.get("objectTypeAttributeId")) not in omitted_ids
                ]
                payload = build_update_payload(
                    jira_type_id=int(jira_type_id),
                    remote_attributes=passthrough,
                    replaced_entries=replaced,
                )
                try:
                    response = await client.update_object(jira_object_id, payload)
                except Exception as exc:  # noqa: BLE001 - classified immediately
                    if classify_write_failure(exc) == "fatal":
                        raise
                    await record_skip_audit(
                        session,
                        connection_id=target_connection_id,
                        direction="push",
                        entity_type="object",
                        entity_id=object_id,
                        category=_skip_category(exc),
                    )
                    counters.skipped += 1
                    continue
                counters.pushed += 1

            for value_id, canonical in converged:
                await upsert_value_baseline(
                    session,
                    object_value_id=value_id,
                    connection_id=connection_id,
                    canonical=canonical,
                )
            for value_id, jira_attr_id, data_type, structure, fallback in response_baselines:
                canonical = _baseline_from_response(
                    response,
                    jira_attr_id,
                    data_type,
                    structure,
                    fallback,
                    status_resolver=lambda jid: store.find_status(schema_id, jid),
                )
                await upsert_value_baseline(
                    session,
                    object_value_id=value_id,
                    connection_id=connection_id,
                    canonical=canonical,
                )
            for target_object, ota_id, set_repr in attribute_baseline_writes:
                await upsert_attribute_baseline(
                    session,
                    object_id=target_object,
                    object_type_attribute_id=ota_id,
                    connection_id=connection_id,
                    set_repr=set_repr,
                )
            for conflict_id, conflict_object, reason in closures:
                await close_conflict(
                    session,
                    conflict_id=conflict_id,
                    status="resolved_auto",
                    resolved_by=None,
                    connection_id=target_connection_id,
                    direction="push",
                    object_id=conflict_object,
                    details=f"auto-resolved: {reason}"[:255],
                )

            if replaced:
                await write_sync_audit(
                    session,
                    connection_id=target_connection_id,
                    direction="push",
                    status="success",
                    entity_type="object",
                    entity_id=object_id,
                )
            await session.commit()


async def _load_type_links(session, scope, core_type_id):
    from .links import load_type_object_links

    return await load_type_object_links(
        session,
        source_id=scope["source_id"],
        connection_id=scope["source_connection_id"],
        object_type_id=core_type_id,
    )


async def _load_remote_objects(client, jira_type_id):
    """Every remote object entry of one type, keyed by Jira id (page by page)."""
    entries: dict[str, dict] = {}
    page = 1
    while True:
        page_data = await client.get_objects_page(jira_type_id, page=page, limit=50)
        for entry in page_data.get("values") or []:
            if isinstance(entry, dict) and entry.get("id") is not None:
                entries[str(entry["id"])] = entry
        if not page_data.get("hasNextPage"):
            break
        page = int(page_data["nextPage"])
    return entries


async def finish_outcome(ctx, counters, *, tasktype) -> dict:
    """Report the bounded final step/progress/outcome and return the result.

    Reused by plan 09. ``nothing attempted`` is SUCCESS; every attempted unit
    failing is FAILED (A8/D-10); a mix is PARTIAL_SUCCESS.
    """
    if counters.attempted > 0 and counters.succeeded == 0:
        raise PluginTaskError("Every attempted Jira write failed.")

    if counters.attempted == 0:
        status = TaskStatus.SUCCESS
        label = "SUCCESS"
    elif counters.skipped > 0:
        status = TaskStatus.PARTIAL_SUCCESS
        label = "PARTIAL_SUCCESS"
    else:
        status = TaskStatus.SUCCESS
        label = "SUCCESS"

    await ctx.report_step(
        (f"{label}: {counters.pushed} pushed, {counters.skipped} skipped")[:255]
    )
    await ctx.report_progress(force=True)
    ctx.set_outcome(status)
    task = ctx.task
    return {
        "task_id": str(getattr(task, "id", "")),
        "tasktype": tasktype,
        "schema_id": str(getattr(task, "schema_id", "")),
        "target_id": (
            None if getattr(task, "target_id", None) is None
            else str(task.target_id)
        ),
        "status": label,
        **counters.as_result(),
    }


async def run_push(ctx, task_id, **kwargs: Any) -> dict:
    """Execute one ``jira.sync-push`` for the task's schema, Target and Source."""
    task = ctx.task
    source_id = task.source_id
    schema_id = task.schema_id
    target_id = task.target_id
    object_type_id = kwargs.get("object_type_id")

    if source_id is None:
        raise PluginTaskError("A Jira sync push requires a Source scope.")
    if schema_id is None:
        raise PluginTaskError("A Jira sync push requires a selected schema.")
    if target_id is None:
        raise PluginTaskError("A Jira sync push requires a Target.")

    async with ctx.db() as session:
        link = await _fetch_schema_link(session, schema_id)
        if link is None or link["jira_schema_id"] is None:
            raise PluginTaskError(
                "The selected Core schema is not linked to a Jira schema."
            )
        if str(link["source_id"]) != str(source_id):
            raise PluginTaskError(
                "The selected schema does not belong to this Source."
            )
        jira_schema_id = str(link["jira_schema_id"])

        source_connection = await _load_connection(session, source_id)
        if source_connection is None:
            raise PluginTaskError(
                "No Source-bound Jira connection exists for this Source."
            )
        _validate_source_binding(source_connection)

        account_id = task.service_account_id
        if account_id is None:
            raise PluginTaskError(
                "A Jira sync push requires a bound service account."
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

        scope = await _load_push_scope(
            session,
            source_id=source_id,
            schema_id=schema_id,
            target_id=target_id,
            jira_schema_id=jira_schema_id,
            object_type_id=object_type_id,
        )

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
            # against a fresh answer on every run, and reads the Target row's own
            # current URL and flag.
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
                store = LinkStore(
                    session,
                    source_id=source_id,
                    connection_id=source_connection["connection_id"],
                    connection_revision=source_connection["connection_revision"],
                    core_source_url=source_connection["core_source_url"],
                    connection_url=source_connection["connection_base_url"],
                )
                await store.preload()
                recorder = LinkRecorder(
                    session,
                    store,
                    source_id=source_id,
                    connection_id=source_connection["connection_id"],
                    revision=source_connection["connection_revision"],
                )
                include = _push_include(
                    store, session, schema_id, jira_schema_id
                )
                await pass1_create_missing(
                    ctx,
                    session,
                    client,
                    scope,
                    recorder=recorder,
                    include=include,
                    counters=counters,
                )
                await pass2_push_values(
                    ctx,
                    session,
                    client,
                    store,
                    scope,
                    counters=counters,
                    task_id=task_id,
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

    return await finish_outcome(ctx, counters, tasktype="jira.sync-push")
