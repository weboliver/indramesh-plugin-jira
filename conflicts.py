"""Versioned conflict snapshots and the D-02/D-03 pending-conflict upsert.

A conflict snapshot is a versioned JSON envelope holding the COMPLETE local and
Jira value sets of one anchor, so two equal sets serialize byte-identically and
equality is string equality (D-02). ``decode_snapshot`` rejects anything that is
not envelope version 1.

``upsert_pending`` is the single write path for every engine (pull now, push and
resolution later). It names its partial unique index by elements plus the
verbatim predicate, never by constraint name (294-RESEARCH bug 1 — these are
indexes, not constraints), and implements D-03 / D-03 (Nachtrag): a re-observed
divergence updates the
existing pending row's snapshots and task attribution, and raises ``version`` by
exactly 1 only when the stored local/remote pair actually changes. An identical
re-observation refreshes only ``task_id``/``updated_at``, so a scheduled sync
never invalidates a running manual resolution with a stale version. The first
detector keeps the row's ``direction`` and ``connection_id``.

The module imports only stdlib, SQLAlchemy, the SDK and its sibling plugin
modules (the plugin AST gate); it never touches ``app.*``. ``upsert_pending``
never commits — the caller owns that transaction. ``resolve_conflict`` owns its
own plugin transaction (baseline + close + audit) because the resolution is the
unit of work; it commits only after the chosen side was actually written.
"""

import json
import uuid
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

import httpx

from indramesh.sdk import (
    CoreDataError,
    ObjectRefCreate,
    ObjectValueCreate,
    ObjectValueUpdate,
    check_record_permission,
    create_object_ref,
    create_object_value,
    delete_object_ref,
    delete_object_value,
    update_object_value,
)

from .client import JiraPullApiError, JiraPullAuthError, JiraPullClient
from .link_guard import is_stale_binding
from .links import effective_direction
from .value_merge import (
    canonical_repr,
    canonical_set,
    decode_repr,
    repr_from_columns,
)
from .wire import (
    attribute_entry,
    build_update_payload,
    jira_reference_values,
    jira_scalar_values,
    parse_rejected_attribute_ids,
    remote_reference_ids,
    remote_scalar_items,
    strip_rejected,
)

SNAPSHOT_VERSION = 1

# The closed set of statuses a resolved conflict can carry (site vocabulary).
RESOLUTION_STATUSES = (
    "pending",
    "resolved_keep_local",
    "resolved_keep_remote",
    "resolved_auto",
)

# The GET /conflicts/count cap (Phase 295 renders "99+" beyond it).
COUNT_CAP = 100

_SCALAR_KINDS = ("scalar",)
_REFERENCE_KINDS = ("reference",)
_CARDINALITIES = ("single", "multi")


class PendingConflict(NamedTuple):
    """The outcome of one ``upsert_pending`` call."""

    id: uuid.UUID
    version: int
    inserted: bool
    changed: bool


def build_snapshot(kind: str, cardinality: str, data_type, items) -> dict:
    """A version-1 envelope over the complete local/remote value set (D-02).

    ``scalar`` items are canonical repr strings (duplicates kept — a scalar
    attribute is a multiset); ``reference`` items are ``{object_id,
    jira_object_id}`` dicts sorted by ``(jira_object_id, object_id)``. Sorting
    makes equal sets serialize byte-identically.
    """
    if kind not in _SCALAR_KINDS + _REFERENCE_KINDS:
        raise ValueError(f"unsupported conflict snapshot kind {kind!r}")
    if cardinality not in _CARDINALITIES:
        raise ValueError(f"unsupported conflict cardinality {cardinality!r}")

    if kind == "scalar":
        normalized = sorted(str(item) for item in items)
    else:
        normalized = [
            {
                "object_id": (
                    None if item.get("object_id") is None else str(item["object_id"])
                ),
                "jira_object_id": (
                    None
                    if item.get("jira_object_id") is None
                    else str(item["jira_object_id"])
                ),
            }
            for item in items
        ]
        normalized.sort(
            key=lambda item: (item["jira_object_id"] or "", item["object_id"] or "")
        )

    return {
        "v": SNAPSHOT_VERSION,
        "kind": kind,
        "cardinality": cardinality,
        "data_type": data_type,
        "items": normalized,
    }


