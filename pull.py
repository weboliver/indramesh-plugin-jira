"""The ``jira.sync-pull`` runner: the first end-to-end linked-object path (293-01).

This tracer proves the production plugin-task path from an authenticated Task row
through the Jira read adapter to a committed Core write through ``indramesh.sdk``
for ONE already-linked object, and enforces the Source-scoped safety gates the
later importer expands:

* Source-bound plugin connection, validated Source URL/revision and a
  Source-scoped ServiceAccount;
* the selected Core schema must be linked to this Source/connection;
* a run-lifetime per-Source lock (``source_run_lock``);
* ``link_guard.is_stale_binding`` re-checked immediately before the SDK write.

Core writes go only through ``indramesh.sdk`` (no ``app.*`` import, no raw Core
SQL write). Plugin-owned tables are read through the Core-provided session.
"""

import logging
import uuid
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import text

from indramesh.sdk import (
    CoreDataConflict,
    CoreDataError,
    ObjectCreate,
    ObjectRefCreate,
    ObjectTypeAttributeCreate,
    ObjectTypeAttributeUpdate,
    ObjectTypeCreate,
    ObjectTypeUpdate,
    ObjectUpdate,
    ObjectValueCreate,
    ObjectValueUpdate,
    PluginTaskError,
    ReferenceTypeCreate,
    SchemaCreate,
    StatusCreate,
    TaskStatus,
    create_object,
    create_object_ref,
    create_object_type,
    create_object_type_attribute,
    create_object_value,
    create_reference_type,
    create_schema,
    create_status,
    delete_object,
    delete_object_ref,
    delete_object_value,
    update_object,
    update_object_type,
    update_object_type_attribute,
    update_object_value,
)

from .adoption import require_adoptable_schema
from .admission import source_run_lock
from .checkpoints import (
    commit_attribute_effects,
    commit_completed_page,
    finish_type_run,
    load_attribute_baselines,
    load_or_begin_type_run,
    record_retry_ids,
)
from .conflicts import load_pending_conflicts
from .client import (
    JiraPullAuthError,
    JiraPullApiError,
    JiraPullClient,
    JiraPullItemError,
)
from .link_guard import is_stale_binding, normalize_url
from .links import (
    LinkStore,
    load_source_object_links,
    load_stale_candidates,
    load_type_object_links,
)
from .media import reconcile_object_media
from .protection import reconcile_write_protection
from .system_attributes import JIRA_METADATA, METADATA_MARKER, is_jira_metadata
from .value_merge import (
    APPLY,
    CONFLICT,
    CONVERGE,
    KEEP_LOCAL,
    NO_OP,
    canonical_repr,
    canonical_set,
    merge_value,
    normalize_jira_value,
    repr_from_columns,
)
from .wire import (
    attribute_entry,
    build_update_payload,
    jira_reference_values,
    jira_scalar_values,
)

# One bounded page read per linked ObjectType in this tracer (checkpoints and
# multi-page walks land in later plans).
_PULL_PAGE_LIMIT = 50
logger = logging.getLogger(__name__)

_CONNECTION_SQL = text(
    """
    SELECT c.id                             AS connection_id,
           c.source_id                      AS connection_source_id,
           c.base_url                       AS connection_base_url,
           c.bound_source_url_revision      AS connection_revision,
           c.default_conflict_strategy      AS default_conflict_strategy,
           s.source_url                     AS core_source_url,
           s.source_url_revision            AS core_revision
    FROM plugin_jira_connections AS c
    JOIN sources AS s ON s.id = c.source_id
    WHERE c.source_id = CAST(:source_id AS uuid)
      AND c.target_id IS NULL
    LIMIT 1
    """
)

_SCHEMA_LINK_BY_JIRA_SQL = text(
    """
    SELECT schema_id
    FROM plugin_jira_schema_links
    WHERE source_id = CAST(:source_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND jira_schema_id = :jira_schema_id
    LIMIT 1
    """
)

_SCHEMA_LINK_BY_ID_SQL = text(
    """
    SELECT jira_schema_id
    FROM plugin_jira_schema_links
    WHERE source_id = CAST(:source_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND schema_id = CAST(:schema_id AS uuid)
    LIMIT 1
    """
)

_ACCOUNT_SCOPE_SQL = text(
    """
    SELECT 1 FROM service_accounts
    WHERE id = CAST(:account_id AS uuid)
      AND source_id = CAST(:source_id AS uuid)
      AND is_active = true
    LIMIT 1
    """
)

_LINKED_TYPES_SQL = text(
    """
    SELECT otl.object_type_id AS object_type_id, otl.jira_type_id AS jira_type_id,
           ot.name AS object_type_name
    FROM plugin_jira_object_type_links AS otl
    JOIN object_types AS ot ON ot.id = otl.object_type_id
    WHERE otl.source_id = CAST(:source_id AS uuid)
      AND otl.connection_id = CAST(:connection_id AS uuid)
      AND otl.jira_type_id IS NOT NULL
      AND ot.schema_id = CAST(:schema_id AS uuid)
    """
)

# One durable, safe audit row per isolated skipped entry (D-06, T-293-08-03).
# Written and committed OUTSIDE any page transaction, so a page rollback can
# never erase it. Only bounded, non-secret data lands here: the Jira id, the
# task id, a fixed category and an SDK-safe reason — never a PAT or response body.
_SKIP_AUDIT_INSERT_SQL = text(
    """
    INSERT INTO plugin_jira_sync_audit_logs
        (id, connection_id, direction, status, entity_type, entity_id, details)
    VALUES (CAST(:id AS uuid), CAST(:c AS uuid), 'pull', 'skipped',
            :et, :eid, :d)
    """
)

_LINKED_OBJECTS_SQL = text(
    """
    SELECT object_id, jira_object_id
    FROM plugin_jira_object_links
    WHERE source_id = CAST(:source_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND jira_object_id IS NOT NULL
      AND is_detached = false
    """
)

# Detached Jira ids of this Source/connection: their link row is kept, but the
# pull must never touch the object's name, values, refs or media (D-04, 293-07).
_DETACHED_OBJECT_IDS_SQL = text(
    """
    SELECT jira_object_id
    FROM plugin_jira_object_links
    WHERE source_id = CAST(:source_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND jira_object_id IS NOT NULL
      AND is_detached = true
    """
)

_INSERT_SCHEMA_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_schema_links
        (schema_id, source_id, connection_id, jira_schema_id)
    VALUES (CAST(:schema_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_schema_id)
    ON CONFLICT (schema_id) DO NOTHING
    """
)

# --- Typed value import (293-04) ---------------------------------------------

# Existing Core values for one object, each left-joined to its plugin baseline so
# the content-based merge can compare (local, remote, baseline) without a second
# round-trip. The plugin NEVER reads or writes the legacy
# ``ObjectValue.last_synced_*`` columns.
_OBJECT_VALUES_SQL = text(
    """
    SELECT v.id, v.object_type_attribute_id, v.value_simple, v.value_integer,
           v.value_float, v.value_boolean, v.value_datetime, v.value_struct,
           b.last_synced_value AS baseline
    FROM object_values v
    LEFT JOIN plugin_jira_value_sync_state b
      ON b.object_value_id = v.id
     AND b.connection_id = CAST(:connection_id AS uuid)
    WHERE v.object_id = CAST(:object_id AS uuid)
    """
)

_OTA_META_SQL = text(
    """
    SELECT ota.id, ota.object_type_id, ota.name, ota.is_system, ota.maximum_cardinality,
           ota.structure, at.data_type
    FROM object_type_attributes ota
    JOIN attribute_types at ON at.id = ota.attribute_type_id
    WHERE ota.id = CAST(:id AS uuid)
    """
)

# --- Reference reconciliation (293-05, JSYNC-01/JSYNC-13) ---------------------

# The plugin's own Source-scoped reference group for one (from-object, attribute
# name). Grouping is by the Core attribute name, mirroring the Core engine, so
# two attributes that reference the same target stay distinct groups.
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


def _schema_idempotency_key(source_id, jira_schema_id) -> str:
    """The stable key for a Schema bound to one Jira schema within a Source.

    Core prefixes the plugin name and entity kind, so this is only the
    ``{source}:{jira_schema_id}`` scope (D-12). The same key under a different
    Source is a different entity; a different Jira schema is a different entity.
    """
    return f"{source_id}:{jira_schema_id}"


def _object_idempotency_key(source_id, jira_object_id) -> str:
    """The stable key for an Object bound to one Jira object within a Source.

    Jira object ids are unique per instance, so ``{source}:{jira_object_id}`` is
    the identity; Core adds the plugin name and entity kind (D-12).
    """
    return f"{source_id}:{jira_object_id}"


# --- Metadata discovery (293-03) ---------------------------------------------

# Jira attribute "type" index -> data type used by the legacy Core importer.
_JIRA_ATTR_TYPE_MAP = {
    "text": "text",
    "number": "double",
    "integer": "integer",
    "date": "datetime",
    "datetime": "datetime",
    "boolean": "boolean",
    "json": "json",
    "status": "status",
    "version": "version",
    "jira": "jira",
    "confluence": "confluence",
    "reference": "reference",
    "select": "select",
    "url": "url",
    "email": "email",
    "textarea": "textarea",
}

# Jira status "category" int codes (0=Inactive, 1=Active, 2=Pending).
_JIRA_STATUS_CATEGORY = {0: "Inactive", 1: "Active", 2: "Pending"}

_ATTRIBUTE_TYPE_BY_DATA_TYPE_SQL = text(
    "SELECT id FROM attribute_types WHERE data_type = :data_type "
    "ORDER BY created_at ASC LIMIT 1"
)

_ICON_BY_NAME_SQL = text(
    "SELECT id FROM icons WHERE schema_id = CAST(:schema_id AS uuid) "
    "AND name = :name LIMIT 1"
)

_SCHEMA_STATUS_OPTIONS_SQL = text(
    "SELECT id, name, color, status_category FROM statuses "
    'WHERE schema_id = CAST(:schema_id AS uuid) ORDER BY "order", id'
)

_ALL_SCHEMA_LINKS_SQL = text(
    """
    SELECT schema_id, jira_schema_id
    FROM plugin_jira_schema_links
    WHERE source_id = CAST(:source_id AS uuid)
      AND connection_id = CAST(:connection_id AS uuid)
      AND jira_schema_id IS NOT NULL
    ORDER BY jira_schema_id
    """
)


def _object_type_idempotency_key(source_id, schema_id, jira_type_id) -> str:
    """``{source}:{Core schema}:{jira type}`` — the key contains the Core parent."""
    return f"{source_id}:{schema_id}:{jira_type_id}"


def _attribute_idempotency_key(source_id, object_type_id, jira_attribute_id) -> str:
    """``{source}:{defining Core ObjectType}:{jira attribute}`` (D-12)."""
    return f"{source_id}:{object_type_id}:{jira_attribute_id}"


def _status_idempotency_key(source_id, schema_id, jira_status_id) -> str:
    """``{source}:{Core schema}:{jira status}``.

    The Core schema id is ALWAYS in the key, even for a Jira-global status: the
    same global Jira status id materializes as its own schema-bound Core Status
    per schema (the Core Status contract requires a schema).
    """
    return f"{source_id}:{schema_id}:{jira_status_id}"


def _reference_type_idempotency_key(source_id, schema_id, jira_reference_type_id) -> str:
    """``{source}:{Core schema or '-'}:{jira reference type}``.

    A genuinely schema-less Core ReferenceType uses ``-`` so it is shared once
    within the Source while a schema-bound one stays distinct.
    """
    scope = "-" if schema_id is None else str(schema_id)
    return f"{source_id}:{scope}:{jira_reference_type_id}"


def _parent_jira_id(jira_type: dict) -> str | None:
    ref = (
        jira_type.get("parentObjectTypeId")
        or jira_type.get("parentId")
        or jira_type.get("parent_id")
    )
    return str(ref) if ref else None


def _attribute_owner_jira_id(jira_attr: dict) -> str | None:
    owner = jira_attr.get("objectType")
    if isinstance(owner, dict) and owner.get("id") is not None:
        return str(owner["id"])
    raw = jira_attr.get("objectTypeId")
    return str(raw) if raw is not None else None


def _attribute_data_type(jira_attr: dict) -> str:
    """Port the legacy Jira attribute type mapping (reference/status/textarea)."""
    attr_type_value = jira_attr.get("type", 0)
    if attr_type_value == 1:
        jira_type = "reference"
    elif attr_type_value == 7:
        jira_type = "status"
    else:
        default_type = jira_attr.get("defaultType")
        name = ""
        if isinstance(default_type, dict):
            raw_name = default_type.get("name")
            name = raw_name.strip().lower() if isinstance(raw_name, str) else ""
        jira_default_type_id = jira_attr.get("defaultTypeId")
        if jira_default_type_id is None and isinstance(default_type, dict):
            jira_default_type_id = default_type.get("id")
        if not name and jira_default_type_id == 9:
            jira_type = "textarea"
        else:
            jira_type = name or "text"
    return _JIRA_ATTR_TYPE_MAP.get(jira_type, "text")


def _status_category(value) -> str:
    try:
        return _JIRA_STATUS_CATEGORY.get(int(value), "Active")
    except (TypeError, ValueError):
        return "Active"


def _status_color(jira_status: dict) -> str | None:
    """A ``#RRGGBB`` icon string only; anything else is dropped (color max 7)."""
    icon = jira_status.get("icon")
    if isinstance(icon, str) and len(icon) == 7 and icon.startswith("#"):
        try:
            int(icon[1:], 16)
        except ValueError:
            return None
        return icon
    return None


def _ensure_option_ids(raw_options) -> list[dict]:
    """Local port of the Core SELECT-option normalizer (plugin cannot import it)."""
    if not raw_options:
        return []
    normalized: list[dict] = []
    for idx, opt in enumerate(raw_options):
        if isinstance(opt, str):
            entry: dict = {"value": opt}
        elif isinstance(opt, dict):
            entry = dict(opt)
        else:
            continue
        value = entry.get("value")
        if not value:
            continue
        if not isinstance(value, str):
            value = str(value)
        entry["value"] = value
        if not entry.get("id"):
            entry["id"] = str(uuid.uuid4())
        if entry.get("position") is None:
            entry["position"] = idx
        normalized.append(entry)
    return normalized


def _order_types_parent_first(jira_types: list[dict]) -> list[dict]:
    """Depth-first parent-before-child order; refuse cycles/missing parents.

    A parent reference that does not resolve within the schema, or a hierarchy
    with a cycle, is a fatal error (T-293-03-03) — never a silent partial import.
    """
    by_id = {
        str(jt["id"]): jt
        for jt in jira_types
        if isinstance(jt, dict) and jt.get("id") is not None
    }
    ordered: list[dict] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(jira_id: str) -> None:
        if jira_id in visited:
            return
        if jira_id in visiting:
            raise PluginTaskError("Jira returned an ObjectType hierarchy with a cycle.")
        visiting.add(jira_id)
        parent = _parent_jira_id(by_id[jira_id])
        if parent:
            if parent not in by_id:
                raise PluginTaskError(
                    "Jira reported an ObjectType parent that is missing from the schema."
                )
            visit(parent)
        visiting.discard(jira_id)
        visited.add(jira_id)
        ordered.append(by_id[jira_id])

    for jira_id in by_id:
        visit(jira_id)
    return ordered


async def _resolve_attribute_type_id(session, data_type: str) -> uuid.UUID:
    row = (
        await session.execute(
            _ATTRIBUTE_TYPE_BY_DATA_TYPE_SQL, {"data_type": data_type}
        )
    ).scalar()
    if row is None:
        raise PluginTaskError(
            f"No Core attribute type exists for Jira data type {data_type!r}."
        )
    return row


async def _resolve_icon_id(session, schema_id, jira_icon):
    name = jira_icon.get("name") if isinstance(jira_icon, dict) else jira_icon
    if not isinstance(name, str) or not name.strip():
        return None
    return (
        await session.execute(
            _ICON_BY_NAME_SQL, {"schema_id": str(schema_id), "name": name.strip()}
        )
    ).scalar()


async def _build_attribute_structure(
    session, *, schema_id, data_type, jira_attr, referenced_object_type_id
) -> dict | None:
    """Best-effort port of the legacy per-attribute ``structure`` payload."""
    if data_type == "status":
        rows = (
            await session.execute(
                _SCHEMA_STATUS_OPTIONS_SQL, {"schema_id": str(schema_id)}
            )
        ).mappings().all()
        if rows:
            return {
                "status_options": [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "color": r["color"],
                        "status_category": (
                            str(r["status_category"]) if r["status_category"] else None
                        ),
                    }
                    for r in rows
                ]
            }
        return None
    if data_type == "select":
        additional_value = jira_attr.get("additionalValue") or jira_attr.get("options")
        if additional_value:
            if isinstance(additional_value, str):
                options = [
                    {"value": v.strip(), "position": i}
                    for i, v in enumerate(additional_value.split(","))
                    if v.strip()
                ]
            else:
                options = additional_value
            return {"options": _ensure_option_ids(options)}
        return None
    if data_type == "textarea":
        return {"format": "html"}
    if data_type == "reference" and referenced_object_type_id is not None:
        return {"filter": {"objecttypeid": str(referenced_object_type_id)}}
    return None


