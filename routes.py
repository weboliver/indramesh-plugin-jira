"""Permission- and Source-scoped Jira plugin read/settings routes (293-07).

Five authenticated routes back the later plugin UI (Phase 295) and make the
per-level sync policy and object detach durable in the plugin's own link tables:

* ``GET /links/{entity}?ids=...`` — one bounded batch read per link family
  (``schema``/``object_type``/``attribute``/``status``/``reference_type``/
  ``object``/``comment``/``attachment``). Every id is resolved against the
  entity's OWN Source lineage, gated by Core's deny-wins record resolver, and
  projected as ``linked``/``jira_id``/direction (with origin) / ``is_detached``.
  An id the caller may not see is reported as an explicit unlinked item, never as
  a foreign-source mapping (T-293-07-01).
* ``GET /schemas/source/{source_id}`` and ``GET /schemas/target/{target_id}`` —
  the Jira schema picker for a Source or Target. The bound, active ServiceAccount
  is verified for the endpoint, its PAT is read through the typed
  ``EncryptedString`` column only after the permission gate, and the credential
  is never returned or logged (T-293-07-03).
* ``PUT /directions/{entity}/{id}`` — set or clear an explicit Schema/ObjectType/
  Attribute direction (``none|pull|push|both|null``). It reuses
  ``links.effective_direction`` (F-05) instead of a second evaluator.
* ``PUT /objects/{id}/detach`` — toggle ``is_detached`` on an EXISTING
  JiraObjectLink (never delete/recreate) after the record-write gate and the
  live-binding re-check.

Only ``indramesh.sdk`` (plus third-party/stdlib) is imported; the plugin never
reaches into ``app.*`` or Core raw-SQL write paths.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from indramesh.sdk import (
    EncryptedString,
    PluginTaskError,
    check_record_permission,
    require_permission,
    set_object_write_protection_release,
)

from .admission import source_run_lock
from .adoption import link_existing_schema, list_adoptable_schemas
from .client import JiraPullApiError, JiraPullAuthError, JiraPullClient
from .conflicts import (
    RESOLUTION_STATUSES,
    count_pending_conflicts,
    list_conflicts,
    list_object_names,
    resolve_conflict,
)
from .connections import (
    JIRA_SCHEMA_ID_RE,
    JIRA_UNREACHABLE,
    count_destination_object_types,
    get_connection,
    load_schema_sync_status,
    run_connection_test,
    upsert_connection,
)
from .link_guard import is_stale_binding
from .system_attributes import is_jira_metadata
from .links import (
    SYNC_DIRECTIONS,
    LinkLineageError,
    assert_link_lineage,
    effective_direction,
)
from .protection import reconcile_write_protection

# The maximum batch a single ``GET /links/{entity}`` accepts: reject an oversize
# request rather than silently truncate it.
MAX_BATCH_IDS = 100

# The three families that carry an explicit per-level sync direction.
DIRECTION_ENTITIES = ("schema", "object_type", "attribute")


# --- family table (whitelist, never a dynamic table name) --------------------
#
# Every query joins the Core entity that OWNS the link so the row's Source is
# PROVEN (the composite plugin FK only proves the connection's Source). Columns:
# core_id, source_id, jira_id, schema_id, object_type_id (the type whose link
# supplies the type-level direction), is_detached, record_id (the Core record the
# deny-wins permission gate is resolved against).
_FAMILIES: dict[str, dict[str, Any]] = {
    "schema": {
        "record_entity_type": "schema",
        "has_direction": True,
        "sql": text(
            """
            SELECT l.schema_id        AS core_id,
                   l.source_id        AS source_id,
                   l.jira_schema_id   AS jira_id,
                   l.schema_id        AS schema_id,
                   NULL::uuid         AS object_type_id,
                   false              AS is_detached,
                   l.schema_id        AS record_id
            FROM plugin_jira_schema_links l
            JOIN schemas s ON s.id = l.schema_id
            WHERE l.schema_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "object_type": {
        "record_entity_type": "object_type",
        "has_direction": True,
        "sql": text(
            """
            SELECT l.object_type_id   AS core_id,
                   l.source_id        AS source_id,
                   l.jira_type_id     AS jira_id,
                   ot.schema_id       AS schema_id,
                   ot.id              AS object_type_id,
                   false              AS is_detached,
                   ot.id              AS record_id
            FROM plugin_jira_object_type_links l
            JOIN object_types ot ON ot.id = l.object_type_id
            JOIN schemas s ON s.id = ot.schema_id
            WHERE l.object_type_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "attribute": {
        "record_entity_type": "object_type",
        "has_direction": True,
        "sql": text(
            """
            SELECT l.object_type_attribute_id AS core_id,
                   l.source_id                AS source_id,
                   l.jira_type_attribute_id   AS jira_id,
                   ot.schema_id               AS schema_id,
                   ot.id                      AS object_type_id,
                   false                      AS is_detached,
                   ot.id                      AS record_id
            FROM plugin_jira_attribute_links l
            JOIN object_type_attributes ota
              ON ota.id = l.object_type_attribute_id
            JOIN object_types ot ON ot.id = ota.object_type_id
            JOIN schemas s ON s.id = ot.schema_id
            WHERE l.object_type_attribute_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "status": {
        "record_entity_type": "schema",
        "has_direction": False,
        "sql": text(
            """
            SELECT l.status_id  AS core_id,
                   l.source_id  AS source_id,
                   l.jira_status_id AS jira_id,
                   st.schema_id AS schema_id,
                   NULL::uuid   AS object_type_id,
                   false        AS is_detached,
                   st.schema_id AS record_id
            FROM plugin_jira_status_links l
            JOIN statuses st ON st.id = l.status_id
            JOIN schemas s ON s.id = st.schema_id
            WHERE l.status_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "reference_type": {
        "record_entity_type": "source",
        "has_direction": False,
        "sql": text(
            """
            SELECT l.reference_type_id      AS core_id,
                   l.source_id              AS source_id,
                   l.jira_reference_type_id AS jira_id,
                   rt.schema_id             AS schema_id,
                   NULL::uuid               AS object_type_id,
                   false                    AS is_detached,
                   l.source_id              AS record_id
            FROM plugin_jira_reference_type_links l
            JOIN reference_types rt ON rt.id = l.reference_type_id
            WHERE l.reference_type_id = CAST(:id AS uuid)
              AND rt.source_id = l.source_id
            """
        ),
    },
    "object": {
        "record_entity_type": "object",
        "has_direction": False,
        "sql": text(
            """
            SELECT l.object_id     AS core_id,
                   l.source_id     AS source_id,
                   l.jira_object_id AS jira_id,
                   o.schema_id     AS schema_id,
                   NULL::uuid      AS object_type_id,
                   l.is_detached   AS is_detached,
                   o.id            AS record_id
            FROM plugin_jira_object_links l
            JOIN objects o ON o.id = l.object_id
            JOIN schemas s ON s.id = o.schema_id
            WHERE l.object_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "comment": {
        "record_entity_type": "object",
        "has_direction": False,
        "sql": text(
            """
            SELECT l.object_comment_id AS core_id,
                   l.source_id         AS source_id,
                   l.jira_comment_id   AS jira_id,
                   o.schema_id         AS schema_id,
                   NULL::uuid          AS object_type_id,
                   false               AS is_detached,
                   o.id                AS record_id
            FROM plugin_jira_comment_links l
            JOIN object_comments oc ON oc.id = l.object_comment_id
            JOIN objects o ON o.id = oc.object_id
            JOIN schemas s ON s.id = o.schema_id
            WHERE l.object_comment_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
    "attachment": {
        "record_entity_type": "object",
        "has_direction": False,
        "sql": text(
            """
            SELECT l.object_file_id    AS core_id,
                   l.source_id         AS source_id,
                   l.jira_attachment_id AS jira_id,
                   o.schema_id         AS schema_id,
                   NULL::uuid          AS object_type_id,
                   false               AS is_detached,
                   o.id                AS record_id
            FROM plugin_jira_attachment_links l
            JOIN object_files of ON of.id = l.object_file_id
            JOIN objects o ON o.id = of.object_id
            JOIN schemas s ON s.id = o.schema_id
            WHERE l.object_file_id = CAST(:id AS uuid)
              AND s.source_id = l.source_id
            """
        ),
    },
}

# The object badge shows the Jira key from the pull-imported "Jira Key" value
# (identified by its structure marker, 261002-qyq) in one batch read.
_OBJECT_KEYS_SQL = text(
    """
    SELECT ov.object_id, ov.value_simple
    FROM object_values ov
    JOIN object_type_attributes ota ON ota.id = ov.object_type_attribute_id
    WHERE ov.object_id = ANY(CAST(:ids AS uuid[]))
      AND ota.structure ->> 'jira_managed_field' = 'Key'
      AND ov.value_simple IS NOT NULL
    """
)

# Direction rows for a Source. A link is only ever read for an entity already
# proven to belong to that Source, so a direction lookup can never leak a
# foreign-Source value.
_SCHEMA_DIRECTIONS_SQL = text(
    "SELECT schema_id, sync_direction FROM plugin_jira_schema_links "
    "WHERE source_id = CAST(:source_id AS uuid)"
)
_TYPE_DIRECTIONS_SQL = text(
    "SELECT object_type_id, sync_direction FROM plugin_jira_object_type_links "
    "WHERE source_id = CAST(:source_id AS uuid)"
)
_ATTRIBUTE_DIRECTIONS_SQL = text(
    "SELECT l.object_type_attribute_id, l.sync_direction, ota.structure "
    "FROM plugin_jira_attribute_links l "
    "JOIN object_type_attributes ota ON ota.id = l.object_type_attribute_id "
    "WHERE l.source_id = CAST(:source_id AS uuid)"
)

# Account read through the typed EncryptedString column so the PAT is decrypted
# only after the permission gate. TextClause.columns() attaches the result
# processor without any Core ORM import.
_ACCOUNT_SQL = text(
    """
    SELECT id, source_id, target_id, is_active, expires_at,
           source_bearer_token, target_bearer_token
    FROM service_accounts
    WHERE id = CAST(:id AS uuid)
    """
).columns(
    source_bearer_token=EncryptedString(),
    target_bearer_token=EncryptedString(),
)

_SOURCE_SQL = text(
    "SELECT id, type, source_url FROM sources WHERE id = CAST(:id AS uuid)"
)
_TARGET_SQL = text(
    "SELECT id, connector_type, target_url, schema_id FROM targets "
    "WHERE id = CAST(:id AS uuid)"
)

_OBJECT_LINK_SQL = text(
    """
    SELECT l.object_id AS object_id,
           l.connection_id AS connection_id,
           l.source_id AS source_id,
           l.jira_object_id AS jira_id,
           l.is_detached AS is_detached
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.object_id = CAST(:id AS uuid)
      AND s.source_id = l.source_id
    """
)

_UPDATE_DIRECTION_SQL = {
    "schema": text(
        "UPDATE plugin_jira_schema_links SET sync_direction = :direction "
        "WHERE schema_id = CAST(:id AS uuid)"
    ),
    "object_type": text(
        "UPDATE plugin_jira_object_type_links SET sync_direction = :direction "
        "WHERE object_type_id = CAST(:id AS uuid)"
    ),
    "attribute": text(
        "UPDATE plugin_jira_attribute_links SET sync_direction = :direction "
        "WHERE object_type_attribute_id = CAST(:id AS uuid)"
    ),
}

_UPDATE_DETACH_SQL = text(
    "UPDATE plugin_jira_object_links SET is_detached = :detached "
    "WHERE object_id = CAST(:id AS uuid)"
)


def _make_transport():
    """The HTTP transport for a route's Jira call; ``None`` selects the network.

    A module-level factory so a test can inject an ``httpx`` mock transport.
    Production always gets ``None``.
    """
    return None


def _parse_batch_ids(raw: list[str]) -> list[uuid.UUID]:
    """Parse repeated/comma-delimited UUIDs into a bounded, stable-ordered list.

    Anything invalid or over the cap is a ``ValueError`` the route maps to 422 —
    an oversize batch is rejected, never silently truncated.
    """
    ordered: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for chunk in raw:
        for piece in str(chunk).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                parsed = uuid.UUID(piece)
            except (ValueError, AttributeError, TypeError):
                raise ValueError(f"Invalid UUID in ids: {piece!r}") from None
            if parsed not in seen:
                seen.add(parsed)
                ordered.append(parsed)
    if len(ordered) > MAX_BATCH_IDS:
        raise ValueError(
            f"At most {MAX_BATCH_IDS} ids may be requested per call."
        )
    return ordered


async def _load_directions(session, source_ids) -> dict[str, dict]:
    """The explicit direction maps for every Source in one batch read."""
    maps = {"schema": {}, "object_type": {}, "attribute": {}}
    for source_id in source_ids:
        params = {"source_id": str(source_id)}
        for row in (
            await session.execute(_SCHEMA_DIRECTIONS_SQL, params)
        ).mappings():
            maps["schema"][row["schema_id"]] = row["sync_direction"]
        for row in (
            await session.execute(_TYPE_DIRECTIONS_SQL, params)
        ).mappings():
            maps["object_type"][row["object_type_id"]] = row["sync_direction"]
        for row in (
            await session.execute(_ATTRIBUTE_DIRECTIONS_SQL, params)
        ).mappings():
            maps["attribute"][row["object_type_attribute_id"]] = (
                "pull" if is_jira_metadata(row["structure"]) else row["sync_direction"]
            )
    return maps


def _project_direction(entity: str, row, maps: dict[str, dict]):
    """``(explicit, effective, origin)`` for a direction-bearing family."""
    schema_dir = maps["schema"].get(row["schema_id"])
    if entity == "schema":
        explicit = schema_dir
        effective, origin = effective_direction(None, None, explicit)
        return explicit, effective, origin
    if entity == "object_type":
        explicit = maps["object_type"].get(row["object_type_id"])
        effective, origin = effective_direction(None, explicit, schema_dir)
        return explicit, effective, origin
    # attribute
    explicit = maps["attribute"].get(row["core_id"])
    type_dir = maps["object_type"].get(row["object_type_id"])
    effective, origin = effective_direction(explicit, type_dir, schema_dir)
    return explicit, effective, origin


def _unlinked_item(entity_id: uuid.UUID) -> dict:
    return {
        "id": str(entity_id),
        "linked": False,
        "jira_id": None,
        "jira_key": None,
        "explicit_direction": None,
        "effective_direction": None,
        "direction_origin": None,
        "is_detached": None,
    }


class DirectionUpdate(BaseModel):
    """The explicit direction body; ``null`` means "inherit from the parent"."""

    direction: str | None = None


class DetachUpdate(BaseModel):
    detached: bool


class ConnectionUpsert(BaseModel):
    """The connection bind/update body; extra fields are rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    default_conflict_strategy: Literal["prefer_local", "prefer_remote"] | None = (
        None
    )
    # Omitted keeps the stored default account; ``null`` clears it (261004).
    default_service_account_id: uuid.UUID | None = None


class SchemaLinkRequest(BaseModel):
    """Link an existing empty schema to a Jira schema (261004); no import runs."""

    model_config = ConfigDict(extra="forbid")

    jira_schema_id: str
    service_account_id: uuid.UUID


_SCHEMA_SOURCE_SQL = text(
    "SELECT id, source_id FROM schemas WHERE id = CAST(:id AS uuid)"
)
# A link made with an account remembers it as the connection default when the
# connection has none yet (261004); an existing default is never overwritten.
_ADOPT_DEFAULT_ACCOUNT_SQL = text(
    "UPDATE plugin_jira_connections "
    "SET default_service_account_id = CAST(:account_id AS uuid), updated_at = now() "
    "WHERE id = CAST(:id AS uuid) AND default_service_account_id IS NULL"
)


class ConnectionTestRequest(BaseModel):
    """The connection-test body: which bound account to use and an optional schema."""

    model_config = ConfigDict(extra="forbid")

    service_account_id: uuid.UUID
    jira_schema_id: str | None = None


class ResolveConflict(BaseModel):
    """The conflict-resolution body; extra fields are rejected (422)."""

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["keep_local", "keep_remote"]
    version: int = 1
    service_account_id: uuid.UUID


# The conflict's Object, read before the record-write gate (a denied object is
# reported exactly like a missing one — T-294-10).
_CONFLICT_OBJECT_SQL = text(
    "SELECT object_id FROM plugin_jira_conflicts WHERE id = CAST(:id AS uuid)"
)


def _require_schema_digits(jira_schema_id: str | None) -> None:
    """Reject a non-digit Jira schema id (SSRF-safe path/body, T-294-07)."""
    if jira_schema_id is not None and not JIRA_SCHEMA_ID_RE.fullmatch(
        jira_schema_id
    ):
        raise HTTPException(422, "jira_schema_id must be a digit string.")


def _account_is_expired(expires_at) -> bool:
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return expires_at < now


async def _load_account(session, account_id, *, expect: str, endpoint_id) -> dict:
    """Load the bound, active ServiceAccount (PAT decrypted) or raise 404.

    ``expect`` is ``"source"`` or ``"target"``; the account must be bound to the
    matching endpoint column and (for a source) carry the source token. An
    expired or unbound account is indistinguishable from a missing one (404).
    """
    row = (
        await session.execute(_ACCOUNT_SQL, {"id": str(account_id)})
    ).mappings().first()
    if row is None or not row["is_active"] or _account_is_expired(row["expires_at"]):
        raise HTTPException(404, "The service account is not available.")
    bound_id = row["source_id"] if expect == "source" else row["target_id"]
    if bound_id is None or str(bound_id) != str(endpoint_id):
        raise HTTPException(
            404, "The service account is not bound to this endpoint."
        )
    token = (
        row["source_bearer_token"]
        if expect == "source"
        else row["target_bearer_token"]
    )
    if not token:
        raise HTTPException(404, "The service account has no Jira token.")
    account = dict(row)
    account["token"] = token
    # Never return or log the credential itself.
    account.pop("source_bearer_token", None)
    account.pop("target_bearer_token", None)
    return account


async def _live_base_url(session, *, kind, endpoint_id) -> str:
    """The bound base URL of a live connection, else 409.

    TRI317-52: a connection test sends the decrypted PAT, so it only goes to the
    URL captured at bind time — never to an endpoint URL changed afterwards.
    """
    connection = await get_connection(session, kind=kind, endpoint_id=endpoint_id)
    if connection is None:
        raise HTTPException(
            409, f"This {kind} has no Jira connection; save the {kind} first."
        )
    if connection["binding"] != "live":
        raise HTTPException(
            409, "The endpoint URL changed after this Jira connection was bound."
        )
    return connection["base_url"]


async def _default_account_change(session, payload, *, kind, endpoint_id):
    """The connection's default-account change, validated against the endpoint.

    ``None`` keeps the stored account (field omitted); otherwise a one-tuple
    with the new id or ``None``. A set account must be an active account bound
    to this endpoint, else 422 — it is never stored for a foreign endpoint.
    """
    if payload is None or "default_service_account_id" not in payload.model_fields_set:
        return None
    account_id = payload.default_service_account_id
    if account_id is None:
        return (None,)
    try:
        await _load_account(session, account_id, expect=kind, endpoint_id=endpoint_id)
    except HTTPException:
        raise HTTPException(
            422, f"The service account is not usable for this {kind}."
        ) from None
    return (account_id,)


async def _assert_target_record_permission(user, target, *, action="read") -> None:
    """The record-level gate for a Target route (WR-04).

    A Target is not itself a Core record-permission entity (there is no
    ``target`` ``record_entity_type``); its only record scope is the optional
    bound Core schema. When the Target IS schema-bound the deny-wins schema
    check applies. An UNBOUND Target has no Core record to deny against, so the
    type-level ``sync`` permission the route already declared is the only
    applicable gate — this is deliberate, not a skipped check.
    """
    if target["schema_id"] is None:
        return
    if not await check_record_permission(user, "schema", target["schema_id"], action):
        raise HTTPException(403, "Record-level permission denied for this Target.")


def _project_schemas(payload) -> list[dict]:
    """Normalize the Jira schema list to ``{jira_schema_id, name, key}`` only."""
    if payload is None:
        return []
    if isinstance(payload, dict):
        payload = payload.get("objectschemas", payload)
    if not isinstance(payload, list):
        raise JiraPullApiError(502, "Jira schema list response is malformed")
    items = []
    for schema in payload:
        if not isinstance(schema, dict) or schema.get("id") is None:
            raise JiraPullApiError(502, "Jira schema list has an invalid entry")
        items.append(
            {
                "jira_schema_id": str(schema["id"]),
                "name": schema.get("name") or "Unknown",
                "key": schema.get("objectSchemaKey") or schema.get("key"),
            }
        )
    return items


async def _list_jira_schemas(base_url: str, token: str) -> list[dict]:
    client = JiraPullClient(base_url=base_url, pat=token, transport=_make_transport())
    try:
        payload = await client.get_schemas()
    finally:
        await client.aclose()
    return _project_schemas(payload)


def register_jira_routes(ctx) -> APIRouter:
    """Build the Jira plugin router for the Core mount chain (293-07)."""
    router = APIRouter()

    @router.get(
        "/links/{entity}",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def get_links(
        entity: str,
        ids: list[str] = Query(default=[]),
        user=Depends(require_permission("sync", "read")),
    ):
        family = _FAMILIES.get(entity)
        if family is None:
            raise HTTPException(404, "Unknown link entity.")
        try:
            ordered = _parse_batch_ids(ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

        async with ctx.db() as session:
            rows: dict[uuid.UUID, Any] = {}
            for entity_id in ordered:
                row = (
                    await session.execute(family["sql"], {"id": str(entity_id)})
                ).mappings().first()
                if row is not None and row["jira_id"] is not None:
                    rows[entity_id] = row
            maps = await _load_directions(
                session, {row["source_id"] for row in rows.values()}
            )
            keys: dict[uuid.UUID, str] = {}
            if entity == "object" and rows:
                key_rows = await session.execute(
                    _OBJECT_KEYS_SQL, {"ids": list(rows)}
                )
                keys = {row[0]: row[1] for row in key_rows}

            items = []
            for entity_id in ordered:
                row = rows.get(entity_id)
                if row is None:
                    items.append(_unlinked_item(entity_id))
                    continue
                allowed = await check_record_permission(
                    user, family["record_entity_type"], row["record_id"], "read"
                )
                if not allowed:
                    # The forbidden record is reported exactly like a missing one:
                    # the caller learns nothing about it.
                    items.append(_unlinked_item(entity_id))
                    continue
                item = {
                    "id": str(entity_id),
                    "linked": True,
                    "jira_id": str(row["jira_id"]),
                    "jira_key": None,
                    "explicit_direction": None,
                    "effective_direction": None,
                    "direction_origin": None,
                    "is_detached": None,
                }
                if family["has_direction"]:
                    explicit, effective, origin = _project_direction(
                        entity, row, maps
                    )
                    item["explicit_direction"] = explicit
                    item["effective_direction"] = effective
                    item["direction_origin"] = origin
                if entity == "object":
                    item["is_detached"] = bool(row["is_detached"])
                    item["jira_key"] = keys.get(entity_id)
                items.append(item)
            return {"entity": entity, "items": items}

    @router.get(
        "/schemas/source/{source_id}",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def list_source_schemas(
        source_id: uuid.UUID,
        service_account_id: uuid.UUID = Query(...),
        user=Depends(require_permission("sync", "read")),
    ):
        async with ctx.db() as session:
            if not await check_record_permission(user, "source", source_id, "read"):
                raise HTTPException(
                    403, "Record-level permission denied for this Source."
                )
            source = (
                await session.execute(_SOURCE_SQL, {"id": str(source_id)})
            ).mappings().first()
            if source is None or source["type"] != "jira":
                raise HTTPException(404, "JIRA source not found")
            account = await _load_account(
                session, service_account_id, expect="source", endpoint_id=source_id
            )
            try:
                schemas = await _list_jira_schemas(
                    source["source_url"], account["token"]
                )
            except JiraPullAuthError as exc:
                raise HTTPException(502, f"Jira authentication failed ({exc.status_code}).") from None
            except httpx.TransportError:
                raise HTTPException(502, JIRA_UNREACHABLE) from None
            except (JiraPullApiError, ValueError):
                raise HTTPException(502, "The Jira schema list could not be read.") from None
            linked_rows = (
                await session.execute(
                    text(
                        "SELECT l.jira_schema_id, s.id, s.name "
                        "FROM plugin_jira_schema_links l "
                        "JOIN schemas s ON s.id = l.schema_id "
                        "JOIN plugin_jira_connections c ON c.id = l.connection_id "
                        "WHERE l.source_id = CAST(:s AS uuid) "
                        "AND s.source_id = l.source_id AND c.source_id = l.source_id"
                    ),
                    {"s": str(source_id)},
                )
            ).mappings().all()
            linked = {str(row["jira_schema_id"]): row for row in linked_rows}
            visible_schemas = []
            for schema in schemas:
                row = linked.get(str(schema["jira_schema_id"]))
                if row is not None:
                    if not await check_record_permission(user, "schema", row["id"], "read"):
                        continue
                    schema["core_schema_id"] = str(row["id"])
                    schema["core_schema_name"] = row["name"]
                visible_schemas.append(schema)
            return {"source_id": str(source_id), "schemas": visible_schemas}

    @router.post(
        "/schemas/{schema_id}/link",
        dependencies=[Depends(require_permission("sync", "trigger"))],
    )
    async def link_schema(
        schema_id: uuid.UUID,
        payload: SchemaLinkRequest,
        user=Depends(require_permission("sync", "trigger")),
    ):
        """Link an empty, unlinked schema of a Jira Source to a Jira schema.

        The Jira schema is read once with the chosen bound account (it must
        exist); the link is written under the Source run lock so a concurrent
        pull cannot bootstrap the same Jira schema. The next pull fills it.
        """
        _require_schema_digits(payload.jira_schema_id)
        async with ctx.db() as session:
            if not await check_record_permission(user, "schema", schema_id, "write"):
                raise HTTPException(
                    403, "Record-level permission denied for this schema."
                )
            schema = (
                await session.execute(_SCHEMA_SOURCE_SQL, {"id": str(schema_id)})
            ).mappings().first()
            if schema is None:
                raise HTTPException(404, "Schema not found.")
            source_id = schema["source_id"]
            source = (
                await session.execute(_SOURCE_SQL, {"id": str(source_id)})
            ).mappings().first()
            if source is None or source["type"] != "jira":
                raise HTTPException(409, "This schema's Source is not a Jira Source.")
            connection = await get_connection(
                session, kind="source", endpoint_id=source_id
            )
            if connection is None:
                raise HTTPException(
                    409, "This Source has no Jira connection; save the Source first."
                )
            if connection["binding"] != "live":
                raise HTTPException(
                    409,
                    "The Source URL changed after its Jira connection was bound.",
                )
            account = await _load_account(
                session, payload.service_account_id, expect="source", endpoint_id=source_id
            )
            client = JiraPullClient(
                base_url=source["source_url"],
                pat=account["token"],
                transport=_make_transport(),
            )
            try:
                await client.get_schema(payload.jira_schema_id)
            except JiraPullAuthError as exc:
                raise HTTPException(502, f"Jira authentication failed ({exc.status_code}).") from None
            except httpx.TransportError:
                raise HTTPException(502, JIRA_UNREACHABLE) from None
            except JiraPullApiError as exc:
                if exc.status_code == 404:
                    raise HTTPException(422, "Jira has no schema with this ID.") from None
                raise HTTPException(502, "The Jira schema could not be read.") from None
            finally:
                await client.aclose()

            async with source_run_lock(session, source_id) as acquired:
                if not acquired:
                    raise HTTPException(
                        409,
                        "A Jira sync is running for this Source; try again when it has finished.",
                    )
                try:
                    await link_existing_schema(
                        session,
                        source_id=source_id,
                        schema_id=schema_id,
                        connection_id=connection["id"],
                        jira_schema_id=payload.jira_schema_id,
                    )
                except PluginTaskError as exc:
                    await session.rollback()
                    raise HTTPException(409, str(exc)) from None
                await session.execute(
                    _ADOPT_DEFAULT_ACCOUNT_SQL,
                    {"account_id": str(account["id"]), "id": connection["id"]},
                )
                await session.commit()
            return {
                "schema_id": str(schema_id),
                "jira_schema_id": payload.jira_schema_id,
                "linked": True,
            }

    @router.get(
        "/schemas/source/{source_id}/adoptable",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def list_adoptable_source_schemas(
        source_id: uuid.UUID,
        user=Depends(require_permission("sync", "read")),
    ):
        """Empty, unlinked schemas of the Source a first pull may adopt (261004)."""
        async with ctx.db() as session:
            if not await check_record_permission(user, "source", source_id, "read"):
                raise HTTPException(
                    403, "Record-level permission denied for this Source."
                )
            schemas = [
                schema
                for schema in await list_adoptable_schemas(session, source_id)
                if await check_record_permission(user, "schema", schema["id"], "read")
            ]
            return {"source_id": str(source_id), "schemas": schemas}

    @router.get(
        "/schemas/target/{target_id}",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def list_target_schemas(
        target_id: uuid.UUID,
        service_account_id: uuid.UUID = Query(...),
        user=Depends(require_permission("sync", "read")),
    ):
        async with ctx.db() as session:
            target = (
                await session.execute(_TARGET_SQL, {"id": str(target_id)})
            ).mappings().first()
            if target is None or target["connector_type"] != "jira":
                raise HTTPException(404, "Jira target not found")
            await _assert_target_record_permission(user, target)
            account = await _load_account(
                session, service_account_id, expect="target", endpoint_id=target_id
            )
            try:
                schemas = await _list_jira_schemas(
                    target["target_url"], account["token"]
                )
            except JiraPullAuthError as exc:
                raise HTTPException(502, f"Jira authentication failed ({exc.status_code}).") from None
            except httpx.TransportError:
                raise HTTPException(502, JIRA_UNREACHABLE) from None
            except (JiraPullApiError, ValueError):
                raise HTTPException(502, "The Jira schema list could not be read.") from None
            return {"target_id": str(target_id), "schemas": schemas}

    @router.put(
        "/directions/{entity}/{entity_id}",
        dependencies=[Depends(require_permission("sync", "trigger"))],
    )
    async def set_direction(
        entity: str,
        entity_id: uuid.UUID,
        payload: DirectionUpdate,
        user=Depends(require_permission("sync", "trigger")),
    ):
        if entity not in DIRECTION_ENTITIES:
            raise HTTPException(404, "Unknown direction entity.")
        if payload.direction is not None and payload.direction not in SYNC_DIRECTIONS:
            raise HTTPException(
                422,
                "direction must be one of none, pull, push, both or null.",
            )
        family = _FAMILIES[entity]

        async with ctx.db() as session:
            row = (
                await session.execute(family["sql"], {"id": str(entity_id)})
            ).mappings().first()
            if row is None or row["jira_id"] is None:
                raise HTTPException(404, "No Jira link exists for this entity.")
            try:
                await assert_link_lineage(
                    session, entity, entity_id, row["source_id"]
                )
            except LinkLineageError:
                raise HTTPException(404, "No Jira link exists for this entity.") from None
            if not await check_record_permission(
                user, family["record_entity_type"], row["record_id"], "write"
            ):
                raise HTTPException(
                    403, "Record-level permission denied for this entity."
                )
            if entity == "attribute" and payload.direction != "pull":
                structure = (
                    await session.execute(
                        text("SELECT structure FROM object_type_attributes WHERE id = CAST(:id AS uuid)"),
                        {"id": str(entity_id)},
                    )
                ).scalar()
                if is_jira_metadata(structure):
                    raise HTTPException(409, "Jira-managed metadata attributes are pull-only.")
            await session.execute(
                _UPDATE_DIRECTION_SQL[entity],
                {"direction": payload.direction, "id": str(entity_id)},
            )
            await session.commit()
            maps = await _load_directions(session, {row["source_id"]})
            explicit, effective, origin = _project_direction(entity, row, maps)
            # D-08: a pull direction protects the attribute in Core for everyone
            # (and leaving pull lifts it again) in the SAME request, so the lock
            # is never a second, eventual step. Only this plugin's own markers
            # are touched; a foreign owner is reported as skipped.
            protection = await reconcile_write_protection(
                ctx, session, schema_id=row["schema_id"], user=user
            )
            return {
                "id": str(entity_id),
                "entity": entity,
                "explicit_direction": explicit,
                "effective_direction": effective,
                "direction_origin": origin,
                "write_protection": protection,
            }

    @router.put(
        "/objects/{object_id}/detach",
        dependencies=[Depends(require_permission("sync", "trigger"))],
    )
    async def set_object_detach(
        object_id: uuid.UUID,
        payload: DetachUpdate,
        user=Depends(require_permission("sync", "trigger")),
    ):
        async with ctx.db() as session:
            row = (
                await session.execute(_OBJECT_LINK_SQL, {"id": str(object_id)})
            ).mappings().first()
            if row is None or row["jira_id"] is None:
                raise HTTPException(404, "No Jira link exists for this object.")
            if not await check_record_permission(
                user, "object", row["object_id"], "write"
            ):
                raise HTTPException(
                    403, "Record-level permission denied for this object."
                )
            if await is_stale_binding(session, row["connection_id"], object_id):
                raise HTTPException(
                    409,
                    "The Jira object binding is stale for this Source.",
                )
            new_value = bool(payload.detached)
            await session.execute(
                _UPDATE_DETACH_SQL,
                {"detached": new_value, "id": str(object_id)},
            )
            await session.commit()
            # D-11: detaching releases this plugin's write-protection for the
            # object in the SAME request, so its pull-only values become
            # editable; re-attaching removes the release again.
            release = await set_object_write_protection_release(
                ctx, object_id, released=new_value, user=user
            )
            return {
                "id": str(object_id),
                "jira_id": str(row["jira_id"]),
                "is_detached": new_value,
                "write_protection_release": release,
            }

    @router.get(
        "/connections/source/{source_id}",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def get_source_connection(
        source_id: uuid.UUID,
        user=Depends(require_permission("sync", "read")),
    ):
        async with ctx.db() as session:
            if not await check_record_permission(user, "source", source_id, "read"):
                raise HTTPException(
                    403, "Record-level permission denied for this Source."
                )
            source = (
                await session.execute(_SOURCE_SQL, {"id": str(source_id)})
            ).mappings().first()
            if source is None or source["type"] != "jira":
                raise HTTPException(404, "JIRA source not found")
            connection = await get_connection(
                session, kind="source", endpoint_id=source_id
            )
            if connection is None:
                raise HTTPException(
                    404, "No Jira connection exists for this Source."
                )
            return {"connection": connection}

    @router.put(
        "/connections/source/{source_id}",
        dependencies=[Depends(require_permission("sync", "trigger"))],
    )
    async def upsert_source_connection(
        source_id: uuid.UUID,
        payload: ConnectionUpsert | None = None,
        user=Depends(require_permission("sync", "trigger")),
    ):
        strategy = (
            payload.default_conflict_strategy if payload is not None else None
        )
        async with ctx.db() as session:
            if not await check_record_permission(user, "source", source_id, "write"):
                raise HTTPException(
                    403, "Record-level permission denied for this Source."
                )
            service_account = await _default_account_change(
                session, payload, kind="source", endpoint_id=source_id
            )
            connection, created = await upsert_connection(
                session,
                kind="source",
                endpoint_id=source_id,
                default_conflict_strategy=strategy,
                service_account=service_account,
            )
            await session.commit()
            return {"connection": connection, "created": created}

    @router.post(
        "/connections/source/{source_id}/test",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def test_source_connection(
        source_id: uuid.UUID,
        payload: ConnectionTestRequest,
        user=Depends(require_permission("sync", "read")),
    ):
        _require_schema_digits(payload.jira_schema_id)
        async with ctx.db() as session:
            if not await check_record_permission(user, "source", source_id, "read"):
                raise HTTPException(
                    403, "Record-level permission denied for this Source."
                )
            source = (
                await session.execute(_SOURCE_SQL, {"id": str(source_id)})
            ).mappings().first()
            if source is None or source["type"] != "jira":
                raise HTTPException(404, "JIRA source not found")
            account = await _load_account(
                session,
                payload.service_account_id,
                expect="source",
                endpoint_id=source_id,
            )
            base_url = await _live_base_url(
                session, kind="source", endpoint_id=source_id
            )
            return await run_connection_test(
                base_url,
                account["token"],
                jira_schema_id=payload.jira_schema_id,
                transport=_make_transport(),
            )

    @router.get(
        "/connections/target/{target_id}",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def get_target_connection(
        target_id: uuid.UUID,
        user=Depends(require_permission("sync", "read")),
    ):
        async with ctx.db() as session:
            target = (
                await session.execute(_TARGET_SQL, {"id": str(target_id)})
            ).mappings().first()
            if target is None or target["connector_type"] != "jira":
                raise HTTPException(404, "Jira target not found")
            await _assert_target_record_permission(user, target)
            connection = await get_connection(
                session, kind="target", endpoint_id=target_id
            )
            if connection is None:
                raise HTTPException(
                    404, "No Jira connection exists for this Target."
                )
            return {"connection": connection}

    @router.put(
        "/connections/target/{target_id}",
        dependencies=[Depends(require_permission("sync", "trigger"))],
    )
    async def upsert_target_connection(
        target_id: uuid.UUID,
        payload: ConnectionUpsert | None = None,
        user=Depends(require_permission("sync", "trigger")),
    ):
        strategy = (
            payload.default_conflict_strategy if payload is not None else None
        )
        async with ctx.db() as session:
            target = (
                await session.execute(_TARGET_SQL, {"id": str(target_id)})
            ).mappings().first()
            if target is None or target["connector_type"] != "jira":
                raise HTTPException(404, "Jira target not found")
            await _assert_target_record_permission(user, target, action="write")
            service_account = await _default_account_change(
                session, payload, kind="target", endpoint_id=target_id
            )
            connection, created = await upsert_connection(
                session,
                kind="target",
                endpoint_id=target_id,
                default_conflict_strategy=strategy,
                service_account=service_account,
            )
            await session.commit()
            return {"connection": connection, "created": created}

    @router.post(
        "/connections/target/{target_id}/test",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def test_target_connection(
        target_id: uuid.UUID,
        payload: ConnectionTestRequest,
        user=Depends(require_permission("sync", "read")),
    ):
        _require_schema_digits(payload.jira_schema_id)
        async with ctx.db() as session:
            target = (
                await session.execute(_TARGET_SQL, {"id": str(target_id)})
            ).mappings().first()
            if target is None or target["connector_type"] != "jira":
                raise HTTPException(404, "Jira target not found")
            await _assert_target_record_permission(user, target)
            account = await _load_account(
                session,
                payload.service_account_id,
                expect="target",
                endpoint_id=target_id,
            )
            base_url = await _live_base_url(
                session, kind="target", endpoint_id=target_id
            )
            return await run_connection_test(
                base_url,
                account["token"],
                jira_schema_id=payload.jira_schema_id,
                transport=_make_transport(),
            )

    @router.get(
        "/schemas/{schema_id}/sync-status",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def get_schema_sync_status(
        schema_id: uuid.UUID,
        user=Depends(require_permission("sync", "read")),
    ):
        async with ctx.db() as session:
            if not await check_record_permission(user, "schema", schema_id, "read"):
                raise HTTPException(
                    403, "Record-level permission denied for this Schema."
                )
            status = await load_schema_sync_status(session, schema_id, user)
            if status is None:
                raise HTTPException(404, "Schema not found")
            return status

    @router.get(
        "/schemas/target/{target_id}/{jira_schema_id}/object-type-count",
        dependencies=[Depends(require_permission("sync", "read"))],
    )
    async def get_target_object_type_count(
        target_id: uuid.UUID,
        jira_schema_id: str,
        service_account_id: uuid.UUID = Query(...),
        user=Depends(require_permission("sync", "read")),
    ):
        _require_schema_digits(jira_schema_id)
        async with ctx.db() as session:
            target = (
                await session.execute(_TARGET_SQL, {"id": str(target_id)})
            ).mappings().first()
            if target is None or target["connector_type"] != "jira":
                raise HTTPException(404, "Jira target not found")
            await _assert_target_record_permission(user, target)
            account = await _load_account(
                session,
                service_account_id,
                expect="target",
                endpoint_id=target_id,
            )
            try:
                result = await count_destination_object_types(
                    target["target_url"],
                    account["token"],
                    jira_schema_id,
                    transport=_make_transport(),
                )
            except JiraPullAuthError as exc:
                raise HTTPException(
                    502, f"Jira authentication failed ({exc.status_code})."
                ) from None
            except httpx.TransportError:
                raise HTTPException(502, JIRA_UNREACHABLE) from None
            except (JiraPullApiError, ValueError):
                raise HTTPException(
                    502, "The Jira object type list could not be read."
                ) from None
            return result

    @router.get(
        "/conflicts",
        dependencies=[Depends(require_permission("conflicts", "read"))],
    )
    async def get_conflicts(
        status: str = Query("pending"),
        direction: str | None = Query(None),
        schema_id: uuid.UUID | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user=Depends(require_permission("conflicts", "read")),
    ):
        if status not in RESOLUTION_STATUSES:
            raise HTTPException(422, "Unknown conflict status.")
        if direction is not None and direction not in ("pull", "push"):
            raise HTTPException(422, "direction must be pull or push.")
        async with ctx.db() as session:
            return await list_conflicts(
                session,
                status=status,
                direction=direction,
                schema_id=schema_id,
                limit=limit,
                offset=offset,
                user=user,
            )

    @router.get(
        "/conflicts/count",
        dependencies=[Depends(require_permission("conflicts", "read"))],
    )
    async def get_conflict_count(
        user=Depends(require_permission("conflicts", "read")),
    ):
        async with ctx.db() as session:
            return await count_pending_conflicts(session, user=user)

    @router.get(
        "/conflicts/object-names",
        dependencies=[
            Depends(require_permission("conflicts", "read")),
            # R5-03: naming Objects by UUID is a Core read, so the caller must
            # ALSO hold this resource permission; conflicts:read alone would let
            # a role enumerate names the Core API would deny it.
            Depends(require_permission("objects", "read")),
        ],
    )
    async def get_conflict_object_names(
        ids: list[str] = Query(default=[]),
        user=Depends(require_permission("conflicts", "read")),
    ):
        try:
            ordered = _parse_batch_ids(ids)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if not ordered:
            raise HTTPException(422, "At least one id must be requested.")
        async with ctx.db() as session:
            return await list_object_names(session, ids=ordered, user=user)

    @router.post(
        "/conflicts/{conflict_id}/resolve",
        dependencies=[Depends(require_permission("conflicts", "manage"))],
    )
    async def resolve_conflict_route(
        conflict_id: uuid.UUID,
        payload: ResolveConflict,
        user=Depends(require_permission("conflicts", "manage")),
    ):
        async with ctx.db() as session:
            row = (
                await session.execute(
                    _CONFLICT_OBJECT_SQL, {"id": str(conflict_id)}
                )
            ).first()
            object_id = None if row is None else row[0]
            if object_id is None or not await check_record_permission(
                user, "object", object_id, "write"
            ):
                raise HTTPException(404, "Conflict not found.")
            try:
                return await resolve_conflict(
                    ctx,
                    session,
                    conflict_id=conflict_id,
                    resolution=payload.resolution,
                    version=payload.version,
                    service_account_id=payload.service_account_id,
                    user=user,
                    transport=_make_transport(),
                )
            except HTTPException:
                raise
            except JiraPullAuthError as exc:
                raise HTTPException(
                    502, f"Jira authentication failed ({exc.status_code})."
                ) from None
            except httpx.TransportError:
                raise HTTPException(502, JIRA_UNREACHABLE) from None
            except (JiraPullApiError, ValueError):
                raise HTTPException(502, "The conflict could not be resolved.") from None

    return router