def encode_snapshot(snapshot: dict) -> str:
    """A byte-stable serialization of a snapshot envelope."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def decode_snapshot(raw):
    """Decode a stored snapshot, refusing anything that is not envelope v1."""
    if raw is None:
        return None
    decoded = json.loads(raw)
    if not isinstance(decoded, dict) or decoded.get("v") != SNAPSHOT_VERSION:
        raise ValueError("unsupported Jira conflict snapshot version")
    return decoded


_VALUE_LOOKUP_SQL = text(
    """
    SELECT local_value, remote_value
    FROM plugin_jira_conflicts
    WHERE object_value_id = CAST(:object_value_id AS uuid)
      AND status = 'pending'
    """
)

_ATTRIBUTE_LOOKUP_SQL = text(
    """
    SELECT local_value, remote_value
    FROM plugin_jira_conflicts
    WHERE object_id = CAST(:object_id AS uuid)
      AND object_type_attribute_id = CAST(:object_type_attribute_id AS uuid)
      AND status = 'pending'
      AND object_value_id IS NULL
      AND object_ref_id IS NULL
    """
)

_VALUE_UPSERT_SQL = text(
    """
    INSERT INTO plugin_jira_conflicts
        (id, object_id, object_value_id, object_ref_id, object_type_attribute_id,
         task_id, connection_id, direction, status, local_value, remote_value,
         version, created_at, updated_at)
    VALUES (CAST(:id AS uuid), CAST(:object_id AS uuid),
            CAST(:object_value_id AS uuid), NULL,
            CAST(:object_type_attribute_id AS uuid), CAST(:task_id AS uuid),
            CAST(:connection_id AS uuid), :direction, 'pending',
            :local_value, :remote_value, 1, now(), now())
    ON CONFLICT (object_value_id)
        WHERE status = 'pending' AND object_value_id IS NOT NULL
    DO UPDATE SET local_value = EXCLUDED.local_value,
                  remote_value = EXCLUDED.remote_value,
                  task_id = EXCLUDED.task_id,
                  object_type_attribute_id = EXCLUDED.object_type_attribute_id,
                  updated_at = now(),
                  version = plugin_jira_conflicts.version + CASE
                      WHEN plugin_jira_conflicts.local_value
                               IS DISTINCT FROM EXCLUDED.local_value
                        OR plugin_jira_conflicts.remote_value
                               IS DISTINCT FROM EXCLUDED.remote_value
                      THEN 1 ELSE 0 END
    RETURNING id, version, (xmax = 0) AS inserted
    """
)

_ATTRIBUTE_UPSERT_SQL = text(
    """
    INSERT INTO plugin_jira_conflicts
        (id, object_id, object_value_id, object_ref_id, object_type_attribute_id,
         task_id, connection_id, direction, status, local_value, remote_value,
         version, created_at, updated_at)
    VALUES (CAST(:id AS uuid), CAST(:object_id AS uuid), NULL, NULL,
            CAST(:object_type_attribute_id AS uuid), CAST(:task_id AS uuid),
            CAST(:connection_id AS uuid), :direction, 'pending',
            :local_value, :remote_value, 1, now(), now())
    ON CONFLICT (object_id, object_type_attribute_id)
        WHERE status = 'pending' AND object_value_id IS NULL
          AND object_ref_id IS NULL
    DO UPDATE SET local_value = EXCLUDED.local_value,
                  remote_value = EXCLUDED.remote_value,
                  task_id = EXCLUDED.task_id,
                  updated_at = now(),
                  version = plugin_jira_conflicts.version + CASE
                      WHEN plugin_jira_conflicts.local_value
                               IS DISTINCT FROM EXCLUDED.local_value
                        OR plugin_jira_conflicts.remote_value
                               IS DISTINCT FROM EXCLUDED.remote_value
                      THEN 1 ELSE 0 END
    RETURNING id, version, (xmax = 0) AS inserted
    """
)


def _nullable(value):
    return None if value is None else str(value)


async def upsert_pending(
    session,
    *,
    scope: str,
    object_id,
    object_type_attribute_id,
    connection_id,
    direction: str,
    task_id,
    local_snapshot,
    remote_snapshot,
    object_value_id=None,
) -> PendingConflict:
    """Insert or update ONE pending conflict for the given anchor (D-03).

    ``scope`` is ``"value"`` (anchored on ``object_value_id``) or ``"attribute"``
    (anchored on ``(object_id, object_type_attribute_id)`` with both value/ref
    ids NULL). ``local_snapshot``/``remote_snapshot`` are ``build_snapshot``
    envelopes. The row's ``direction``/``connection_id`` are never changed by a
    re-observation. Never commits.
    """
    local_value = encode_snapshot(local_snapshot)
    remote_value = encode_snapshot(remote_snapshot)

    if scope == "value":
        lookup = await session.execute(
            _VALUE_LOOKUP_SQL, {"object_value_id": str(object_value_id)}
        )
        params = {
            "id": str(uuid.uuid4()),
            "object_id": str(object_id),
            "object_value_id": str(object_value_id),
            "object_type_attribute_id": _nullable(object_type_attribute_id),
            "task_id": _nullable(task_id),
            "connection_id": str(connection_id),
            "direction": direction,
            "local_value": local_value,
            "remote_value": remote_value,
        }
        statement = _VALUE_UPSERT_SQL
    elif scope == "attribute":
        lookup = await session.execute(
            _ATTRIBUTE_LOOKUP_SQL,
            {
                "object_id": str(object_id),
                "object_type_attribute_id": str(object_type_attribute_id),
            },
        )
        params = {
            "id": str(uuid.uuid4()),
            "object_id": str(object_id),
            "object_type_attribute_id": str(object_type_attribute_id),
            "task_id": _nullable(task_id),
            "connection_id": str(connection_id),
            "direction": direction,
            "local_value": local_value,
            "remote_value": remote_value,
        }
        statement = _ATTRIBUTE_UPSERT_SQL
    else:
        raise ValueError(f"unsupported conflict scope {scope!r}")

    previous = lookup.mappings().first()
    changed = (
        previous is None
        or previous["local_value"] != local_value
        or previous["remote_value"] != remote_value
    )

    row = (
        await session.execute(statement, params)
    ).mappings().first()
    return PendingConflict(
        id=row["id"],
        version=int(row["version"]),
        inserted=bool(row["inserted"]),
        changed=changed,
    )


# --- listing and counting (294-04, JSYNC-06 bug 5) ---------------------------

# The conflict row plus the Object it anchors and (LEFT JOINed, never INNER —
# bug 5) the connection's endpoint kind. ``object_type_attribute_id`` is joined
# directly on the attribute (bug 6: never through the non-existent
# ``ObjectRef.object_type_attribute_id``).
_CONFLICT_LIST_BASE = """
    SELECT c.id, c.object_id, c.object_value_id, c.object_ref_id,
           c.object_type_attribute_id, c.connection_id, c.direction,
           c.status, c.local_value, c.remote_value, c.version,
           c.created_at, c.updated_at, c.resolved_by, c.resolved_at,
           o.name AS object_name, o.schema_id AS schema_id,
           ota.name AS attribute_name,
           cc.source_id AS source_id, cc.target_id AS target_id,
           CASE WHEN cc.source_id IS NOT NULL THEN 'source' ELSE 'target' END
               AS endpoint_kind
    FROM plugin_jira_conflicts c
    LEFT JOIN plugin_jira_connections cc ON cc.id = c.connection_id
    JOIN objects o ON o.id = c.object_id
    LEFT JOIN object_type_attributes ota ON ota.id = c.object_type_attribute_id