def _make_transport():
    """The HTTP transport for this run; ``None`` selects the real network.

    A module-level factory so a test can inject an ``httpx`` mock transport for
    the whole run. Production always gets ``None``.
    """
    return None


def _entry_label(entry: dict) -> str | None:
    """The Jira entry's display label, falling back to its first string value."""
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        return label
    for attribute in entry.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        for value in attribute.get("objectAttributeValues") or []:
            if (
                isinstance(value, dict)
                and isinstance(value.get("value"), str)
                and value["value"].strip()
            ):
                return value["value"]
    return None


def _skip_category(exc: BaseException) -> str:
    """The bounded, non-secret category persisted for a skipped entry."""
    if isinstance(exc, JiraPullItemError):
        return exc.category
    return "core_entity_fault"


async def _record_skipped_entry(
    session, *, connection_id, task_id, entity_type, entity_id, category, fault=None
):
    """Persist ONE safe skip audit row and commit it independently.

    The row is committed on its own BEFORE the page transaction, so a later page
    rollback (D-02) can never erase the evidence that an entry was skipped. The
    details carry the task id, category and an SDK-safe CoreDataError message —
    never arbitrary exception text, a PAT or a response body. A failure propagates:
    a run that cannot record why it skipped an entry must fail rather than look
    successful (T-293-08-03).
    """
    reason = (
        " ".join(fault.message.split())[:500]
        if isinstance(fault, CoreDataError) else None
    )
    safe_entity_type = " ".join(str(entity_type).split())[:50]
    safe_entity_id = (
        None if entity_id is None else " ".join(str(entity_id).split())[:100]
    )
    details = f"task={task_id} category={category}"
    if reason:
        details += f" reason={reason}"
        logger.warning(
            "Jira pull skipped %s %s (task=%s category=%s): %s",
            safe_entity_type, safe_entity_id, task_id, category, reason,
        )
    await session.execute(
        _SKIP_AUDIT_INSERT_SQL,
        {
            "id": str(uuid.uuid4()),
            "c": str(connection_id),
            "et": safe_entity_type,
            "eid": safe_entity_id,
            "d": details[:1000],
        },
    )
    await session.commit()


async def _load_connection(session, source_id):
    return (
        await session.execute(_CONNECTION_SQL, {"source_id": str(source_id)})
    ).mappings().first()


def _validate_source_binding(connection) -> None:
    """Fail closed unless the live Source URL/revision matches the bind snapshot."""
    core_url = normalize_url(connection["core_source_url"])
    connection_url = normalize_url(connection["connection_base_url"])
    if core_url is None or connection_url is None or core_url != connection_url:
        raise PluginTaskError(
            "The Source URL does not match this Jira connection binding."
        )
    core_revision = connection["core_revision"]
    connection_revision = connection["connection_revision"]
    if core_revision is None or connection_revision is None:
        raise PluginTaskError("The Source URL revision could not be verified.")
    if int(core_revision) != int(connection_revision):
        raise PluginTaskError(
            "The Source URL changed after this Jira connection was bound."
        )


async def _validate_account_scope(session, account_id, source_id) -> None:
    row = (
        await session.execute(
            _ACCOUNT_SCOPE_SQL,
            {"account_id": str(account_id), "source_id": str(source_id)},
        )
    ).first()
    if row is None:
        raise PluginTaskError(
            "The task's service account is not bound to this Source."
        )


async def _resolve_schema_id(session, connection, selected_schema_id, jira_schema_id):
    """Resolve the selected Core schema AND its Jira schema id, requiring a link.

    A Core schema that is not linked to this Source/connection is refused (an
    empty unlinked schema is never adopted by the tracer; a nonempty unlinked one
    is exactly the case the ROADMAP forbids).
    """
    params = {
        "source_id": str(connection["connection_source_id"]),
        "connection_id": str(connection["connection_id"]),
    }
    if jira_schema_id is not None:
        params["jira_schema_id"] = str(jira_schema_id)
        row = (await session.execute(_SCHEMA_LINK_BY_JIRA_SQL, params)).first()
        if row is None:
            raise PluginTaskError(
                "The requested Jira schema is not linked to this Source."
            )
        # An optional Core ``task.schema_id`` is a constraint, not a second path:
        # it must be the very same linked Core schema, otherwise the run is
        # refused before any SDK mutation (T-293-08-02).
        if selected_schema_id is not None and str(row[0]) != str(selected_schema_id):
            raise PluginTaskError(
                "The selected Core schema is not linked to the requested Jira schema."
            )
        return row[0], str(jira_schema_id)
    if selected_schema_id is not None:
        params["schema_id"] = str(selected_schema_id)
        row = (await session.execute(_SCHEMA_LINK_BY_ID_SQL, params)).first()
        if row is None or row[0] is None:
            raise PluginTaskError(
                "The selected Core schema is not linked to this Source."
            )
        return selected_schema_id, str(row[0])
    raise PluginTaskError("A Jira sync pull requires a selected schema.")


async def _bootstrap_schema(
    ctx, session, connection, source_id, jira_schema_id, client, target_schema_id=None
):
    """Create and link a new Core Schema for an unlinked Jira schema (D-12).

    Without an explicit target, always create a keyed new schema, never adopt a
    user's existing schema by name or contents. The Core create and the
    schema-link insert are two separate commits (D-01/D-03): a crash between
    them leaves the Core schema (replayable through the deterministic id) and no
    link, so the next run re-reads that exact schema instead of creating a
    duplicate.

    Quick 261004: an explicitly selected ``target_schema_id`` is adopted instead
    — re-checked under a row lock to be an empty, unlinked schema of this Source
    — and only linked, so its name and keygen prefix stay the user's.
    """
    if target_schema_id is not None:
        await require_adoptable_schema(
            session, source_id, target_schema_id, lock=True
        )
        await session.execute(
            _INSERT_SCHEMA_LINK_SQL,
            {
                "schema_id": str(target_schema_id),
                "source_id": str(source_id),
                "connection_id": str(connection["connection_id"]),
                "jira_schema_id": str(jira_schema_id),
            },
        )
        await session.commit()
        return target_schema_id, str(jira_schema_id)

    definition = await client.get_schema(str(jira_schema_id))
    name = definition.get("name") if isinstance(definition, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise PluginTaskError("The Jira schema response is malformed.")
    try:
        created = await create_schema(
            ctx,
            SchemaCreate(source_id=source_id, name=name.strip()[:255]),
            idempotency_key=_schema_idempotency_key(source_id, jira_schema_id),
        )
    except CoreDataError as exc:
        raise PluginTaskError(exc.message) from None

    await session.execute(
        _INSERT_SCHEMA_LINK_SQL,
        {
            "schema_id": str(created.id),
            "source_id": str(source_id),
            "connection_id": str(connection["connection_id"]),
            "jira_schema_id": str(jira_schema_id),
        },
    )
    await session.commit()
    return created.id, str(jira_schema_id)


async def _plan_scopes(
    session, connection, selected_schema_id, jira_schema_id, target_schema_id=None
):
    """The (Core schema id, Jira schema id) pairs a run would import, read-only.

    A ``None`` Core id marks an unlinked Jira schema the run bootstraps as a new
    keyed schema (D-12), or — with an explicit ``target_schema_id`` (261004) —
    by adopting that empty, unlinked schema of the Source. An explicit Core
    schema constraint must resolve by link and never permits adoption or a
    second schema (T-293-08-02). Shared by ``admit_pull`` and
    ``_resolve_scopes`` so admission and run never diverge.
    """
    if target_schema_id is not None:
        if jira_schema_id is None or selected_schema_id is not None:
            raise PluginTaskError(
                "Pulling into an existing schema requires exactly one selected "
                "Jira schema."
            )
        try:
            linked = await _resolve_schema_id(session, connection, None, jira_schema_id)
        except PluginTaskError:
            await require_adoptable_schema(
                session, connection["connection_source_id"], target_schema_id
            )
            return [(None, str(jira_schema_id))]
        if str(linked[0]) != str(target_schema_id):
            raise PluginTaskError(
                "This Jira schema is already linked to another Core schema."
            )
        return [linked]
    if selected_schema_id is not None:
        return [
            await _resolve_schema_id(
                session, connection, selected_schema_id, jira_schema_id
            )
        ]
    if jira_schema_id is not None:
        try:
            return [
                await _resolve_schema_id(session, connection, None, jira_schema_id)
            ]
        except PluginTaskError:
            return [(None, str(jira_schema_id))]
    rows = (
        await session.execute(
            _ALL_SCHEMA_LINKS_SQL,
            {
                "source_id": str(connection["connection_source_id"]),
                "connection_id": str(connection["connection_id"]),
            },
        )
    ).mappings().all()
    if not rows:
        raise PluginTaskError(
            "This Source has no linked Jira schema yet; select a Jira schema "
            "to pull first."
        )
    return [(row["schema_id"], str(row["jira_schema_id"])) for row in rows]


# --- Typed value import and content-based three-way merge (293-04, D-01..D-03) --


def _baseline_repr(last_synced_value) -> str | None:
    """The canonical repr stored in a plugin baseline row, or ``None``."""
    if isinstance(last_synced_value, dict):
        return last_synced_value.get("canonical")
    if isinstance(last_synced_value, str):
        return last_synced_value
    return None


async def _load_existing_values(session, object_id, connection_id):
    """Group this object's Core values (plus plugin baselines) by attribute id."""
    rows = (
        await session.execute(
            _OBJECT_VALUES_SQL,
            {
                "object_id": str(object_id),
                "connection_id": str(connection_id),
            },
        )
    ).mappings().all()
    grouped: dict = defaultdict(list)
    for row in rows:
        typed, value = repr_from_columns(row)
        if typed is None:
            continue
        grouped[row["object_type_attribute_id"]].append(
            {
                "id": row["id"],
                "typed": typed,
                "value": value,
                "repr": canonical_repr(typed, value),
                "baseline": _baseline_repr(row["baseline"]),
            }
        )
    return grouped


async def _load_pending_for_object(session, object_id):
    """The object's pending conflicts keyed by anchor (ObjectValue or OTA id).

    One read per object feeds both the value merge (a value-scope row anchored on
    the ObjectValue) and the attribute merge (an attribute-scope row anchored on
    the ObjectTypeAttribute), so D-03/D-04 can be applied per anchor.
    """
    rows = await load_pending_conflicts(session, object_ids=[object_id])
    return None if not rows else {anchor: row for (_oid, anchor), row in rows.items()}


async def _load_ota_meta(session, ota_id, cache):
    key = str(ota_id)
    if key in cache:
        return cache[key]
    row = (
        await session.execute(_OTA_META_SQL, {"id": key})
    ).mappings().first()
    if row is None:
        raise PluginTaskError("A linked Core attribute is unavailable.")
    meta = dict(row)
    cache[key] = meta
    return meta


async def _sdk_create_value(ctx, object_id, ota_id, normalized):
    typed, value, _ = normalized
    created = await create_object_value(
        ctx,
        ObjectValueCreate(
            object_id=object_id,
            object_type_attribute_id=ota_id,
            **{typed: value},
        ),
    )
    return created.id


async def _sdk_update_value(ctx, object_value_id, normalized):
    typed, value, _ = normalized
    await update_object_value(
        ctx, object_value_id, ObjectValueUpdate(**{typed: value})
    )


async def _sdk_delete_value(ctx, object_value_id):
    await delete_object_value(ctx, object_value_id)


async def _normalize_attribute_values(
    data_type, structure, raw_values, store, schema_id
):
    """Normalize one Jira attribute's values into ``(typed_col, value, repr)``.

    Returns ``(normalized, incomplete)``: ``incomplete`` is set when Jira reported
    a value this pass could not normalize, so the remote set is not authoritative.
    """
    normalized: list[tuple[str, object, str]] = []
    incomplete = False
    if data_type == "status":
        for raw in raw_values:
            status = raw.get("status")
            if not isinstance(status, dict):
                incomplete = True
                continue
            jira_status_id = str(status.get("id", ""))
            core_status_id = store.find_status(schema_id, jira_status_id)
            if core_status_id is None:
                # Unknown local Status: never fabricate one (anti-fabrication).
                incomplete = True
                continue
            struct = {"id": str(core_status_id), "name": status.get("name", "")}
            normalized.append(
                ("value_struct", struct, canonical_repr("value_struct", struct))
            )
        return normalized, incomplete

    for raw in raw_values:
        typed, value, repr_ = normalize_jira_value(
            data_type, raw.get("value"), structure=structure
        )
        if typed is not None:
            normalized.append((typed, value, repr_))
        else:
            incomplete = True
    return normalized, incomplete


def _pull_decision(direction, decision, *, local_empty=False):
    """Map a three-way merge decision onto the pull's effective direction (JSYNC-14).

    At ``pull`` Jira is the source of truth: a local value/set is never kept and
    never conflicts, so ``keep_local`` and ``conflict`` both become ``apply``
    (``converge``/``no_op`` are unchanged). At ``both`` the three-way result
    stands, except that an empty local value/set with a real baseline is a native
    deletion the pull restores from Jira (A9, D-10). ``none``/``push`` never reach
    a merge — the caller filters them before staging.
    """
    if direction == "pull":
        if decision in (KEEP_LOCAL, CONFLICT):
            return APPLY
        return decision
    if local_empty and decision == CONFLICT:
        return APPLY
    return decision


def _stage_closure(staged_closures, pending_row, reason):
    """Stage one D-04/A7 auto-closure of a pending conflict for the page commit."""
    staged_closures.append(
        {
            "conflict_id": pending_row["id"],
            "object_id": pending_row["object_id"],
            "details": f"auto-resolved: {reason}"[:255],
        }
    )


def _effective_baseline_set(existing, attribute_baseline):
    """The whole-attribute baseline set, or ``None`` when none can be trusted.

    An explicit attribute baseline wins; otherwise the set is derived from the
    per-row baselines only when EVERY local row has one. A group with no
    derivable baseline is treated exactly as in 293: Jira wins, and the attribute
    baseline is recorded from the remote set.
    """
    if attribute_baseline is not None:
        return attribute_baseline
    if existing and all(row["baseline"] is not None for row in existing):
        return canonical_set(row["baseline"] for row in existing)
    return None


def _stage_matching_row_baselines(existing, normalized, staged_baselines):
    """Stage per-row baselines for local rows still equal to a remote value."""
    remote_reprs = {item[2] for item in normalized}
    for row in existing:
        if row["repr"] in remote_reprs:
            staged_baselines.append((row["id"], row["repr"]))


async def _converge_multi_value_rows(
    ctx, *, object_id, ota_id, existing, normalized, staged_baselines, force=False
):
    """Converge local rows to the remote multiset by content (D-02).

    Equal rows are kept, a reusable row is reused for a changed value through the
    SDK, a missing value is created, and a surplus reusable row is deleted. At
    ``both``/no-baseline only a synced row (``repr == baseline``) is reusable, so
    a local-only row is never swept; at ``pull`` (``force``) Jira is authoritative
    and every local row may be reused or removed.
    """
    remaining = list(existing)
    unmatched = []
    for item in normalized:
        match = next((row for row in remaining if row["repr"] == item[2]), None)
        if match is not None:
            remaining.remove(match)
            staged_baselines.append((match["id"], item[2]))
        else:
            unmatched.append(item)
    reusable = (
        list(remaining)
        if force
        else [
            row
            for row in remaining
            if row["baseline"] is not None and row["repr"] == row["baseline"]
        ]
    )
    for item in unmatched:
        if reusable:
            row = reusable.pop(0)
            await _sdk_update_value(ctx, row["id"], item)
            staged_baselines.append((row["id"], item[2]))
            continue
        new_id = await _sdk_create_value(ctx, object_id, ota_id, item)
        staged_baselines.append((new_id, item[2]))
    for row in reusable:
        await _sdk_delete_value(ctx, row["id"])


def _collect_write_back(
    strategy_state,
    *,
    conflict,
    ota_id,
    jira_attr_id,
    structure,
    data_type,
    local_items,
    cardinality,
    object_value_id=None,
    baseline_repr=None,
    baseline_set=None,
):
    """Collect one ``prefer_local`` conflict for the object's single write-back PUT.

    The conflict mapping is kept by reference so the PUT result can set its
    ``auto_resolve`` flag and stage the local baseline only on success (D-10).
    """
    if strategy_state is None:
        return
    strategy_state["write_back"].append(
        {
            "conflict": conflict,
            "ota_id": ota_id,
            "jira_attr_id": jira_attr_id,
            "structure": structure,
            "data_type": data_type,
            "local_items": list(local_items),
            "cardinality": cardinality,
            "object_value_id": object_value_id,
            "baseline_repr": baseline_repr,
            "baseline_set": baseline_set,
        }
    )


async def _flush_write_back(ctx, session, state) -> None:
    """Send the object's ONE ``prefer_local`` write-back PUT (D-05/D-10).

    The local value/set is written to Jira with the run's Source PAT; the entry's
    other remote attributes are passed through untouched. Success advances the
    baseline to the LOCAL state and marks each collected conflict
    ``auto_resolve``; a 401/403/transport fault leaves the conflicts pending and
    records one bounded skip audit row — the pull continues.
    """
    replaced = []
    for item in state["write_back"]:
        if item["data_type"] == "reference":
            values = jira_reference_values(item["local_items"])
        else:
            values = jira_scalar_values(
                item["data_type"],
                item["local_items"],
                structure=item["structure"],
                status_jira_ids=state.get("status_jira_ids"),
            )
        replaced.append(attribute_entry(item["jira_attr_id"], values))
    payload = build_update_payload(
        jira_type_id=int(state["jira_type_id"]),
        remote_attributes=[
            attr for attr in state["entry"].get("attributes") or []
            if str(attr.get("objectTypeAttributeId")) not in state.get("metadata_jira_ids", set())
        ],
        replaced_entries=replaced,
    )
    try:
        await state["client"].update_object(state["jira_object_id"], payload)
    except (JiraPullAuthError, JiraPullApiError, httpx.TransportError):
        await _record_skipped_entry(
            session,
            connection_id=state["connection_id"],
            task_id=state["task_id"],
            entity_type="object",
            entity_id=state["jira_object_id"],
            category="write_back_failed",
        )
        return
    for item in state["write_back"]:
        item["conflict"]["auto_resolve"] = True
        if item["cardinality"] == "multi":
            state["staged_attribute_baselines"].append(
                (state["object_id"], item["ota_id"], item["baseline_set"])
            )
        else:
            state["staged_baselines"].append(
                (item["object_value_id"], item["baseline_repr"])
            )


async def _merge_attribute_group(
    ctx,
    *,
    object_id,
    ota_id,
    data_type,
    maximum_cardinality,
    normalized,
    existing,
    staged_baselines,
    staged_conflicts,
    incomplete=False,
    direction="both",
    attribute_baseline=None,
    pending=None,
    staged_attribute_baselines=None,
    staged_attribute_conflicts=None,
    staged_closures=None,
    strategy=None,
    strategy_state=None,
    jira_attr_id=None,
    structure=None,
):
    """Apply the content-based three-way merge for one (object, attribute) group.

    Single-value attributes target the one existing row. Multi-value attributes
    merge as WHOLE SETS against the attribute baseline (D-02/WR-04): the local
    multiset, the remote multiset and the baseline set decide via
    ``merge_value``; ``apply`` converges the local rows by content, a ``conflict``
    stages exactly ONE attribute-scope conflict holding the complete sets, so a
    locally edited set can never be silently swallowed. A group without a
    derivable baseline behaves as in 293 (Jira wins) and records the baseline.

    An ``incomplete`` remote set (a Jira value that could not be normalized) is
    never authoritative: no local row is deleted or reused for it — only missing
    resolvable values are added (T-294-18).

    ``pending`` is this object's pending conflicts keyed by anchor. A pending row
    on this anchor is kept current (D-03), applied and closed when the direction
    became ``pull`` (D-04), or closed as ``resolved_auto`` when the two sides
    converged (A7).
    """
    if staged_attribute_baselines is None:
        staged_attribute_baselines = []
    if staged_attribute_conflicts is None:
        staged_attribute_conflicts = []
    if staged_closures is None:
        staged_closures = []
    max_card = maximum_cardinality
    is_multi = max_card == -1 or (max_card is not None and max_card > 1)

    if not is_multi:
        local = existing[0] if existing else None
        remote = normalized[0] if normalized else None
        if remote is None and (local is None or incomplete):
            return
        local_repr = local["repr"] if local else None
        remote_repr = remote[2] if remote else None
        baseline_repr = local["baseline"] if local else None
        pending_row = (
            (pending or {}).get(local["id"]) if local is not None else None
        )
        if (
            pending_row is not None
            and remote_repr is not None
            and local_repr == remote_repr
        ):
            # A7: the two sides converged while the conflict was pending.
            staged_baselines.append((local["id"], remote_repr))
            _stage_closure(staged_closures, pending_row, "converged")
            return
        decision = _pull_decision(
            direction,
            merge_value(local_repr, remote_repr, baseline_repr),
            local_empty=local is None,
        )
        if decision in (NO_OP, KEEP_LOCAL):
            return
        if decision == CONVERGE:
            if local is not None and remote_repr is not None:
                staged_baselines.append((local["id"], remote_repr))
            if pending_row is not None:
                _stage_closure(staged_closures, pending_row, "converged")
            return
        if decision == CONFLICT:
            conflict = {
                "object_id": object_id,
                "object_value_id": local["id"],
                "object_type_attribute_id": ota_id,
                "data_type": data_type,
                "local_value": local_repr,
                "remote_value": remote_repr,
            }
            if pending_row is None and strategy in ("prefer_remote", "prefer_local"):
                # D-05/D-10: the Source default applies only to a NEW conflict.
                if strategy == "prefer_remote":
                    await _sdk_update_value(ctx, local["id"], remote)
                    staged_baselines.append((local["id"], remote_repr))
                    conflict["auto_resolve"] = True
                else:
                    _collect_write_back(
                        strategy_state,
                        conflict=conflict,
                        ota_id=ota_id,
                        jira_attr_id=jira_attr_id,
                        structure=structure,
                        data_type=data_type,
                        local_items=[local_repr],
                        cardinality="single",
                        object_value_id=local["id"],
                        baseline_repr=local_repr,
                    )
            staged_conflicts.append(conflict)
            return
        # APPLY
        if remote is None:
            if local is not None:
                await _sdk_delete_value(ctx, local["id"])
            if pending_row is not None:
                _stage_closure(staged_closures, pending_row, "direction pull")
            return
        if local is None:
            new_id = await _sdk_create_value(ctx, object_id, ota_id, remote)
            staged_baselines.append((new_id, remote_repr))
        else:
            await _sdk_update_value(ctx, local["id"], remote)
            staged_baselines.append((local["id"], remote_repr))
        if pending_row is not None:
            _stage_closure(staged_closures, pending_row, "direction pull")
        return

    if incomplete:
        # A remote value could not be normalized: the remote set is not
        # authoritative, so no local row is removed or reused. Missing resolvable
        # values are still added (T-294-18, 293-05 contract).
        remaining = list(existing)
        unmatched = []
        for item in normalized:
            match = next((row for row in remaining if row["repr"] == item[2]), None)
            if match is not None:
                staged_baselines.append((match["id"], item[2]))
                remaining.remove(match)
            else:
                unmatched.append(item)
        for item in unmatched:
            match = next(
                (row for row in remaining if row["baseline"] == item[2]), None
            )
            if match is not None:
                remaining.remove(match)
            else:
                new_id = await _sdk_create_value(ctx, object_id, ota_id, item)
                staged_baselines.append((new_id, item[2]))
        return

    local_set = canonical_set(row["repr"] for row in existing) if existing else None
    remote_set = (
        canonical_set(item[2] for item in normalized) if normalized else None
    )
    pending_row = (pending or {}).get(ota_id)
    if (
        pending_row is not None
        and remote_set is not None
        and local_set == remote_set
    ):
        # A7: the two sides converged while the conflict was pending.
        staged_attribute_baselines.append((object_id, ota_id, remote_set))
        _stage_closure(staged_closures, pending_row, "converged")
        return
    baseline_set = _effective_baseline_set(existing, attribute_baseline)
    if baseline_set is None:
        # No trustworthy baseline: 293 behavior, Jira wins, then record it.
        decision = APPLY
    else:
        decision = _pull_decision(
            direction,
            merge_value(local_set, remote_set, baseline_set),
            local_empty=not existing,
        )
    if decision in (NO_OP, KEEP_LOCAL):
        if decision == KEEP_LOCAL:
            _stage_matching_row_baselines(existing, normalized, staged_baselines)
        return
    if decision == CONVERGE:
        _stage_matching_row_baselines(existing, normalized, staged_baselines)
        if remote_set is not None:
            staged_attribute_baselines.append((object_id, ota_id, remote_set))
        if pending_row is not None:
            _stage_closure(staged_closures, pending_row, "converged")
        return
    if decision == CONFLICT:
        conflict = {
            "object_id": object_id,
            "object_value_id": None,
            "object_type_attribute_id": ota_id,
            "data_type": data_type,
            "kind": "scalar",
            "cardinality": "multi",
            "local_items": [row["repr"] for row in existing],
            "remote_items": [item[2] for item in normalized],
        }
        if pending_row is None and strategy in ("prefer_remote", "prefer_local"):
            # D-05/D-10: the Source default applies only to a NEW conflict.
            if strategy == "prefer_remote":
                # Jira wins for the whole set: converge every local row (the
                # conflict row below keeps the local snapshot, so nothing is lost).
                await _converge_multi_value_rows(
                    ctx,
                    object_id=object_id,
                    ota_id=ota_id,
                    existing=existing,
                    normalized=normalized,
                    staged_baselines=staged_baselines,
                    force=True,
                )
                if remote_set is not None:
                    staged_attribute_baselines.append((object_id, ota_id, remote_set))
                conflict["auto_resolve"] = True
            else:
                _collect_write_back(
                    strategy_state,
                    conflict=conflict,
                    ota_id=ota_id,
                    jira_attr_id=jira_attr_id,
                    structure=structure,
                    data_type=data_type,
                    local_items=[row["repr"] for row in existing],
                    cardinality="multi",
                    baseline_set=local_set,
                )
        staged_attribute_conflicts.append(conflict)
        return
    # APPLY: converge the local rows to the remote multiset, then record it.
    await _converge_multi_value_rows(
        ctx,
        object_id=object_id,
        ota_id=ota_id,
        existing=existing,
        normalized=normalized,
        staged_baselines=staged_baselines,
        force=direction == "pull",
    )
    if remote_set is not None:
        staged_attribute_baselines.append((object_id, ota_id, remote_set))
    if pending_row is not None:
        _stage_closure(staged_closures, pending_row, "direction pull")


async def _import_object_values(
    ctx,
    session,
    store,
    *,
    entry,
    object_id,
    jira_type_id,
    schema_id,
    connection_id,
    ota_meta_cache,
    staged_baselines,
    staged_conflicts,
    staged_attribute_baselines=None,
    staged_attribute_conflicts=None,
    staged_closures=None,
    source_strategy=None,
    client=None,
    task_id=None,
):
    """Import every mapped Jira attribute value for one object (293-04)."""
    if staged_attribute_baselines is None:
        staged_attribute_baselines = []
    if staged_attribute_conflicts is None:
        staged_attribute_conflicts = []
    if staged_closures is None:
        staged_closures = []
    jira_attrs = entry.get("attributes")
    if not jira_attrs:
        return
    if not isinstance(jira_attrs, list):
        # A single object's payload is malformed: a narrowly recoverable item
        # fault, not a systemic one — the page continues and the id is retried
        # (D-04/D-06). A malformed PAGE envelope stays fatal in the client.
        raise JiraPullItemError(
            "The Jira object attribute list is malformed.",
            category="malformed_object_payload",
        )

    existing_by_ota = await _load_existing_values(session, object_id, connection_id)
    attribute_baselines = await load_attribute_baselines(session, object_id)
    pending = await _load_pending_for_object(session, object_id)

    # D-05: a Source default strategy applies only to conflicts NEWLY detected in
    # this run; the collection is flushed as ONE write-back PUT per object.
    strategy_state = None
    if client is not None and source_strategy in ("prefer_local", "prefer_remote"):
        strategy_state = {
            "write_back": [],
            "client": client,
            "entry": entry,
            "jira_type_id": jira_type_id,
            "jira_object_id": entry.get("id"),
            "object_id": object_id,
            "connection_id": connection_id,
            "task_id": task_id,
            "staged_baselines": staged_baselines,
            "staged_attribute_baselines": staged_attribute_baselines,
            "status_jira_ids": store.status_jira_by_id,
            "metadata_jira_ids": set(),
        }

    for jattr in jira_attrs:
        if not isinstance(jattr, dict):
            raise JiraPullItemError(
                "The Jira object attribute entry is malformed.",
                category="malformed_object_payload",
            )
        jira_attr_id = jattr.get("objectTypeAttributeId")
        if jira_attr_id is None:
            continue
        ota_id, defining_ot_id = store.find_attribute_by_jira(jira_attr_id)
        if ota_id is None:
            continue
        meta = await _load_ota_meta(session, ota_id, ota_meta_cache)
        if meta.get("is_system") and meta["name"] == "Name":
            # Name is a valueless Object.name binding, not an ObjectValue group.
            continue
        data_type = meta["data_type"]
        if data_type == "reference":
            # ObjectRefs are owned by plan 05, not by this value pass.
            continue
        direction, _origin = store.effective_attribute_direction(
            schema_id=schema_id,
            jira_type_id=jira_type_id,
            defining_object_type_id=defining_ot_id,
            jira_attribute_id=jira_attr_id,
        )
        if is_jira_metadata(meta["structure"]):
            direction = "pull"
            if strategy_state is not None:
                strategy_state["metadata_jira_ids"].add(str(jira_attr_id))
        if direction in ("push", "none"):
            continue

        raw_values = [
            value
            for value in (jattr.get("objectAttributeValues") or [])
            if isinstance(value, dict)
        ]
        normalized, incomplete = await _normalize_attribute_values(
            data_type, meta["structure"], raw_values, store, schema_id
        )
        await _merge_attribute_group(
            ctx,
            object_id=object_id,
            ota_id=ota_id,
            data_type=data_type,
            maximum_cardinality=meta["maximum_cardinality"],
            normalized=normalized,
            existing=existing_by_ota.get(ota_id, []),
            staged_baselines=staged_baselines,
            staged_conflicts=staged_conflicts,
            incomplete=incomplete,
            direction=direction,
            attribute_baseline=attribute_baselines.get(ota_id),
            pending=pending,
            staged_attribute_baselines=staged_attribute_baselines,
            staged_attribute_conflicts=staged_attribute_conflicts,
            staged_closures=staged_closures,
            strategy=source_strategy,
            strategy_state=strategy_state,
            jira_attr_id=jira_attr_id,
            structure=meta["structure"],
        )

    if strategy_state is not None and strategy_state["write_back"]:
        await _flush_write_back(ctx, session, strategy_state)


def _entry_has_pullable_attribute(store, entry, *, schema_id, jira_type_id) -> bool:
    """True when at least one attribute of this entry resolves to pull/both.

    Used only to decide whether a NOT-yet-created surrogate object is worth
    creating under a type whose own effective direction excludes pull (a
    ``Schema none`` ancestor with an explicitly pulled descendant attribute).
    """
    for jattr in entry.get("attributes") or []:
        if not isinstance(jattr, dict):
            continue
        jira_attr_id = jattr.get("objectTypeAttributeId")
        if jira_attr_id is None:
            continue
        ota_id, defining_ot_id = store.find_attribute_by_jira(jira_attr_id)
        if ota_id is None:
            continue
        direction, _origin = store.effective_attribute_direction(
            schema_id=schema_id,
            jira_type_id=jira_type_id,
            defining_object_type_id=defining_ot_id,
            jira_attribute_id=jira_attr_id,
        )
        if direction in ("pull", "both"):
            return True
    return False


async def _process_entry(
    ctx,
    session,
    store,
    *,
    entry,
    linked_objects,
    new_links,
    page_baselines,
    page_conflicts,
    page_attribute_baselines=None,
    page_attribute_conflicts=None,
    page_closures=None,
    fresh_links,
    ota_meta_cache,
    schema_id,
    jira_type_id,
    object_type_id,
    source_id,
    connection_id,
    detached_jira_ids=frozenset(),
    source_strategy=None,
    client=None,
    task_id=None,
):
    """Create/update one Jira object and import its values.

    Returns ``(object_id, created)`` or ``(None, False)`` when the entry carries
    no usable identity/label. A ``CoreDataError`` (a single recoverable entity
    fault) propagates to the caller's per-entry guard so the page can still
    complete and the id is retried later (D-04/D-07).

    ``fresh_links`` is the set of Jira ids linked earlier in THIS run (their link
    rows are staged for the page commit, not yet persisted). Such a binding is
    live by construction, so the stale-binding predicate — which fails closed on
    the not-yet-committed row — must not be re-run against it.
    """
    jira_object_id = entry.get("id")
    if jira_object_id is None:
        return None, False
    jira_key = str(jira_object_id)
    if jira_key in detached_jira_ids:
        # A detached object keeps its link but vetoes ALL of its writes, even
        # when a child attribute says pull (D-04, 293-07).
        return None, False
    label = _entry_label(entry)
    object_id = linked_objects.get(jira_key)
    created = False
    # A linked Name follows its own direction even when Jira only returns the
    # top-level label (no label attribute entry). An unlinked Name stays local
    # under the existing type-direction policy.
    name_direction = store.effective_type_direction(
        schema_id=schema_id, jira_type_id=jira_type_id
    )[0]
    name_cache_key = f"name:{object_type_id}"
    if name_cache_key not in ota_meta_cache:
        ota_meta_cache[name_cache_key] = (
            await session.execute(
                text("SELECT label_attribute_id FROM object_types WHERE id = CAST(:id AS uuid)"),
                {"id": str(object_type_id)},
            )
        ).scalar()
    name_id = ota_meta_cache[name_cache_key]
    for (owner_id, jira_attr_id), ota_id in store.attribute_by_jira.items():
        if ota_id == name_id:
            meta = await _load_ota_meta(session, ota_id, ota_meta_cache)
            if not (meta.get("is_system") and meta["name"] == "Name"):
                continue
            name_direction = store.effective_attribute_direction(
                schema_id=schema_id, jira_type_id=jira_type_id,
                defining_object_type_id=owner_id, jira_attribute_id=jira_attr_id,
            )[0]
            break
    name_allowed = name_direction in ("pull", "both")

    if object_id is None:
        if label is None:
            return None, False
        if not name_allowed and not _entry_has_pullable_attribute(
            store, entry, schema_id=schema_id, jira_type_id=jira_type_id
        ):
            # Nothing on this surrogate object is pull-enabled: do not create it.
            return None, False
        result = await create_object(
            ctx,
            ObjectCreate(
                schema_id=schema_id,
                object_type_id=object_type_id,
                name=label,
            ),
            idempotency_key=_object_idempotency_key(source_id, jira_key),
        )
        object_id = result.id
        linked_objects[jira_key] = object_id
        new_links.append((object_id, jira_key))
        fresh_links.add(jira_key)
        created = bool(result.created)
    elif label is not None and name_allowed:
        if (
            jira_key not in fresh_links
            and await is_stale_binding(session, connection_id, object_id)
        ):
            raise PluginTaskError(
                "The Jira object binding is stale for this Source."
            )
        await update_object(ctx, object_id, ObjectUpdate(name=label))

    await _import_object_values(
        ctx,
        session,
        store,
        entry=entry,
        object_id=object_id,
        jira_type_id=jira_type_id,
        schema_id=schema_id,
        connection_id=connection_id,
        ota_meta_cache=ota_meta_cache,
        staged_baselines=page_baselines,
        staged_conflicts=page_conflicts,
        staged_attribute_baselines=page_attribute_baselines,
        staged_attribute_conflicts=page_attribute_conflicts,
        staged_closures=page_closures,
        source_strategy=source_strategy,
        client=client,
        task_id=task_id,
    )
    return object_id, created


async def _discover_reference_types(
    ctx, session, store, client, *, schema_id, jira_schema_id, source_id
):
    """Create + link the Jira schema's reference types (schema-scoped or Source-global).

    A reference type that Jira reports without a schema (``objectSchemaId`` empty)
    is materialized once per Source (``schema_id=None``) and shared; a
    schema-bound one stays schema-bound. A schema-less Core ReferenceType is
    unique by NAME across all Sources (``uq_reference_types_reference_null_schema``),
    so one that already exists — created by any Source — is adopted by that global
    name instead of inserted; the identity of a schema-less reference type is the
    name, while a schema-bound one keeps its per-Source, per-schema identity.
    """
    jira_refs = await client.get_reference_types(jira_schema_id)
    for jrt in jira_refs:
        if not isinstance(jrt, dict) or jrt.get("id") is None:
            raise PluginTaskError("The Jira reference type response is malformed.")
        jira_ref_id = str(jrt["id"])
        jira_scope = None if jrt.get("objectSchemaId") in (None, 0, "0") else schema_id
        if store.find_reference_type(jira_scope, jira_ref_id) is not None:
            continue
        if jira_scope is None and store.find_reference_type(schema_id, jira_ref_id):
            continue
        reference_name = (jrt.get("name") or "Unknown")[:100]
        if jira_scope is None:
            adopted = await store.find_global_reference_type(reference_name)
            if adopted is not None:
                store.adopt_global_reference_type(
                    reference_type_id=adopted,
                    jira_reference_type_id=jira_ref_id,
                )
                continue
        try:
            created = await create_reference_type(
                ctx,
                ReferenceTypeCreate(
                    reference=reference_name,
                    description=(jrt.get("description") or None),
                    color=(jrt.get("color") or None),
                    source_id=source_id,
                    schema_id=jira_scope,
                ),
                idempotency_key=_reference_type_idempotency_key(
                    source_id, jira_scope, jira_ref_id
                ),
            )
        except CoreDataError as exc:
            raise PluginTaskError(exc.message) from None
        await store.link_reference_type(
            reference_type_id=created.id,
            schema_id=jira_scope,
            jira_reference_type_id=jira_ref_id,
        )


async def _discover_object_types(
    ctx, session, store, client, *, schema_id, jira_schema_id, source_id
):
    """Create + link every ObjectType, parent before child (D-12)."""
    jira_types = await client.get_object_types(jira_schema_id)
    ordered = _order_types_parent_first(jira_types)
    parent_inherits: dict[str, bool] = defaultdict(bool)
    for jt in jira_types:
        if not isinstance(jt, dict) or jt.get("id") is None:
            raise PluginTaskError("The Jira ObjectType response is malformed.")
        parent = _parent_jira_id(jt)
        if parent and jt.get("parentObjectTypeInherited"):
            parent_inherits[parent] = True

    created_types: dict[str, uuid.UUID] = {}
    for jt in ordered:
        jira_type_id = str(jt["id"])
        existing = store.find_type(schema_id, jira_type_id)
        if existing is not None:
            created_types[jira_type_id] = existing
            continue
        parent_jira = _parent_jira_id(jt)
        parent_id = None
        if parent_jira:
            parent_id = created_types.get(parent_jira) or store.find_type(
                schema_id, parent_jira
            )
            if parent_id is None:
                raise PluginTaskError(
                    "A Jira ObjectType parent is not linked to this Source."
                )
        try:
            created = await create_object_type(
                ctx,
                ObjectTypeCreate(
                    schema_id=schema_id,
                    parent_id=parent_id,
                    inherits_attributes=bool(parent_inherits.get(jira_type_id, False)),
                    is_abstract=bool(jt.get("abstractObjectType", False)),
                    name=(jt.get("name") or "Unknown")[:255],
                    sort=int(jt.get("position", 0) or 0),
                    icon_id=await _resolve_icon_id(session, schema_id, jt.get("icon")),
                ),
                idempotency_key=_object_type_idempotency_key(
                    source_id, schema_id, jira_type_id
                ),
            )
        except CoreDataError as exc:
            raise PluginTaskError(exc.message) from None
        created_types[jira_type_id] = created.id
        await store.link_object_type(
            object_type_id=created.id,
            schema_id=schema_id,
            jira_type_id=jira_type_id,
        )
    return ordered, created_types


async def _discover_type_attributes(
    ctx,
    session,
    store,
    client,
    *,
    schema_id,
    source_id,
    ordered_types,
    created_types,
):
    """Create + link own and inherited attributes to their defining Core ObjectType.

    An attribute Jira reports as owned by an ancestor is bound to that ancestor's
    Core ObjectType (the defining owner), never duplicated onto the child
    (T-293-03-01); an unverifiable owner falls back to the child so no attribute
    is silently dropped.
    """
    attributes_by_type = {
        str(jt["id"]): await client.get_object_type_attributes(str(jt["id"]))
        for jt in ordered_types
    }
    types_by_jira = {str(jt["id"]): jt for jt in ordered_types}
    for jt in ordered_types:
        jira_type_id = str(jt["id"])
        object_type_id = created_types[jira_type_id]
        jira_attrs = attributes_by_type[jira_type_id]
        label_jira_id = None
        for ja in jira_attrs:
            if isinstance(ja, dict) and ja.get("label"):
                label_jira_id = str(ja.get("id"))
                break
        for ja in jira_attrs:
            if not isinstance(ja, dict) or ja.get("id") is None:
                raise PluginTaskError("The Jira attribute response is malformed.")
            jira_attr_id = str(ja["id"])
            attr_name = ja.get("name") or "Unknown"
            if jira_attr_id == label_jira_id:
                await _link_label_name(
                    session, store, object_type_id=object_type_id,
                    jira_attr_id=jira_attr_id,
                )
                continue
            if attr_name == "Name":
                continue

            defining_object_type_id = object_type_id
            owner_jira_id = _attribute_owner_jira_id(ja)
            if owner_jira_id and owner_jira_id != jira_type_id:
                resolved_owner = created_types.get(owner_jira_id) or store.find_type(
                    schema_id, owner_jira_id
                )
                if resolved_owner is not None:
                    defining_object_type_id = resolved_owner

            existing_attribute_id = store.find_attribute(
                defining_object_type_id, jira_attr_id
            )
            metadata = JIRA_METADATA.get(attr_name)
            data_type = metadata[1] if metadata else _attribute_data_type(ja)
            if metadata:
                # Jira system fields belong to the topmost inheriting type even
                # when Jira omits objectType on the attribute descriptor.
                cursor = jt
                seen = set()
                while cursor.get("parentObjectTypeInherited"):
                    parent = _parent_jira_id(cursor)
                    if parent in seen or parent not in types_by_jira:
                        break
                    seen.add(parent)
                    cursor = types_by_jira[parent]
                if cursor is not jt:
                    defining_object_type_id = created_types[str(cursor["id"])]
                existing_attribute_id = store.find_attribute(
                    defining_object_type_id, jira_attr_id
                )
                display_name = metadata[0]
                # Inspect the whole remote list before creating anything: its
                # ordering must not decide who wins a genuine-name collision.
                collision = any(
                    str(other.get("name", "")).casefold() == display_name.casefold()
                    for other in jira_attrs if isinstance(other, dict)
                )
                local_collision = (
                    await session.execute(
                        text("SELECT id, structure FROM object_type_attributes "
                             "WHERE object_type_id = CAST(:ot AS uuid) "
                             "AND lower(name) = lower(:name) "
                             "AND (CAST(:existing AS uuid) IS NULL OR id != CAST(:existing AS uuid))"),
                        {"ot": str(defining_object_type_id), "name": display_name,
                         "existing": str(existing_attribute_id) if existing_attribute_id else None},
                    )
                ).mappings().first()
                if (not collision and local_collision is not None
                        and is_jira_metadata(local_collision["structure"])
                        and defining_object_type_id != object_type_id):
                    # Type-local IDs for an inherited system field are aliases,
                    # not permission to duplicate the defining owner's Core row.
                    store.attribute_by_jira[(defining_object_type_id, jira_attr_id)] = local_collision["id"]
                    store.attribute_direction[(defining_object_type_id, jira_attr_id)] = "pull"
                    continue
                if (not collision and local_collision is not None
                        and local_collision["structure"] == {METADATA_MARKER: ja["name"]}):
                    # A crash can leave the keyed Core create committed before
                    # its link. Let the SDK prove/replay that same keyed ID;
                    # never adopt a row by display name alone.
                    local_collision = None
                if collision or local_collision is not None:
                    logger.warning(
                        "Jira metadata collision skipped: field=%s type_id=%s attribute_id=%s",
                        attr_name, jira_type_id, jira_attr_id,
                    )
                    continue
                attr_name = display_name
            if existing_attribute_id is not None:
                if metadata:
                    await _force_metadata_pull(session, store, existing_attribute_id,
                                               defining_object_type_id, jira_attr_id)
                if data_type == "status":
                    meta = await _load_ota_meta(session, existing_attribute_id, {})
                    structure = dict(meta["structure"] or {})
                    expected = await _build_attribute_structure(
                        session, schema_id=schema_id, data_type=data_type,
                        jira_attr=ja, referenced_object_type_id=None,
                    )
                    options = (expected or {}).get("status_options", [])
                    if structure.get("status_options") != options:
                        structure["status_options"] = options
                        try:
                            await update_object_type_attribute(
                                ctx, existing_attribute_id,
                                ObjectTypeAttributeUpdate(structure=structure),
                            )
                        except CoreDataError as exc:
                            raise PluginTaskError(exc.message) from None
                continue

            attribute_type_id = await _resolve_attribute_type_id(session, data_type)
            referenced_object_type_id = None
            if data_type == "reference":
                ref_jira = ja.get("referenceObjectTypeId") or (
                    ja.get("referenceObjectType") or {}
                ).get("id")
                if ref_jira is not None:
                    referenced_object_type_id = created_types.get(
                        str(ref_jira)
                    ) or store.find_type(schema_id, str(ref_jira))
            structure = await _build_attribute_structure(
                session,
                schema_id=schema_id,
                data_type=data_type,
                jira_attr=ja,
                referenced_object_type_id=referenced_object_type_id,
            )
            if metadata:
                structure = {METADATA_MARKER: ja["name"]}
            sort = int(ja.get("position", 0) or 0)
            if metadata:
                last_sort = (
                    await session.execute(
                        text("SELECT coalesce(max(sort), 0) FROM object_type_attributes "
                             "WHERE object_type_id = CAST(:ot AS uuid) AND is_system = false"),
                        {"ot": str(defining_object_type_id)},
                    )
                ).scalar()
                sort = max(last_sort, *(int(a.get("position", 0) or 0) for a in jira_attrs)) + 1
            try:
                created = await create_object_type_attribute(
                    ctx,
                    ObjectTypeAttributeCreate(
                        object_type_id=defining_object_type_id,
                        name=attr_name[:255],
                        sort=sort,
                        attribute_type_id=attribute_type_id,
                        minimum_cardinality=int(ja.get("minimumCardinality", 0) or 0),
                        maximum_cardinality=1 if metadata else int(ja.get("maximumCardinality", -1) or -1),
                        is_unique=bool(ja.get("uniqueAttribute", False)),
                        is_hidden=bool(
                            ja.get("hiddenAttribute", ja.get("hidden", False))
                        ),
                        structure=structure,
                        referenced_object_type_id=referenced_object_type_id,
                    ),
                    idempotency_key=_attribute_idempotency_key(
                        source_id, defining_object_type_id, jira_attr_id
                    ),
                )
            except CoreDataError as exc:
                if metadata and isinstance(exc, CoreDataConflict):
                    logger.warning(
                        "Jira metadata collision skipped: field=%s type_id=%s attribute_id=%s",
                        ja["name"], jira_type_id, jira_attr_id,
                    )
                    continue
                raise PluginTaskError(exc.message) from None
            await store.link_attribute(
                attribute_id=created.id,
                object_type_id=defining_object_type_id,
                jira_attribute_id=jira_attr_id,
            )
            if metadata:
                await _force_metadata_pull(session, store, created.id,
                                           defining_object_type_id, jira_attr_id)

        # Label attribute mapping: point the Core ObjectType at the (possibly
        # ancestor-owned) OTA for Jira's label attribute.
        if label_jira_id:
            label_attribute_id = _resolve_label_attribute(
                store, jira_type_id, label_jira_id, ordered_types, created_types
            )
            if label_attribute_id is not None:
                try:
                    await update_object_type(
                        ctx,
                        object_type_id,
                        ObjectTypeUpdate(label_attribute_id=label_attribute_id),
                    )
                except CoreDataError as exc:
                    raise PluginTaskError(exc.message) from None


async def _link_label_name(session, store, *, object_type_id, jira_attr_id):
    """Bind the actual Jira label to the nearest effective native system Name."""
    row = (
        await session.execute(
            text("""
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_id, inherits_attributes, 0 AS depth
                    FROM object_types WHERE id = CAST(:ot AS uuid)
                    UNION ALL
                    SELECT parent.id, parent.parent_id, parent.inherits_attributes, a.depth + 1
                    FROM ancestors a JOIN object_types parent ON parent.id = a.parent_id
                    WHERE parent.inherits_attributes = true AND a.depth < 100
                )
                SELECT ota.id, ota.object_type_id FROM ancestors a
                JOIN object_type_attributes ota ON ota.object_type_id = a.id
                WHERE ota.is_system = true AND ota.name = 'Name'
                ORDER BY a.depth LIMIT 1
            """),
            {"ot": str(object_type_id)},
        )
    ).mappings().first()
    if row is None:
        raise PluginTaskError("The Core system Name binding is unavailable.")
    old_id, old_owner = store.find_attribute_by_jira(jira_attr_id)
    prior_direction = store.attribute_direction.get((old_owner, str(jira_attr_id)))
    if old_id is not None and old_id != row["id"]:
        # Heal the former non-Name label link without deleting its Core data.
        await session.execute(
            text("DELETE FROM plugin_jira_attribute_links "
                 "WHERE object_type_attribute_id = CAST(:id AS uuid)"),
            {"id": str(old_id)},
        )
        store.attribute_by_jira.pop((old_owner, str(jira_attr_id)), None)
    await store.link_attribute(attribute_id=row["id"], object_type_id=row["object_type_id"],
                               jira_attribute_id=jira_attr_id)
    if row["object_type_id"] == object_type_id:
        # An actual label change must replace the old link, not merely populate
        # an in-memory alias while ON CONFLICT retains yesterday's Jira ID.
        await session.execute(
            text("UPDATE plugin_jira_attribute_links SET jira_type_attribute_id = :jira_id, "
                 "sync_direction = coalesce(:direction, sync_direction) "
                 "WHERE object_type_attribute_id = CAST(:id AS uuid) "
                 "AND source_id = CAST(:source AS uuid)"),
            {"id": str(row["id"]), "source": str(store.source_id),
             "jira_id": str(jira_attr_id), "direction": prior_direction},
        )
        for key, linked_id in list(store.attribute_by_jira.items()):
            if linked_id == row["id"] and key[1] != str(jira_attr_id):
                store.attribute_by_jira.pop(key)
                store.attribute_direction.pop(key, None)
    if prior_direction is not None:
        store.attribute_direction[(row["object_type_id"], str(jira_attr_id))] = prior_direction


async def _force_metadata_pull(session, store, attribute_id, object_type_id, jira_attr_id):
    await session.execute(
        text("UPDATE plugin_jira_attribute_links SET sync_direction = 'pull' "
             "WHERE object_type_attribute_id = CAST(:id AS uuid)"),
        {"id": str(attribute_id)},
    )
    store.attribute_direction[(object_type_id, str(jira_attr_id))] = "pull"


def _resolve_label_attribute(
    store, jira_type_id, label_jira_id, ordered_types, created_types
) -> uuid.UUID | None:
    """Find the OTA for Jira's label attribute on this type or an ancestor."""
    by_jira = {str(jt["id"]): jt for jt in ordered_types}
    seen: set[str] = set()
    cursor = by_jira.get(str(jira_type_id))
    while cursor is not None:
        jira_id = str(cursor["id"])
        if jira_id in seen:
            break
        seen.add(jira_id)
        core_id = created_types.get(jira_id)
        if core_id is not None:
            found = store.find_attribute(core_id, label_jira_id)
            if found is not None:
                return found
        parent = _parent_jira_id(cursor)
        cursor = by_jira.get(parent) if parent else None
    return None


async def _discover_statuses(
    ctx, session, store, client, *, schema_id, jira_schema_id, source_id, global_statuses
):
    """Create + link the schema's statuses, schema-specific wins over global.

    The Jira-global set is fetched once per Source run and materialized
    per-schema here: the Core schema id is part of every key, so two schemas that
    share one global Jira status id get two distinct stable Core rows/links.
    """
    schema_statuses = await client.get_status_types(jira_schema_id)
    merged: dict[str, dict] = {}
    for js in global_statuses:
        if isinstance(js, dict) and js.get("id") is not None:
            merged[str(js["id"])] = js
    for js in schema_statuses:
        if not isinstance(js, dict) or js.get("id") is None:
            raise PluginTaskError("The Jira status type response is malformed.")
        merged[str(js["id"])] = js

    for jira_status_id, js in merged.items():
        if store.find_status(schema_id, jira_status_id) is not None:
            continue
        try:
            created = await create_status(
                ctx,
                StatusCreate(
                    schema_id=schema_id,
                    name=(js.get("name") or "Unknown")[:100],
                    color=_status_color(js),
                    order=int(js.get("position", 0) or 0),
                    status_category=_status_category(js.get("category")),
                ),
                idempotency_key=_status_idempotency_key(
                    source_id, schema_id, jira_status_id
                ),
            )
        except CoreDataError as exc:
            raise PluginTaskError(exc.message) from None
        await store.link_status(
            status_id=created.id,
            schema_id=schema_id,
            jira_status_id=jira_status_id,
        )


async def _discover_metadata(
    ctx, session, store, client, *, schema_id, jira_schema_id, source_id
):
    """Discover statuses before attributes so Status structures are usable."""
    global_statuses = await client.get_all_status_types()
    await _discover_statuses(
        ctx, session, store, client, schema_id=schema_id,
        jira_schema_id=jira_schema_id, source_id=source_id,
        global_statuses=global_statuses,
    )
    await _discover_reference_types(
        ctx,
        session,
        store,
        client,
        schema_id=schema_id,
        jira_schema_id=jira_schema_id,
        source_id=source_id,
    )
    ordered_types, created_types = await _discover_object_types(
        ctx,
        session,
        store,
        client,
        schema_id=schema_id,
        jira_schema_id=jira_schema_id,
        source_id=source_id,
    )
    await _discover_type_attributes(
        ctx,
        session,
        store,
        client,
        schema_id=schema_id,
        source_id=source_id,
        ordered_types=ordered_types,
        created_types=created_types,
    )
    # One plugin transaction for the metadata links; the live binding is
    # re-checked immediately before it commits.
    await store.commit()


# --- Reference reconciliation (293-05, JSYNC-01/JSYNC-13) ---------------------


def _reference_detail_attributes(detail):
    """The detail's attribute list, or ``None`` when the detail is malformed.

    A missing ``attributes`` key is a detail with no references; a non-list
    ``attributes`` value is malformed and must never decode as "no refs" (D-05).
    """
    if not isinstance(detail, dict):
        return None
    attributes = detail.get("attributes")
    if attributes is None:
        return []
    if not isinstance(attributes, list):
        return None
    return attributes


async def _load_object_refs(session, source_id, from_object_id, attribute_name):
    """The plugin Source's current ref rows for one (from-object, attribute) group."""
    rows = (
        await session.execute(
            _OBJECT_REFS_SQL,
            {
                "source_id": str(source_id),
                "object_id": str(from_object_id),
                "attribute_name": attribute_name,
            },
        )
    ).mappings().all()
    return [{"id": row["id"], "to_object_id": row["to_object_id"]} for row in rows]


async def _desired_reference_groups(
    store, session, *, detail, schema_id, jira_type_id, targets, ota_meta_cache
):
    """Validate every reference group of one object BEFORE any mutation.

    Returns ``(groups, blocked)``; each group is ``(attribute_name, ota_id,
    direction, [(target jira id, target Core id), ...], jira_attr_id)`` preserving
    remote multiplicity. ``blocked`` is ``True`` when the detail is malformed or any
    referenced target is unmatched, detached or from a foreign Source — the caller
    then preserves the whole prior group set and retries later instead of deleting
    (D-05, T-293-05-01).
    """
    attributes = _reference_detail_attributes(detail)
    if attributes is None:
        return None, True
    groups: list[tuple] = []
    for jattr in attributes:
        if not isinstance(jattr, dict):
            return None, True
        jira_attr_id = jattr.get("objectTypeAttributeId")
        if jira_attr_id is None:
            continue
        ota_id, defining_ot_id = store.find_attribute_by_jira(jira_attr_id)
        if ota_id is None:
            continue
        meta = await _load_ota_meta(session, ota_id, ota_meta_cache)
        if meta["data_type"] != "reference":
            continue
        direction, _origin = store.effective_attribute_direction(
            schema_id=schema_id,
            jira_type_id=jira_type_id,
            defining_object_type_id=defining_ot_id,
            jira_attribute_id=jira_attr_id,
        )
        # JSYNC-13/F-05: a push-only or disabled reference is not pulled.
        if direction in ("push", "none"):
            continue
        attribute_name = meta.get("name")
        if not attribute_name:
            continue
        raw_values = jattr.get("objectAttributeValues") or []
        if not isinstance(raw_values, list):
            return None, True
        desired: list = []
        for value in raw_values:
            if not isinstance(value, dict):
                return None, True
            referenced = value.get("referencedObject")
            if not isinstance(referenced, dict) or referenced.get("id") is None:
                return None, True
            jira_target_id = str(referenced["id"])
            target = targets.get(jira_target_id)
            if target is None:
                # Unmatched / detached / foreign-Source: never a cross-Source ref.
                return None, True
            desired.append((jira_target_id, target))
        groups.append(
            (attribute_name, ota_id, direction, desired, str(jira_attr_id))
        )
    return groups, False


async def _apply_reference_group(
    ctx,
    session,
    *,
    source_id,
    object_type_id,
    from_object_id,
    attribute_name,
    ota_id,
    desired,
    direction,
    jira_by_core,
    attribute_baseline=None,
    pending=None,
    staged_attribute_baselines=None,
    staged_attribute_conflicts=None,
    staged_closures=None,
    strategy=None,
    strategy_state=None,
    jira_attr_id=None,
):
    """Merge ONE reference group: three-way at ``both``, forced Jira at ``pull``.

    Each side is the canonical set of linked target Jira ids: a local ref maps
    through ``jira_by_core``; an unlinked Core target counts as its Core id, so it
    can never equal a Jira id. ``desired`` is the remote ``(jira id, Core id)``
    list preserving multiplicity. Plain SDK create/delete is enough for replay
    safety: a replayed create is already visible in the local group, so the
    multiset difference is empty (no D-12 key needed).
    """
    if staged_attribute_baselines is None:
        staged_attribute_baselines = []
    if staged_attribute_conflicts is None:
        staged_attribute_conflicts = []
    if staged_closures is None:
        staged_closures = []
    local = await _load_object_refs(
        session, source_id, from_object_id, attribute_name
    )
    local_pairs = [
        (
            jira_by_core.get(row["to_object_id"], str(row["to_object_id"])),
            row["to_object_id"],
        )
        for row in local
    ]
    local_jira = [jira for jira, _core in local_pairs]
    remote_jira = [jira for jira, _core in desired]
    local_set = canonical_set(local_jira) if local else None
    remote_set = canonical_set(remote_jira) if desired else None

    def _reference_items(pairs):
        return [
            {"object_id": core, "jira_object_id": jira} for jira, core in pairs
        ]

    pending_row = pending
    if (
        pending_row is not None
        and remote_set is not None
        and local_set == remote_set
    ):
        # A7: the two sides converged while the conflict was pending.
        staged_attribute_baselines.append((from_object_id, ota_id, remote_set))
        _stage_closure(staged_closures, pending_row, "converged")
        return 0, 0

    if direction == "pull" or attribute_baseline is None:
        # Jira is authoritative (pull), or there is no baseline to merge against
        # (293 behavior: Jira wins and the baseline is recorded below).
        decision = APPLY
    else:
        decision = _pull_decision(
            direction,
            merge_value(local_set, remote_set, attribute_baseline),
            local_empty=not local,
        )
    if decision == KEEP_LOCAL:
        return 0, 0
    if decision == NO_OP:
        return 0, 0
    if decision == CONFLICT:
        conflict = {
            "object_id": from_object_id,
            "object_value_id": None,
            "object_type_attribute_id": ota_id,
            "data_type": "reference",
            "kind": "reference",
            "cardinality": "multi",
            "local_items": _reference_items(local_pairs),
            "remote_items": _reference_items(desired),
        }
        # D-05/D-10: the Source default applies only to a NEW conflict. A
        # prefer_local write-back can only send the local targets that carry a
        # Jira id; the baseline records exactly what Jira ends up holding.
        write_ids = [
            jira for jira, core in local_pairs if jira_by_core.get(core) is not None
        ]
        if pending_row is None and strategy == "prefer_local" and strategy_state is not None:
            _collect_write_back(
                strategy_state,
                conflict=conflict,
                ota_id=ota_id,
                jira_attr_id=jira_attr_id,
                structure=None,
                data_type="reference",
                local_items=write_ids,
                cardinality="multi",
                baseline_set=canonical_set(write_ids),
            )
            staged_attribute_conflicts.append(conflict)
            return 0, 0
        if pending_row is None and strategy == "prefer_remote":
            # Jira wins: fall through to the APPLY/CONVERGE block below, which
            # converges the Core refs to ``desired``, and mark the row resolved.
            conflict["auto_resolve"] = True
            staged_attribute_conflicts.append(conflict)
        else:
            staged_attribute_conflicts.append(conflict)
            return 0, 0

    # APPLY / CONVERGE: converge the Core refs to Jira's targets.
    remaining = list(local)
    created = 0
    deleted = 0
    for _jira, target_id in desired:
        match = next(
            (row for row in remaining if row["to_object_id"] == target_id), None
        )
        if match is not None:
            remaining.remove(match)
            continue
        await create_object_ref(
            ctx,
            ObjectRefCreate(
                from_object_id=from_object_id,
                to_object_id=target_id,
                object_type_id=object_type_id,
                source_id=source_id,
                from_attribute_name=attribute_name,
            ),
        )
        created += 1
    for row in remaining:
        await delete_object_ref(ctx, row["id"])
        deleted += 1
    if remote_set is not None:
        staged_attribute_baselines.append((from_object_id, ota_id, remote_set))
    if pending_row is not None:
        _stage_closure(
            staged_closures,
            pending_row,
            "converged" if decision == CONVERGE else "direction pull",
        )
    return created, deleted


async def reconcile_references(
    ctx,
    session,
    store,
    client,
    *,
    source_id,
    connection_id,
    selected_types,
    task_id=None,
    source_strategy=None,
):
    """Resolve Jira references into Core ObjectRefs after the object pass (293-05).

    The from-object scope is the selected types only; targets resolve through the
    Source-wide target map, so a target linked in another schema of the SAME
    Source is found while a foreign Source, a detached Object, a missing id or a
    malformed detail can never create or erase a ref (D-02, T-293-05-01). Remote
    multiplicity is preserved, and a replay after a Core ref commit makes no
    extra ref.

    At ``both`` each group merges three-way against its attribute baseline; at
    ``pull`` Jira's targets are forced; a group with no baseline follows 293
    (Jira wins). Attribute baselines, reference conflicts and conflict closures
    are staged in one plugin transaction after the SDK ref writes.
    """
    await store.assert_live_binding()
    targets = await load_source_object_links(
        session, source_id=source_id, connection_id=connection_id
    )
    jira_by_core = {
        core_id: jira_id for jira_id, core_id in targets.items()
    }
    ota_meta_cache: dict = {}
    created = 0
    deleted = 0
    deferred = 0
    deferred_types: set[str] = set()
    staged_attribute_baselines: list[tuple] = []
    staged_attribute_conflicts: list[dict] = []
    staged_closures: list[dict] = []
    for schema_id, object_type_id, jira_type_id in selected_types:
        from_rows = await load_type_object_links(
            session,
            source_id=source_id,
            connection_id=connection_id,
            object_type_id=object_type_id,
        )
        for from_object_id, jira_object_id in from_rows:
            try:
                detail = await client.get_object(str(jira_object_id))
            except JiraPullAuthError:
                raise
            except JiraPullApiError as exc:
                if exc.status_code == 404:
                    # Gone in Jira: the stale sweep owns a 404, not the ref pass.
                    continue
                deferred += 1
                deferred_types.add(str(jira_type_id))
                await record_retry_ids(
                    session,
                    source_id=source_id,
                    object_type_id=object_type_id,
                    jira_ids=[jira_object_id],
                )
                continue
            groups, blocked = await _desired_reference_groups(
                store,
                session,
                detail=detail,
                schema_id=schema_id,
                jira_type_id=jira_type_id,
                targets=targets,
                ota_meta_cache=ota_meta_cache,
            )
            if blocked:
                deferred += 1
                deferred_types.add(str(jira_type_id))
                await record_retry_ids(
                    session,
                    source_id=source_id,
                    object_type_id=object_type_id,
                    jira_ids=[jira_object_id],
                )
                continue
            pending = await _load_pending_for_object(session, from_object_id)
            attribute_baselines = await load_attribute_baselines(
                session, from_object_id
            )
            # D-05/D-10: the Source default strategy also governs reference
            # conflicts this run newly detects; prefer_local collects ONE
            # write-back PUT for this object, flushed after its groups.
            strategy_state = None
            if client is not None and source_strategy in (
                "prefer_local",
                "prefer_remote",
            ):
                strategy_state = {
                    "write_back": [],
                    "client": client,
                    "entry": detail,
                    "jira_type_id": jira_type_id,
                    "jira_object_id": jira_object_id,
                    "object_id": from_object_id,
                    "connection_id": connection_id,
                    "task_id": task_id,
                    "staged_baselines": [],
                    "staged_attribute_baselines": staged_attribute_baselines,
                }
            for attribute_name, ota_id, direction, desired, jira_attr_id in groups:
                group_created, group_deleted = await _apply_reference_group(
                    ctx,
                    session,
                    source_id=source_id,
                    object_type_id=object_type_id,
                    from_object_id=from_object_id,
                    attribute_name=attribute_name,
                    ota_id=ota_id,
                    desired=desired,
                    direction=direction,
                    jira_by_core=jira_by_core,
                    attribute_baseline=attribute_baselines.get(ota_id),
                    pending=(pending or {}).get(ota_id),
                    staged_attribute_baselines=staged_attribute_baselines,
                    staged_attribute_conflicts=staged_attribute_conflicts,
                    staged_closures=staged_closures,
                    strategy=source_strategy,
                    strategy_state=strategy_state,
                    jira_attr_id=jira_attr_id,
                )
                created += group_created
                deleted += group_deleted
            if strategy_state is not None and strategy_state["write_back"]:
                await _flush_write_back(ctx, session, strategy_state)
    # The reference pass runs outside any page: commit its baselines, conflicts
    # and closures in one plugin transaction after the SDK ref writes.
    await commit_attribute_effects(
        session,
        connection_id=connection_id,
        task_id=task_id,
        attribute_baselines=staged_attribute_baselines,
        attribute_conflicts=staged_attribute_conflicts,
        conflict_closures=staged_closures,
    )
    await session.commit()
    return {
        "created": created,
        "deleted": deleted,
        "deferred": deferred,
        "deferred_types": deferred_types,
    }


async def cleanup_missing_objects(
    ctx,
    session,
    store,
    client,
    *,
    source_id,
    connection_id,
    revision,
    selected_types,
    type_retry_ids,
    deferred_types=frozenset(),
):
    """Delete only proven-stale, Jira-owned, attached Objects (293-05, D-04/D-07).

    Deletion authority is the complete unfiltered Jira id inventory of one type.
    A type whose inventory is unavailable or forbidden, or that had any isolated
    unverified entry this run (object pass or reference pass), is skipped
    entirely; a native, detached, null-id or foreign-Source Object is never
    selected. Every deletion goes through
    ``indramesh.sdk.delete_object`` so Core owns the outbox, caches and hooks; a
    failing delete is surfaced, never counted as a success.
    """
    await store.assert_live_binding()
    deleted = 0
    swept_types: list[str] = []
    for schema_id, object_type_id, jira_type_id in selected_types:
        if type_retry_ids.get(str(jira_type_id)) or str(jira_type_id) in deferred_types:
            # An isolated item of this type stayed unverified this run.
            continue
        direction, _origin = store.effective_type_direction(
            schema_id=schema_id, jira_type_id=jira_type_id
        )
        if direction in ("push", "none"):
            # Not a pull-managed type: the sweep must never touch it.
            continue
        try:
            inventory = await client.get_object_type_object_ids(str(jira_type_id))
        except JiraPullAuthError as exc:
            if exc.status_code == 401:
                raise
            # 403 on this optional read declines the sweep for this type only.
            continue
        except JiraPullApiError:
            # Unavailable/partial inventory is not deletion authority (D-04).
            continue
        candidates = await load_stale_candidates(
            session,
            source_id=source_id,
            connection_id=connection_id,
            object_type_id=object_type_id,
            revision=revision,
        )
        for object_id, jira_object_id in candidates:
            if jira_object_id in inventory:
                continue
            try:
                await delete_object(ctx, object_id)
            except CoreDataError as exc:
                raise PluginTaskError(
                    "A stale Jira-owned Object could not be deleted: "
                    f"{getattr(exc, 'message', str(exc))}"
                ) from None
            deleted += 1
        swept_types.append(str(jira_type_id))
    return {"deleted": deleted, "types": swept_types}


async def _resolve_scopes(
    ctx,
    session,
    connection,
    source_id,
    selected_schema_id,
    jira_schema_id,
    client,
    target_schema_id=None,
):
    """The (Core schema id, Jira schema id) pairs this run imports."""
    scopes = []
    for schema_id, resolved_jira_schema_id in await _plan_scopes(
        session, connection, selected_schema_id, jira_schema_id, target_schema_id
    ):
        if schema_id is None:
            scopes.append(
                await _bootstrap_schema(
                    ctx,
                    session,
                    connection,
                    source_id,
                    resolved_jira_schema_id,
                    client,
                    target_schema_id,
                )
            )
        else:
            scopes.append((schema_id, resolved_jira_schema_id))
    return scopes


async def run_pull(ctx, task_id, **kwargs: Any) -> dict:
    """Execute one ``jira.sync-pull`` for the task's Source and selected schema."""
    task = ctx.task
    source_id = task.source_id
    if source_id is None:
        raise PluginTaskError("A Jira sync pull requires a Source scope.")

    selected_schema_id = task.schema_id
    jira_schema_id = kwargs.get("jira_schema_id")
    target_schema_id = kwargs.get("target_schema_id") or None
    include_comments_files = bool(kwargs.get("include_comments_files"))

    async with ctx.db() as session:
        connection = await _load_connection(session, source_id)
        if connection is None:
            raise PluginTaskError(
                "No Source-bound Jira connection exists for this Source."
            )
        _validate_source_binding(connection)

        account_id = task.service_account_id
        if account_id is None:
            raise PluginTaskError(
                "A Jira sync pull requires a bound service account."
            )
        await _validate_account_scope(session, account_id, source_id)

        async with source_run_lock(session, source_id) as acquired:
            if not acquired:
                raise PluginTaskError(
                    "Another Jira sync pull is already running for this Source."
                )

            credentials = await ctx.credentials()
            pat = credentials.source_bearer_token
            if not pat:
                raise PluginTaskError(
                    "The bound service account has no Jira token."
                )

            client = JiraPullClient(
                base_url=connection["core_source_url"],
                pat=pat,
                transport=_make_transport(),
            )
            try:
                scopes = await _resolve_scopes(
                    ctx,
                    session,
                    connection,
                    source_id,
                    selected_schema_id,
                    jira_schema_id,
                    client,
                    target_schema_id,
                )
                store = LinkStore(
                    session,
                    source_id=source_id,
                    connection_id=connection["connection_id"],
                    connection_revision=connection["connection_revision"],
                    core_source_url=connection["core_source_url"],
                    connection_url=connection["connection_base_url"],
                )
                await store.preload()

                # D-05: the Source connection's default strategy, read ONCE per
                # run, governs only the conflicts this run newly detects.
                source_strategy = connection.get("default_conflict_strategy")

                processed = 0
                created_count = 0
                # Isolated entries skipped this run (object-pass + media). Drives
                # the PARTIAL_SUCCESS outcome and the compact final result line
                # (D-06/D-11).
                skipped_count = 0
                updated: list[str] = []
                schema_ids: list[str] = []
                # D-08: markers/releases this run set, lifted or skipped, summed
                # over every processed schema.
                protection_totals = {
                    "set": 0,
                    "lifted": 0,
                    "skipped": 0,
                    "released": 0,
                    "unreleased": 0,
                }
                # Types actually covered by THIS pull (object pass + ref scope +
                # stale-sweep eligibility) and the isolated retry list per type.
                selected_types: list[tuple] = []
                type_retry_ids: dict[str, list[str]] = {}
                for schema_id, resolved_jira_schema_id in scopes:
                    schema_ids.append(str(schema_id))
                    # Selected-schema discovery only: never the list-all endpoint.
                    await client.get_schema(resolved_jira_schema_id)
                    # Metadata first: object/value imports need authoritative,
                    # Source-bound links to their Schema/Type/Attribute/Status.
                    await _discover_metadata(
                        ctx,
                        session,
                        store,
                        client,
                        schema_id=schema_id,
                        jira_schema_id=resolved_jira_schema_id,
                        source_id=source_id,
                    )
                    # D-08/D-11: right after discovery, because an attribute newly
                    # linked under an inherited pull direction must be protected in
                    # the same run and detached objects' releases brought in line.
                    protection = await reconcile_write_protection(
                        ctx, session, schema_id=schema_id
                    )
                    for _key in protection_totals:
                        protection_totals[_key] += protection.get(_key, 0)

                    linked_types = (
                        await session.execute(
                            _LINKED_TYPES_SQL,
                            {
                                "source_id": str(source_id),
                                "connection_id": str(connection["connection_id"]),
                                "schema_id": str(schema_id),
                            },
                        )
                    ).mappings().all()
                    linked_objects = {
                        str(row["jira_object_id"]): row["object_id"]
                        for row in (
                            await session.execute(
                                _LINKED_OBJECTS_SQL,
                                {
                                    "source_id": str(source_id),
                                    "connection_id": str(connection["connection_id"]),
                                },
                            )
                        ).mappings().all()
                    }
                    # Detached objects keep their link but are never written by
                    # this run, regardless of any explicit child direction.
                    detached_jira_ids = {
                        str(row["jira_object_id"])
                        for row in (
                            await session.execute(
                                _DETACHED_OBJECT_IDS_SQL,
                                {
                                    "source_id": str(source_id),
                                    "connection_id": str(connection["connection_id"]),
                                },
                            )
                        ).mappings().all()
                    }
                    # Per-run cache of linked-attribute metadata (data_type,
                    # structure, cardinality) shared across every object page.
                    ota_meta_cache: dict = {}

                    for type_row in linked_types:
                        object_type_id = type_row["object_type_id"]
                        jira_type_id = type_row["jira_type_id"]
                        object_type_name = type_row["object_type_name"]
                        # The stable incremental window for this type (D-02/D-03):
                        # an unfinished run keeps run_started_at/last_page; a
                        # completed one starts a new window. last_successful_at is
                        # the lower bound — it moves only on completion.
                        checkpoint = await load_or_begin_type_run(
                            session,
                            connection_id=connection["connection_id"],
                            source_id=source_id,
                            object_type_id=object_type_id,
                        )
                        lower_bound = checkpoint["last_successful_at"]
                        run_started_at = checkpoint["run_started_at"]
                        remaining_retry: list[str] = []

                        # Explicit retry of previously skipped entries, even when
                        # older than the lower bound (D-07). Their plugin-side
                        # effects (links/baselines/conflicts) are staged for the
                        # first page's transaction: a retried create must land its
                        # Object link and baselines exactly like any page entry,
                        # otherwise the retried object would stay unlinked.
                        retry_links: list[tuple] = []
                        retry_baselines: list[tuple] = []
                        retry_conflicts: list[dict] = []
                        retry_attribute_baselines: list[tuple] = []
                        retry_attribute_conflicts: list[dict] = []
                        retry_closures: list[dict] = []
                        fresh_links: set[str] = set()
                        for retry_id in checkpoint["retry_jira_ids"]:
                            try:
                                retry_entry = await client.get_object(str(retry_id))
                            except JiraPullApiError:
                                remaining_retry.append(str(retry_id))
                                continue
                            retry_type = str(
                                (retry_entry.get("objectType") or {}).get("id")
                            )
                            if retry_type != str(jira_type_id):
                                remaining_retry.append(str(retry_id))
                                continue
                            try:
                                await _process_entry(
                                    ctx,
                                    session,
                                    store,
                                    entry=retry_entry,
                                    linked_objects=linked_objects,
                                    new_links=retry_links,
                                    page_baselines=retry_baselines,
                                    page_conflicts=retry_conflicts,
                                    page_attribute_baselines=retry_attribute_baselines,
                                    page_attribute_conflicts=retry_attribute_conflicts,
                                    page_closures=retry_closures,
                                    fresh_links=fresh_links,
                                    ota_meta_cache=ota_meta_cache,
                                    schema_id=schema_id,
                                    jira_type_id=jira_type_id,
                                    object_type_id=object_type_id,
                                    source_id=source_id,
                                    connection_id=connection["connection_id"],
                                    detached_jira_ids=detached_jira_ids,
                                    source_strategy=source_strategy,
                                    client=client,
                                    task_id=task_id,
                                )
                            except (CoreDataError, JiraPullItemError) as exc:
                                # Isolated recoverable fault on the retry path: the
                                # audit row is committed independently, the id stays
                                # on the retry list and the run continues (D-06).
                                await _record_skipped_entry(
                                    session,
                                    connection_id=connection["connection_id"],
                                    task_id=str(task_id),
                                    entity_type="object",
                                    entity_id=str(retry_id),
                                    category=_skip_category(exc),
                                    fault=exc,
                                )
                                skipped_count += 1
                                remaining_retry.append(str(retry_id))

                        page_number = int(checkpoint["last_page"]) + 1
                        first_page = True
                        while True:
                            page = await client.get_objects_page(
                                jira_type_id,
                                page=page_number,
                                limit=_PULL_PAGE_LIMIT,
                                last_synced_at=lower_bound,
                            )
                            seen_on_page: list[str] = []
                            # The first page's plugin transaction also commits the
                            # retry pass's staged links, baselines and conflicts.
                            new_links: list[tuple] = retry_links if first_page else []
                            page_baselines: list[tuple] = (
                                retry_baselines if first_page else []
                            )
                            page_conflicts: list[dict] = (
                                retry_conflicts if first_page else []
                            )
                            page_attribute_baselines: list[tuple] = (
                                retry_attribute_baselines if first_page else []
                            )
                            page_attribute_conflicts: list[dict] = (
                                retry_attribute_conflicts if first_page else []
                            )
                            page_closures: list[dict] = (
                                retry_closures if first_page else []
                            )
                            first_page = False
                            for entry in page["values"]:
                                jira_object_id = entry.get("id")
                                if jira_object_id is None:
                                    continue
                                jira_key = str(jira_object_id)
                                seen_on_page.append(jira_key)
                                try:
                                    object_id, created = await _process_entry(
                                        ctx,
                                        session,
                                        store,
                                        entry=entry,
                                        linked_objects=linked_objects,
                                        new_links=new_links,
                                        page_baselines=page_baselines,
                                        page_conflicts=page_conflicts,
                                        page_attribute_baselines=page_attribute_baselines,
                                        page_attribute_conflicts=page_attribute_conflicts,
                                        page_closures=page_closures,
                                        fresh_links=fresh_links,
                                        ota_meta_cache=ota_meta_cache,
                                        schema_id=schema_id,
                                        jira_type_id=jira_type_id,
                                        object_type_id=object_type_id,
                                        source_id=source_id,
                                        connection_id=connection["connection_id"],
                                        detached_jira_ids=detached_jira_ids,
                                        source_strategy=source_strategy,
                                        client=client,
                                        task_id=task_id,
                                    )
                                except (CoreDataError, JiraPullItemError) as exc:
                                    # Isolated recoverable fault: the audit row is
                                    # committed independently, the page still
                                    # completes, the id is retried next run and the
                                    # terminal outcome becomes PARTIAL_SUCCESS
                                    # (D-04/D-06/D-07).
                                    await _record_skipped_entry(
                                        session,
                                        connection_id=connection["connection_id"],
                                        task_id=str(task_id),
                                        entity_type="object",
                                        entity_id=jira_key,
                                        category=_skip_category(exc),
                                        fault=exc,
                                    )
                                    skipped_count += 1
                                    remaining_retry.append(jira_key)
                                    continue
                                if object_id is None:
                                    continue
                                if created:
                                    created_count += 1
                                else:
                                    updated.append(str(object_id))
                                processed += 1
                                # A monotone processed count only; no whole-run
                                # total is validated, so no percent is emitted —
                                # the page step carries ``processed/–`` (D-08/D-09).
                                await ctx.report_progress(processed_objects=processed)

                            # D-03: one plugin transaction commits the page's links,
                            # baselines, conflicts, seen ids, retry list and
                            # last_page. Core SDK writes stay separate (D-01).
                            await commit_completed_page(
                                session,
                                connection_id=connection["connection_id"],
                                source_id=source_id,
                                object_type_id=object_type_id,
                                page=page_number,
                                seen_jira_ids=seen_on_page,
                                object_links=new_links,
                                value_baselines=page_baselines,
                                conflicts=page_conflicts,
                                attribute_baselines=page_attribute_baselines,
                                attribute_conflicts=page_attribute_conflicts,
                                conflict_closures=page_closures,
                                retry_jira_ids=remaining_retry,
                                run_started_at=run_started_at,
                                task_id=task_id,
                            )
                            # D-08/D-10: a free-form, bounded page step naming the
                            # ObjectType and page plus the monotone processed count
                            # (`processed/–`; no percent without a reliable
                            # whole-run total). Never a per-object step.
                            await ctx.report_step(
                                (
                                    f"{object_type_name} page {page_number}: "
                                    f"{processed}/–"
                                )[:255]
                            )
                            # Completion is decided by the Jira metadata, never by
                            # the locally filtered ``values`` size: an empty page
                            # with a next page does NOT end the type.
                            if page["hasNextPage"]:
                                page_number = int(page["nextPage"])
                                continue
                            await finish_type_run(
                                session,
                                source_id=source_id,
                                object_type_id=object_type_id,
                                run_started_at=run_started_at,
                            )
                            break
                        selected_types.append(
                            (schema_id, object_type_id, jira_type_id)
                        )
                        type_retry_ids[str(jira_type_id)] = list(remaining_retry)

                # 293-05: references resolve only after EVERY selected object row
                # has a Core/link identity, so a target linked in another schema
                # of the same Source is found.
                reference_result = await reconcile_references(
                    ctx,
                    session,
                    store,
                    client,
                    source_id=source_id,
                    connection_id=connection["connection_id"],
                    selected_types=selected_types,
                    task_id=task_id,
                    source_strategy=source_strategy,
                )
                # 293-05: the stale sweep runs last, only for types whose full
                # Jira id inventory was proven this run.
                cleanup_result = await cleanup_missing_objects(
                    ctx,
                    session,
                    store,
                    client,
                    source_id=source_id,
                    connection_id=connection["connection_id"],
                    revision=connection["connection_revision"],
                    selected_types=selected_types,
                    type_retry_ids=type_retry_ids,
                    deferred_types=reference_result["deferred_types"],
                )
                # 293-06: optional comment/attachment reconciliation. Off by
                # default — no comment/file GET is issued unless the task opted
                # in. The media pass runs last and never participates in the
                # stale sweep: a failed list or blob only audits and preserves
                # the previously imported Core media (D-05).
                media_result: dict = {
                    "objects": 0,
                    "comments": 0,
                    "attachments": 0,
                    "failed": 0,
                }
                if include_comments_files:
                    media_result = await reconcile_object_media(
                        ctx,
                        session,
                        client,
                        source_id=source_id,
                        connection_id=connection["connection_id"],
                    )
                # The terminal skip count spans the object pass and the optional
                # media pass; every skipped entry already has its own durable
                # audit row (D-06/D-11).
                skipped_total = skipped_count + int(media_result.get("failed", 0))
            except JiraPullAuthError as exc:
                # A rejected credential (401) or forbidden read (403) is fatal in
                # every mode: never recorded as a skipped item, never an override
                # to a success-looking outcome (D-04, T-293-08-01).
                raise PluginTaskError(str(exc)) from None
            except JiraPullApiError as exc:
                # A malformed/partial page envelope or systemic API failure aborts
                # the run; the Core wrapper persists FAILED (D-04).
                raise PluginTaskError(exc.message) from None
            finally:
                await client.aclose()

    status_label = "PARTIAL_SUCCESS" if skipped_total else "SUCCESS"
    # D-11: exactly one compact final status/count/skip line, before the outcome
    # is persisted — never item details.
    await ctx.report_step(
        (f"{status_label}: {processed} processed, {skipped_total} skipped")[:255]
    )
    # Force the exact terminal monotone pair so the persisted counts agree with
    # the final line (never above the persisted value).
    await ctx.report_progress(processed_objects=processed, force=True)
    ctx.set_outcome(
        TaskStatus.PARTIAL_SUCCESS if skipped_total else TaskStatus.SUCCESS
    )
    return {
        "task_id": str(task_id),
        "tasktype": "jira.sync-pull",
        "source_id": str(source_id),
        "schema_ids": schema_ids,
        "status": status_label,
        "processed": processed,
        "skipped": skipped_total,
        "created": created_count,
        "updated": updated,
        "references": reference_result,
        "deleted": cleanup_result["deleted"],
        "media": media_result,
        "include_comments_files": include_comments_files,
        "write_protection": protection_totals,
    }