"""

_CONFLICT_BY_ID_SQL = text(
    _CONFLICT_LIST_BASE + " WHERE c.id = CAST(:id AS uuid) FOR UPDATE OF c NOWAIT"
)

_OBJECT_LINK_SQL = text(
    """
    SELECT l.object_id, l.connection_id, l.source_id, l.jira_object_id,
           l.is_detached
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.object_id = CAST(:object_id AS uuid)
      AND s.source_id = l.source_id
    """
)

_CONNECTION_SQL = text(
    "SELECT id, source_id, target_id, base_url FROM plugin_jira_connections "
    "WHERE id = CAST(:id AS uuid)"
)

_OBJECT_ROW_SQL = text(
    "SELECT object_type_id, schema_id, name FROM objects "
    "WHERE id = CAST(:id AS uuid)"
)

_OTA_META_SQL = text(
    """
    SELECT ota.id, ota.object_type_id, ota.name, ota.maximum_cardinality,
           ota.structure, at.data_type
    FROM object_type_attributes ota
    JOIN attribute_types at ON at.id = ota.attribute_type_id
    WHERE ota.id = CAST(:id AS uuid)
    """
)

_ATTR_JIRA_ID_SQL = text(
    "SELECT jira_type_attribute_id FROM plugin_jira_attribute_links "
    "WHERE object_type_attribute_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid)"
)
_TYPE_JIRA_ID_SQL = text(
    "SELECT jira_type_id FROM plugin_jira_object_type_links "
    "WHERE object_type_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid)"
)
_ATTR_DIRECTION_SQL = text(
    "SELECT sync_direction FROM plugin_jira_attribute_links "
    "WHERE object_type_attribute_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid)"
)
_TYPE_DIRECTION_SQL = text(
    "SELECT sync_direction FROM plugin_jira_object_type_links "
    "WHERE object_type_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid)"
)
_SCHEMA_DIRECTION_SQL = text(
    "SELECT sync_direction FROM plugin_jira_schema_links "
    "WHERE schema_id = CAST(:id AS uuid) "
    "AND source_id = CAST(:source_id AS uuid)"
)

_OBJECT_VALUES_SQL = text(
    """
    SELECT v.id, v.value_simple, v.value_integer, v.value_float,
           v.value_boolean, v.value_datetime, v.value_struct
    FROM object_values v
    WHERE v.object_id = CAST(:object_id AS uuid)
      AND v.object_type_attribute_id = CAST(:ota_id AS uuid)
    ORDER BY v.id
    """
)
_OBJECT_VALUE_BY_ID_SQL = text(
    """
    SELECT v.id, v.value_simple, v.value_integer, v.value_float,
           v.value_boolean, v.value_datetime, v.value_struct
    FROM object_values v
    WHERE v.id = CAST(:id AS uuid)
    """
)
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
_SOURCE_OBJECT_LINKS_SQL = text(
    """
    SELECT l.jira_object_id, l.object_id
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND s.source_id = l.source_id
      AND l.is_detached = false
    """
)

_STATUS_RESOLVE_SQL = text(
    """
    SELECT l.jira_status_id, l.status_id
    FROM plugin_jira_status_links l
    JOIN statuses st ON st.id = l.status_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.jira_status_id IS NOT NULL
      AND st.schema_id = CAST(:schema_id AS uuid)
    """
)

_CLOSE_CONFLICT_SQL = text(
    """
    UPDATE plugin_jira_conflicts
    SET status = :status,
        resolved_by = CAST(:resolved_by AS uuid),
        resolved_at = now(),
        updated_at = now(),
        version = version + 1
    WHERE id = CAST(:id AS uuid)
      AND status = 'pending'
    """
)
_SYNC_AUDIT_SQL = text(
    """
    INSERT INTO plugin_jira_sync_audit_logs
        (id, connection_id, direction, status, entity_type, entity_id, details,
         created_at)
    VALUES (CAST(:id AS uuid), CAST(:connection_id AS uuid), :direction, :status,
            :entity_type, :entity_id, :details, now())
    """
)


def _serialize_conflict(row) -> dict:
    """The API projection of one conflict row, with decoded snapshots."""
    return {
        "id": str(row["id"]),
        "object_id": str(row["object_id"]),
        "object_name": row["object_name"],
        "schema_id": None if row["schema_id"] is None else str(row["schema_id"]),
        "object_value_id": (
            None if row["object_value_id"] is None else str(row["object_value_id"])
        ),
        "object_ref_id": (
            None if row["object_ref_id"] is None else str(row["object_ref_id"])
        ),
        "object_type_attribute_id": (
            None
            if row["object_type_attribute_id"] is None
            else str(row["object_type_attribute_id"])
        ),
        "attribute_name": row["attribute_name"],
        "direction": row["direction"],
        "endpoint_kind": row["endpoint_kind"],
        # R5-03/D-14: the bound endpoint UUID, projected only here — after the
        # caller passed the per-Object record-read gate in ``list_conflicts``.
        # A pull row exposes its Source, a push row its Target; the other is
        # null on the connection row itself.
        "source_id": (
            None if row["source_id"] is None else str(row["source_id"])
        ),
        "target_id": (
            None if row["target_id"] is None else str(row["target_id"])
        ),
        "status": row["status"],
        "version": int(row["version"]),
        "local": decode_snapshot(row["local_value"]),
        "remote": decode_snapshot(row["remote_value"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "resolved_by": (
            None if row["resolved_by"] is None else str(row["resolved_by"])
        ),
        "resolved_at": row["resolved_at"],
    }


def _list_statement(*, status, direction, schema_id, limit, offset):
    clauses = ["c.status = :status"]
    params = {"status": status, "limit": limit, "offset": offset}
    if direction is not None:
        clauses.append("c.direction = :direction")
        params["direction"] = direction
    if schema_id is not None:
        clauses.append("o.schema_id = CAST(:schema_id AS uuid)")
        params["schema_id"] = str(schema_id)
    sql = text(
        _CONFLICT_LIST_BASE
        + " WHERE "
        + " AND ".join(clauses)
        + " ORDER BY c.updated_at DESC, c.id LIMIT :limit OFFSET :offset"
    )
    return sql, params


async def list_conflicts(
    session,
    *,
    status="pending",
    direction=None,
    schema_id=None,
    limit=50,
    offset=0,
    user,
) -> dict:
    """One visible page of conflicts for both directions (bug 5, T-294-10).

    Every row is filtered through the deny-wins record resolver; a forbidden
    Object is omitted exactly like a missing one. ``next_offset`` is the raw DB
    offset after the scanned window (never the count of visible rows), and is
    ``None`` once the underlying page was short.
    """
    statement, params = _list_statement(
        status=status,
        direction=direction,
        schema_id=schema_id,
        limit=limit,
        offset=offset,
    )
    rows = (await session.execute(statement, params)).mappings().all()
    items = []
    for row in rows:
        if not await check_record_permission(user, "object", row["object_id"], "read"):
            continue
        items.append(_serialize_conflict(row))
    next_offset = None if len(rows) < limit else offset + limit
    return {"items": items, "next_offset": next_offset}


async def count_pending_conflicts(session, *, user, cap=COUNT_CAP) -> dict:
    """The visible pending total, capped at ``cap`` with a ``capped`` flag."""
    visible = 0
    offset = 0
    while True:
        rows = (
            await session.execute(
                text(
                    "SELECT id, object_id FROM plugin_jira_conflicts "
                    "WHERE status = 'pending' "
                    "ORDER BY updated_at DESC, id LIMIT :limit OFFSET :offset"
                ),
                {"limit": cap, "offset": offset},
            )
        ).mappings().all()
        if not rows:
            break
        for row in rows:
            if await check_record_permission(user, "object", row["object_id"], "read"):
                visible += 1
                if visible >= cap:
                    return {"pending": cap, "capped": True}
        if len(rows) < cap:
            break
        offset += cap
    return {"pending": visible, "capped": False}


_OBJECT_NAMES_SQL = text(
    "SELECT id, name FROM objects WHERE id = ANY(CAST(:ids AS uuid[]))"
)


async def list_object_names(session, *, ids, user) -> dict:
    """Names for the caller-readable Objects among ``ids`` (R5-03, S7 batching).

    One bounded batch read for every reference id an opened conflict detail
    needs. A missing Object and a forbidden one are omitted identically — the
    caller learns nothing about the denied id (T-295-05-I). The route enforces
    the ``objects:read`` resource gate and the id bounds; this function only
    resolves and filters the rows.
    """
    if not ids:
        return {"items": []}
    rows = (
        await session.execute(
            _OBJECT_NAMES_SQL, {"ids": [str(object_id) for object_id in ids]}
        )
    ).mappings().all()
    by_id = {str(row["id"]): row for row in rows}
    items = []
    for object_id in ids:
        row = by_id.get(str(object_id))
        if row is None:
            continue
        if not await check_record_permission(user, "object", row["id"], "read"):
            continue
        items.append({"id": str(row["id"]), "name": row["name"]})
    return {"items": items}


async def load_pending_conflicts(session, *, object_ids) -> dict:
    """Pending rows keyed by ``(object_id, anchor)`` for the engines (plans 05-08).

    ``anchor`` is the ObjectValue id, the ObjectRef id or (attribute scope) the
    ObjectTypeAttribute id. Not permission-filtered: the engine is the plugin
    itself, not a user request.
    """
    if not object_ids:
        return {}
    ids = [str(object_id) for object_id in object_ids]
    rows = (
        await session.execute(
            text(
                "SELECT id, object_id, object_value_id, object_ref_id, "
                "       object_type_attribute_id, connection_id, direction, "
                "       local_value, remote_value, version "
                "FROM plugin_jira_conflicts "
                "WHERE status = 'pending' "
                "AND object_id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": ids},
        )
    ).mappings().all()
    result = {}
    for row in rows:
        anchor = (
            row["object_value_id"]
            or row["object_ref_id"]
            or row["object_type_attribute_id"]
        )
        result[(row["object_id"], anchor)] = dict(row)
    return result


async def write_sync_audit(
    session,
    *,
    connection_id,
    direction,
    status,
    entity_type=None,
    entity_id=None,
    details=None,
) -> None:
    """Insert one connection-anchored audit row (bug 4; never a Source FK)."""
    await session.execute(
        _SYNC_AUDIT_SQL,
        {
            "id": str(uuid.uuid4()),
            "connection_id": str(connection_id),
            "direction": direction,
            "status": status,
            "entity_type": entity_type,
            "entity_id": None if entity_id is None else str(entity_id),
            "details": details,
        },
    )


async def close_conflict(
    session,
    *,
    conflict_id,
    status,
    resolved_by,
    connection_id,
    direction,
    object_id,
    details=None,
) -> None:
    """Set ``status``/``resolved_by``/``resolved_at``/version+1 and audit (no commit).

    Only a still-``pending`` row closes, so a repeated or racing closure is a
    no-op. ``resolved_by`` is ``None`` for an engine auto-closure.
    """
    if status not in RESOLUTION_STATUSES or status == "pending":
        raise ValueError(f"unsupported resolution status {status!r}")
    await session.execute(
        _CLOSE_CONFLICT_SQL,
        {
            "id": str(conflict_id),
            "status": status,
            "resolved_by": None if resolved_by is None else str(resolved_by),
        },
    )
    await write_sync_audit(
        session,
        connection_id=connection_id,
        direction=direction,
        status="resolved",
        entity_type="object",
        entity_id=object_id,
        details=details,
    )


# --- resolution (294-04, D-01/D-02) ------------------------------------------


def _direction_not_both(effective) -> HTTPException:
    """The machine-readable D-04 refusal, distinct from every other 409."""
    return HTTPException(
        409,
        detail={
            "error": {
                "code": "CONFLICT_DIRECTION_NOT_BOTH",
                "effective_direction": effective,
                "message": (
                    "This conflict is resolved by the next sync because the "
                    "attribute's sync direction is not both."
                ),
            }
        },
    )


async def _locked_conflict(session, conflict_id):
    try:
        return (
            await session.execute(_CONFLICT_BY_ID_SQL, {"id": str(conflict_id)})
        ).mappings().first()
    except DBAPIError as exc:
        if "lock" in str(exc).lower():
            raise HTTPException(
                409, "The conflict is being resolved by another request."
            ) from None
        raise


async def _schema_level_effective_direction(
    session, *, ota_id, object_type_id, schema_id, source_id
) -> str:
    async def _scalar(sql, params):
        row = (await session.execute(sql, params)).first()
        return None if row is None else row[0]

    attribute_link = await _scalar(
        _ATTR_DIRECTION_SQL, {"id": str(ota_id), "source_id": str(source_id)}
    )
    type_link = await _scalar(
        _TYPE_DIRECTION_SQL,
        {"id": str(object_type_id), "source_id": str(source_id)},
    )
    schema_link = await _scalar(
        _SCHEMA_DIRECTION_SQL,
        {"id": str(schema_id), "source_id": str(source_id)},
    )
    direction, _origin = effective_direction(
        attribute_link, type_link, schema_link
    )
    return direction


def _find_attribute(jira_object, jira_attribute_id):
    for attribute in (jira_object or {}).get("attributes") or []:
        if str(attribute.get("objectTypeAttributeId")) == str(jira_attribute_id):
            return attribute
    return None


async def _current_local_snapshot(
    session, *, stored, object_id, ota_id, object_value_id, attribute_name,
    source_id,
) -> dict:
    """The CURRENT local side as a ``build_snapshot`` envelope (drift input)."""
    kind = stored["kind"]
    cardinality = stored["cardinality"]
    data_type = stored["data_type"]
    if kind == "scalar":
        if object_value_id is not None:
            rows = (
                await session.execute(
                    _OBJECT_VALUE_BY_ID_SQL, {"id": str(object_value_id)}
                )
            ).mappings().all()
        else:
            rows = (
                await session.execute(
                    _OBJECT_VALUES_SQL,
                    {"object_id": str(object_id), "ota_id": str(ota_id)},
                )
            ).mappings().all()
        items = []
        for row in rows:
            typed, value = repr_from_columns(row)
            if typed is not None:
                items.append(canonical_repr(typed, value))
        return build_snapshot(kind, cardinality, data_type, items)

    # reference
    links = await _source_object_links(session, source_id)
    by_object = {row["object_id"]: str(row["jira_object_id"]) for row in links}
    refs = (
        await session.execute(
            _OBJECT_REFS_SQL,
            {
                "source_id": str(source_id),
                "object_id": str(object_id),
                "attribute_name": attribute_name,
            },
        )
    ).mappings().all()
    items = [
        {
            "object_id": str(row["to_object_id"]),
            "jira_object_id": by_object.get(row["to_object_id"]),
        }
        for row in refs
    ]
    return build_snapshot(kind, cardinality, data_type, items)


async def _source_object_links(session, source_id):
    return (
        await session.execute(_SOURCE_OBJECT_LINKS_SQL, {"source_id": str(source_id)})
    ).mappings().all()


async def _status_resolver(session, *, source_id, schema_id):
    """A sync ``jira_status_id -> Core status id`` lookup for one schema."""
    rows = (
        await session.execute(
            _STATUS_RESOLVE_SQL,
            {"source_id": str(source_id), "schema_id": str(schema_id)},
        )
    ).mappings().all()
    by_jira = {str(row["jira_status_id"]): str(row["status_id"]) for row in rows}
    return lambda jira_id: by_jira.get(str(jira_id))


async def _status_jira_ids(session, *, source_id, schema_id):
    """A ``Core status id -> jira_status_id`` map for one schema."""
    rows = (
        await session.execute(
            _STATUS_RESOLVE_SQL,
            {"source_id": str(source_id), "schema_id": str(schema_id)},
        )
    ).mappings().all()
    return {str(row["status_id"]): str(row["jira_status_id"]) for row in rows}


async def _remote_items(
    session, stored, jira_attribute, *, source_id, structure, status_resolver=None
):
    """The CURRENT Jira side as ``build_snapshot`` items (drift input).

    Reference items carry the linked Core ``object_id`` exactly as the pull/push
    engines store them (``pull._reference_items`` / ``push``), resolved through
    the live Source object links. Without it a recomputed reference snapshot would
    never equal the stored one and the drift guard would trip on every resolve.
    """
    kind = stored["kind"]
    if kind == "reference":
        links = await _source_object_links(session, source_id)
        by_jira = {
            str(row["jira_object_id"]): str(row["object_id"]) for row in links
        }
        return [
            {"object_id": by_jira.get(str(jira_id)), "jira_object_id": jira_id}
            for jira_id in remote_reference_ids(jira_attribute)
        ]
    return remote_scalar_items(
        stored["data_type"],
        jira_attribute,
        structure=structure,
        status_resolver=status_resolver,
    )


def _decode_repr(rep, data_type=None) -> tuple[str | None, object]:
    return decode_repr(rep, data_type)


async def _reconcile_refs(
    ctx, session, *, object_id, object_type_id, attribute_name, source_id, desired,
    user,
):
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
    remaining = [dict(row) for row in rows]
    for target_id in desired:
        match = next(
            (row for row in remaining if str(row["to_object_id"]) == str(target_id)),
            None,
        )
        if match is not None:
            remaining.remove(match)
            continue
        await create_object_ref(
            ctx,
            ObjectRefCreate(
                from_object_id=object_id,
                to_object_id=target_id,
                object_type_id=object_type_id,
                source_id=source_id,
                from_attribute_name=attribute_name,
            ),
            user=user,
        )
    for row in remaining:
        await delete_object_ref(ctx, row["id"], user=user)


async def apply_remote_set_to_core(
    ctx,
    session,
    *,
    scope,
    object_id,
    ota_id,
    data_type,
    structure,
    remote_items,
    object_value_id=None,
    object_type_id=None,
    source_id=None,
    attribute_name=None,
    user=None,
) -> None:
    """Converge Core onto the chosen Jira set by content diff (D-01/D-02).

    ``scope`` is ``"value"`` (the anchored ObjectValue), ``"attribute"`` (a
    scalar multi-value set) or a reference group (``data_type == "reference"``).
    A raise here leaves the plugin conflict untouched; the caller maps it.
    """
    if scope == "value":
        if not remote_items:
            await delete_object_value(ctx, object_value_id, user=user)
            return
        typed_column, value = _decode_repr(remote_items[0], data_type)
        await update_object_value(
            ctx, object_value_id, ObjectValueUpdate(**{typed_column: value}), user=user
        )
        return

    if data_type == "reference":
        desired = []
        for item in remote_items:
            if item.get("object_id") is None:
                raise HTTPException(
                    409, "A Jira reference target has no linked Core object."
                )
            desired.append(item["object_id"])
        await _reconcile_refs(
            ctx,
            session,
            object_id=object_id,
            object_type_id=object_type_id,
            attribute_name=attribute_name,
            source_id=source_id,
            desired=desired,
            user=user,
        )
        return

    rows = (
        await session.execute(
            _OBJECT_VALUES_SQL,
            {"object_id": str(object_id), "ota_id": str(ota_id)},
        )
    ).mappings().all()
    remaining = []
    for row in rows:
        typed, value = repr_from_columns(row)
        remaining.append(
            (None if typed is None else canonical_repr(typed, value), dict(row))
        )
    for rep in remote_items:
        match = next((item for item in remaining if item[0] == rep), None)
        if match is not None:
            remaining.remove(match)
            continue
        typed_column, value = _decode_repr(rep, data_type)
        await create_object_value(
            ctx,
            ObjectValueCreate(
                object_id=object_id,
                object_type_attribute_id=ota_id,
                **{typed_column: value},
            ),
            user=user,
        )
    for _rep, row in remaining:
        await delete_object_value(ctx, row["id"], user=user)


async def _apply_keep_local_to_jira(client, jira_object_id, payload, conflicted_id):
    """One PUT; a 400 naming rejected passthrough attributes retries once."""
    try:
        await client.update_object(jira_object_id, payload)
        return
    except (JiraPullAuthError, JiraPullApiError) as exc:
        status_code = getattr(exc, "status_code", None)
        rejected = parse_rejected_attribute_ids(getattr(exc, "message", "") or "")
        is_400 = status_code == 400
        if is_400 and rejected:
            conflicted = int(conflicted_id)
            if conflicted in rejected:
                raise HTTPException(
                    502, "Jira rejected the conflicted attribute."
                ) from None
            retry = strip_rejected(payload, rejected)
            try:
                await client.update_object(jira_object_id, retry)
                return
            except (JiraPullAuthError, JiraPullApiError, httpx.HTTPError):
                raise HTTPException(502, "The Jira write failed.") from None
        raise HTTPException(502, "The Jira write failed.") from None
    except httpx.HTTPError:
        raise HTTPException(502, "The Jira write failed.") from None


async def resolve_conflict(
    ctx,
    session,
    *,
    conflict_id,
    resolution,
    version,
    service_account_id,
    user,
    transport=None,
) -> dict:
    """Apply the chosen side, then close the conflict in one plugin transaction.

    Nothing is marked resolved and no baseline advances unless the write landed
    (the top prohibition). A failed Jira/Core write rolls back and leaves the row
    pending; a stale version, a non-pending row, a held row lock, a stale binding
    or a sync-owned (non-``both``) direction refuses with 409 and changes nothing.
    """
    from .checkpoints import upsert_attribute_baseline, upsert_value_baseline
    from .routes import _guard_pat_target, _load_account

    row = await _locked_conflict(session, conflict_id)
    if row is None:
        raise HTTPException(404, "Conflict not found.")
    if row["status"] != "pending" or int(row["version"]) != int(version):
        raise HTTPException(
            409, "The conflict is not pending or the version is stale."
        )

    object_id = row["object_id"]
    link = (
        await session.execute(_OBJECT_LINK_SQL, {"object_id": str(object_id)})
    ).mappings().first()
    if link is None or link["is_detached"] or link["jira_object_id"] is None:
        raise HTTPException(409, "The object has no live Jira link.")
    if await is_stale_binding(session, link["connection_id"], object_id):
        raise HTTPException(409, "The Jira object binding is stale for this Source.")

    source_conn = (
        await session.execute(_CONNECTION_SQL, {"id": str(link["connection_id"])})
    ).mappings().first()
    conflict_conn = (
        await session.execute(_CONNECTION_SQL, {"id": str(row["connection_id"])})
    ).mappings().first()
    if source_conn is None or source_conn["source_id"] is None:
        raise HTTPException(409, "The object link's Source connection is unavailable.")
    if conflict_conn is None:
        raise HTTPException(409, "The conflict connection is unavailable.")

    if row["direction"] == "pull":
        expect, endpoint_id = "source", conflict_conn["source_id"]
    else:
        expect, endpoint_id = "target", conflict_conn["target_id"]
        if (conflict_conn["base_url"] or "") != (source_conn["base_url"] or ""):
            raise HTTPException(
                409, "The push Target points at a different Jira instance."
            )
    account = await _load_account(
        session, service_account_id, expect=expect, endpoint_id=endpoint_id
    )
    # SESS-07 (D-06): the endpoint about to receive the PAT is re-vetted against a
    # fresh answer, read from its own current URL and flag — pull-direction rows
    # as the Source, push-direction rows as the Target. ``is_stale_binding`` above
    # already refused a repointed endpoint, so for a live binding
    # ``source_conn["base_url"]`` is ``normalize_url(<endpoint url>)`` and the
    # guarded URL is the dialled one; a flagged endpoint therefore passes the
    # address check and still cannot pass the stale check.
    #
    # The wrapper is imported inside the function on purpose: ``connections.py``
    # imports ``.conflicts`` at module level, so a module-level import of
    # ``.outbound`` here (which imports ``.connections``) would close the cycle
    # ``conflicts -> outbound -> connections -> conflicts``.
    await _guard_pat_target(session, kind=expect, endpoint_id=endpoint_id)

    if row["object_type_attribute_id"] is None:
        raise HTTPException(409, "The conflict carries no attribute anchor.")
    ota_id = row["object_type_attribute_id"]
    meta = (
        await session.execute(_OTA_META_SQL, {"id": str(ota_id)})
    ).mappings().first()
    if meta is None:
        raise HTTPException(409, "The conflicted attribute is unavailable.")

    object_row = (
        await session.execute(_OBJECT_ROW_SQL, {"id": str(object_id)})
    ).mappings().first()
    if object_row is None:
        raise HTTPException(404, "Conflict not found.")

    effective = await _schema_level_effective_direction(
        session,
        ota_id=ota_id,
        object_type_id=meta["object_type_id"],
        schema_id=object_row["schema_id"],
        source_id=source_conn["source_id"],
    )
    if effective != "both":
        raise _direction_not_both(effective)

    attr_link = (
        await session.execute(
            _ATTR_JIRA_ID_SQL,
            {"id": str(ota_id), "source_id": str(source_conn["source_id"])},
        )
    ).first()
    type_link = (
        await session.execute(
            _TYPE_JIRA_ID_SQL,
            {"id": str(meta["object_type_id"]), "source_id": str(source_conn["source_id"])},
        )
    ).first()
    if attr_link is None or type_link is None:
        raise HTTPException(409, "The Jira attribute or type link is missing.")
    jira_attribute_id = attr_link[0]
    jira_type_id = type_link[0]

    stored_local = decode_snapshot(row["local_value"])
    stored_remote = decode_snapshot(row["remote_value"])
    if stored_local is None or stored_remote is None:
        raise HTTPException(409, "The conflict snapshot is malformed.")

    source_id = source_conn["source_id"]
    scope = "value" if row["object_value_id"] is not None else "attribute"

    current_local = await _current_local_snapshot(
        session,
        stored=stored_local,
        object_id=object_id,
        ota_id=ota_id,
        object_value_id=row["object_value_id"],
        attribute_name=meta["name"],
        source_id=source_id,
    )

    client = JiraPullClient(
        base_url=source_conn["base_url"],
        pat=account["token"],
        transport=transport,
        max_429_retries=0,
    )
    try:
        jira_object = await client.get_object(str(link["jira_object_id"]))
    except (JiraPullAuthError, JiraPullApiError, httpx.HTTPError):
        await client.aclose()
        raise HTTPException(502, "The Jira object could not be read.") from None
    jira_attribute = _find_attribute(jira_object, jira_attribute_id)
    status_resolver = await _status_resolver(
        session, source_id=source_id, schema_id=object_row["schema_id"]
    )
    remote_items = await _remote_items(
        session,
        stored_remote,
        jira_attribute,
        source_id=source_id,
        structure=meta["structure"],
        status_resolver=status_resolver,
    )
    fresh_remote = build_snapshot(
        stored_remote["kind"],
        stored_remote["cardinality"],
        stored_remote["data_type"],
        remote_items,
    )

    async def _drift_both():
        await session.execute(
            text(
                "UPDATE plugin_jira_conflicts "
                "SET local_value = :local, remote_value = :remote, "
                "    version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {
                "id": str(conflict_id),
                "local": encode_snapshot(current_local),
                "remote": encode_snapshot(fresh_remote),
            },
        )
        await session.commit()

    if (
        current_local["items"] != stored_local["items"]
        or fresh_remote["items"] != stored_remote["items"]
    ):
        await _drift_both()
        await client.aclose()
        raise HTTPException(
            409, "The conflict changed; review and resolve again."
        )

    if resolution == "keep_local":
        if stored_local["kind"] == "reference":
            replaced = attribute_entry(
                jira_attribute_id,
                jira_reference_values(
                    [
                        item["jira_object_id"]
                        for item in current_local["items"]
                        if item.get("jira_object_id")
                    ]
                ),
            )
        else:
            replaced = attribute_entry(
                jira_attribute_id,
                jira_scalar_values(
                    stored_local["data_type"],
                    current_local["items"],
                    structure=meta["structure"],
                    status_jira_ids=await _status_jira_ids(
                        session,
                        source_id=source_id,
                        schema_id=object_row["schema_id"],
                    ),
                ),
            )
        payload = build_update_payload(
            jira_type_id=jira_type_id,
            remote_attributes=(jira_object or {}).get("attributes"),
            replaced_entries=[replaced],
        )
        try:
            await _apply_keep_local_to_jira(
                client, str(link["jira_object_id"]), payload, jira_attribute_id
            )
        except HTTPException:
            await session.rollback()
            await client.aclose()
            raise
        applied_items = current_local["items"]
        applied_reprs = current_local["items"]
    else:
        # keep_remote: mirror the freshly read Jira set onto Core.
        if stored_remote["kind"] == "reference":
            links = await _source_object_links(session, source_id)
            by_jira = {str(r["jira_object_id"]): str(r["object_id"]) for r in links}
            desired_items = []
            for item in remote_items:
                jira_id = item.get("jira_object_id")
                desired_items.append(
                    {"object_id": by_jira.get(str(jira_id)), "jira_object_id": jira_id}
                )
            apply_items = desired_items
        else:
            apply_items = remote_items
        try:
            await apply_remote_set_to_core(
                ctx,
                session,
                scope=scope,
                object_id=object_id,
                ota_id=ota_id,
                data_type=stored_remote["data_type"],
                structure=meta["structure"],
                remote_items=apply_items,
                object_value_id=row["object_value_id"],
                object_type_id=meta["object_type_id"],
                source_id=source_id,
                attribute_name=meta["name"],
                user=user,
            )
        except HTTPException:
            await session.rollback()
            await client.aclose()
            raise
        except CoreDataError:
            await session.rollback()
            await client.aclose()
            raise HTTPException(409, "The Core write was refused.") from None
        applied_items = apply_items
        applied_reprs = remote_items

    # One plugin transaction: baseline, close, audit.
    if scope == "value":
        applied_reprs = applied_items if stored_local["kind"] != "reference" else []
        if applied_reprs:
            await upsert_value_baseline(
                session,
                object_value_id=row["object_value_id"],
                connection_id=link["connection_id"],
                canonical=applied_reprs[0],
            )
        else:
            await upsert_value_baseline(
                session,
                object_value_id=row["object_value_id"],
                connection_id=link["connection_id"],
                canonical=None,
            )
    else:
        if stored_local["kind"] == "reference":
            # Every other writer of plugin_jira_attribute_sync_state stores the
            # canonical set; the merge compares the two engines' baselines, so an
            # envelope snapshot here would make both sides look changed.
            set_repr = canonical_set(
                str(item["jira_object_id"])
                for item in applied_items
                if item.get("jira_object_id")
            )
        else:
            set_repr = canonical_set(applied_reprs)
        await upsert_attribute_baseline(
            session,
            object_id=object_id,
            object_type_attribute_id=ota_id,
            connection_id=link["connection_id"],
            set_repr=set_repr,
        )

    status = (
        "resolved_keep_local" if resolution == "keep_local" else "resolved_keep_remote"
    )
    await close_conflict(
        session,
        conflict_id=conflict_id,
        status=status,
        resolved_by=user.id,
        connection_id=row["connection_id"],
        direction=row["direction"],
        object_id=object_id,
        details=json.dumps({"resolution": resolution}),
    )
    await session.commit()
    await client.aclose()

    return {
        "id": str(conflict_id),
        "status": status,
        "resolution": resolution,
        "object_id": str(object_id),
        "direction": row["direction"],
    }
