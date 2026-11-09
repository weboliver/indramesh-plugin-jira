"""Connection projection, bind/upsert, health probe and schema sync status (294-03).

There was no production path that created ``plugin_jira_connections`` rows — only
tests inserted them — so pull/push were unreachable for users (294-RESEARCH
Pattern 7). This module is that path plus the read-only status projections the
Phase 295 config editors consume (JSYNC-07/08/09).

* :func:`project_connection` / :func:`get_connection` read one endpoint's
  connection and project its binding. ``base_url`` is the normalized snapshot
  taken at bind time; a Source additionally carries the ``source_url_revision``
  stamp, so a changed-then-reverted URL still reads ``stale`` (292 D-04).
* :func:`upsert_connection` creates the connection, or updates ONLY the default
  conflict strategy of an existing live one. A stale binding is refused (409) and
  never re-stamped: relinking is out of scope (299 Phase-172 class).
* :func:`run_connection_test` probes ``/rest/api/2/myself`` then the schema list
  (and, when asked, the one selected schema) and maps every failure to exactly one
  category. It never returns the PAT or a Jira response body (T-294-06).
* :func:`load_schema_sync_status` reports, per schema, the Jira link/binding, the
  newest real pull and push runs, the open resumable checkpoints and the pending
  conflict counts (JSYNC-08).
* :func:`count_destination_object_types` backs the export pre-check (JSYNC-05).
* :func:`load_endpoint_target` reads the endpoint row's OWN current URL and
  ``config`` for the connect-time outbound guard (SESS-07, 321-05): it answers
  before the first bind, which the bind-snapshot projection cannot.

Only stdlib, ``sqlalchemy``, ``httpx`` and sibling plugin modules are imported (the
plugin AST gate); no ``app.*`` import ever appears here.
"""

import os
import re
import uuid

import httpx
from fastapi import HTTPException
from sqlalchemy import text

from indramesh.sdk import check_record_permission

from .client import JiraPullApiError, JiraPullAuthError, JiraPullClient
from .conflicts import COUNT_CAP
from .link_guard import normalize_url

# The complete, closed set of connection-test outcomes.
CONNECTION_TEST_CATEGORIES = (
    "ok",
    "auth",
    "network",
    "permission",
    "not_found",
    "error",
)

# The plugin task-type literals the status projection reads (module.py registers
# them with these names).
PULL_TASK_TYPE = "jira.sync-pull"
PUSH_TASK_TYPE = "jira.sync-push"

# A Jira schema id in a path/body is digits only (T-294-07: a client cannot steer
# the probe at an arbitrary path).
JIRA_SCHEMA_ID_RE = re.compile(r"^[0-9]{1,20}$")

# The endpoint kinds, also the projection's ``endpoint_kind`` values.
SOURCE = "source"
TARGET = "target"

# Bounded status text — the Core column is Text but the wire contract cuts it.
_ERROR_TEXT_MAX = 255

_SOURCE_ENDPOINT_SQL = text(
    "SELECT id, type, source_url, source_url_revision FROM sources "
    "WHERE id = CAST(:id AS uuid) FOR SHARE"
)
_TARGET_ENDPOINT_SQL = text(
    "SELECT id, connector_type, target_url FROM targets "
    "WHERE id = CAST(:id AS uuid) FOR SHARE"
)

# The endpoint row's OWN current URL and config — the values the connect-time
# outbound guard must read. Deliberately not ``_SOURCE_ENDPOINT_SQL`` (which also
# carries the revision stamp) and deliberately not the connection projection:
# ``get_connection`` returns ``None`` before the first bind and keeps only the
# bind snapshot ``base_url``, so it cannot answer "what is this endpoint's
# current URL and its flag".
_SOURCE_TARGET_SQL = text(
    "SELECT source_url AS url, config FROM sources WHERE id = CAST(:id AS uuid)"
)
_TARGET_TARGET_SQL = text(
    "SELECT target_url AS url, config FROM targets WHERE id = CAST(:id AS uuid)"
)

_SOURCE_CONNECTION_SQL = text(
    """
    SELECT c.id, c.source_id, c.target_id, c.base_url,
           c.bound_source_url_revision, c.default_conflict_strategy,
           c.default_service_account_id,
           c.created_at, c.updated_at,
           s.source_url AS source_url,
           s.source_url_revision AS source_revision
    FROM plugin_jira_connections c
    LEFT JOIN sources s ON s.id = c.source_id
    WHERE c.source_id = CAST(:id AS uuid)
    """
)
_TARGET_CONNECTION_SQL = text(
    """
    SELECT c.id, c.source_id, c.target_id, c.base_url,
           c.bound_source_url_revision, c.default_conflict_strategy,
           c.default_service_account_id,
           c.created_at, c.updated_at,
           t.target_url AS target_url
    FROM plugin_jira_connections c
    LEFT JOIN targets t ON t.id = c.target_id
    WHERE c.target_id = CAST(:id AS uuid)
    """
)

_INSERT_SOURCE_CONNECTION_SQL = text(
    """
    INSERT INTO plugin_jira_connections
        (id, source_id, base_url, bound_source_url_revision,
         default_conflict_strategy, default_service_account_id)
    VALUES (CAST(:id AS uuid), CAST(:source_id AS uuid), :base_url,
            CAST(:revision AS bigint), :strategy,
            CAST(:service_account_id AS uuid))
    ON CONFLICT (source_id) DO NOTHING
    RETURNING id
    """
)
_INSERT_TARGET_CONNECTION_SQL = text(
    """
    INSERT INTO plugin_jira_connections
        (id, target_id, base_url, bound_source_url_revision,
         default_conflict_strategy, default_service_account_id)
    VALUES (CAST(:id AS uuid), CAST(:target_id AS uuid), :base_url,
            NULL, :strategy, CAST(:service_account_id AS uuid))
    ON CONFLICT (target_id) DO NOTHING
    RETURNING id
    """
)

_UPDATE_STRATEGY_SQL = text(
    "UPDATE plugin_jira_connections "
    "SET default_conflict_strategy = :strategy, updated_at = now() "
    "WHERE id = CAST(:id AS uuid)"
)
_UPDATE_SERVICE_ACCOUNT_SQL = text(
    "UPDATE plugin_jira_connections "
    "SET default_service_account_id = CAST(:service_account_id AS uuid), "
    "updated_at = now() "
    "WHERE id = CAST(:id AS uuid)"
)

# --- schema sync status (JSYNC-08) -------------------------------------------

_SCHEMA_SQL = text(
    "SELECT id, source_id FROM schemas WHERE id = CAST(:id AS uuid)"
)

_SCHEMA_LINK_SQL = text(
    """
    SELECT l.jira_schema_id, l.connection_id, c.base_url,
           c.bound_source_url_revision,
           s.source_url AS source_url,
           s.source_url_revision AS source_revision
    FROM plugin_jira_schema_links l
    JOIN plugin_jira_connections c ON c.id = l.connection_id
    LEFT JOIN sources s ON s.id = c.source_id
    WHERE l.schema_id = CAST(:schema_id AS uuid)
    """
)

# A pull run belongs to the schema when it names the schema, or when it is a
# source-wide run whose ``plugin_parameters`` do not target a different Jira
# schema. ``tasks.jira_schema_id`` is NEVER read (dropped in Phase 296).
_LAST_PULL_SQL = text(
    """
    SELECT id, current_status, started_at, completed_at, error_message
    FROM tasks
    WHERE tasktype = :tasktype
      AND source_id = CAST(:source_id AS uuid)
      AND schema_id = CAST(:schema_id AS uuid)
      AND NOT (is_permanent AND schedule_id IS NULL)
    ORDER BY created_at DESC
    LIMIT 1
    """
)
_LAST_PULL_SOURCE_WIDE_SQL = text(
    """
    SELECT id, current_status, started_at, completed_at, error_message
    FROM tasks
    WHERE tasktype = :tasktype
      AND source_id = CAST(:source_id AS uuid)
      AND schema_id IS NULL
      AND (plugin_parameters->>'jira_schema_id' IS NULL
           OR plugin_parameters->>'jira_schema_id' = :linked_jira)
      AND NOT (is_permanent AND schedule_id IS NULL)
    ORDER BY created_at DESC
    LIMIT 1
    """
)
_LAST_PUSH_SQL = text(
    """
    SELECT id, current_status, started_at, completed_at, error_message
    FROM tasks
    WHERE tasktype = :tasktype
      AND schema_id = CAST(:schema_id AS uuid)
      AND NOT (is_permanent AND schedule_id IS NULL)
    ORDER BY created_at DESC
    LIMIT 1
    """
)

_OPEN_CHECKPOINTS_SQL = text(
    """
    SELECT c.object_type_id, c.last_page, c.retry_jira_ids
    FROM plugin_jira_sync_checkpoints c
    JOIN object_types ot ON ot.id = c.object_type_id
    WHERE ot.schema_id = CAST(:schema_id AS uuid)
      AND (c.status = 'IN_PROGRESS' OR json_array_length(c.retry_jira_ids) > 0)
    ORDER BY c.object_type_id
    """
)

# R3-03: candidate pending conflicts for a schema, one row per conflict. The
# per-direction count is computed in Python so each conflict's Object can be run
# through the same deny-wins record resolver the conflict list uses — a raw
# aggregate would leak counts for Objects the caller may not read.
_OPEN_CONFLICTS_SQL = text(
    """
    SELECT cf.direction, cf.object_id
    FROM plugin_jira_conflicts cf
    JOIN objects o ON o.id = cf.object_id
    WHERE o.schema_id = CAST(:schema_id AS uuid)
      AND cf.status = 'pending'
    ORDER BY cf.direction, cf.updated_at DESC, cf.id
    """
)


def _iso(value):
    return value.isoformat() if value is not None else None


def _stale_source(row, source_url_key: str, revision_key: str) -> bool:
    """``True`` unless the live Source URL/revision matches the bind snapshot."""
    core_url = normalize_url(row[source_url_key])
    connection_url = normalize_url(row["base_url"])
    revision = row[revision_key]
    bound = row["bound_source_url_revision"]
    if (
        core_url is None
        or connection_url is None
        or core_url != connection_url
        or revision is None
        or bound is None
    ):
        return True
    return int(revision) != int(bound)


def project_connection(row, *, kind):
    """Project one connection row (or ``None``) into the wire shape.

    ``binding`` is ``live`` only while the endpoint's current URL and (for a
    Source) its ``source_url_revision`` still equal the bind snapshot; anything
    else — including a changed-then-reverted URL — is ``stale``.
    """
    if row is None:
        return None
    if kind == SOURCE:
        endpoint_id = row["source_id"]
        stale = _stale_source(row, "source_url", "source_revision")
    else:
        endpoint_id = row["target_id"]
        core_url = normalize_url(row["target_url"])
        connection_url = normalize_url(row["base_url"])
        stale = (
            core_url is None or connection_url is None or core_url != connection_url
        )
    return {
        "id": str(row["id"]),
        "endpoint_kind": kind,
        "endpoint_id": str(endpoint_id),
        "base_url": row["base_url"],
        "binding": "stale" if stale else "live",
        "default_conflict_strategy": row["default_conflict_strategy"],
        "default_service_account_id": (
            str(row["default_service_account_id"])
            if row["default_service_account_id"] is not None
            else None
        ),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def get_connection(session, *, kind, endpoint_id):
    """The endpoint's connection projection, or ``None`` when none exists."""
    sql = _SOURCE_CONNECTION_SQL if kind == SOURCE else _TARGET_CONNECTION_SQL
    row = (
        await session.execute(sql, {"id": str(endpoint_id)})
    ).mappings().first()
    return project_connection(row, kind=kind)


async def load_endpoint_target(session, *, kind, endpoint_id):
    """The endpoint's OWN current ``url`` and ``config``, or ``None``.

    Reads the Source/Target row itself, which is what makes the connect-time
    outbound guard cover a FIRST bind (no ``plugin_jira_connections`` row exists
    yet, so :func:`get_connection` would answer ``None``) and a repeat PUT where
    only the strategy changes. One read feeds every call site.

    ``None`` means the endpoint row is gone. Every caller must treat that as a
    REFUSAL, never as a pass: a missing row is not an endpoint that may be
    dialled. A first bind reads exactly the same fields as a repeat PUT, because
    the row is the Source/Target itself and not the bind snapshot.
    """
    sql = _SOURCE_TARGET_SQL if kind == SOURCE else _TARGET_TARGET_SQL
    row = (
        await session.execute(sql, {"id": str(endpoint_id)})
    ).mappings().first()
    if row is None:
        return None
    return {"url": row["url"], "config": row["config"]}


async def _existing(session, *, kind, endpoint_id, strategy, service_account):
    """Apply a strategy/default-account change to an existing connection.

    ``service_account`` is ``None`` to keep the stored default account, else a
    one-tuple holding the new account id (or ``None`` to clear it). The bind
    snapshot is never re-stamped.
    """
    sql = _SOURCE_CONNECTION_SQL if kind == SOURCE else _TARGET_CONNECTION_SQL
    row = (
        await session.execute(sql, {"id": str(endpoint_id)})
    ).mappings().first()
    if row is None:
        return None
    projection = project_connection(row, kind=kind)
    if projection["binding"] != "live":
        raise HTTPException(
            409,
            "The endpoint URL changed after this Jira connection was bound.",
        )
    await session.execute(
        _UPDATE_STRATEGY_SQL, {"strategy": strategy, "id": str(row["id"])}
    )
    if service_account is not None:
        await session.execute(
            _UPDATE_SERVICE_ACCOUNT_SQL,
            {"service_account_id": _uuid_or_none(service_account[0]),
             "id": str(row["id"])},
        )
    fresh = (
        await session.execute(sql, {"id": str(endpoint_id)})
    ).mappings().first()
    return project_connection(fresh, kind=kind), False


def _uuid_or_none(value):
    return str(value) if value is not None else None


async def upsert_connection(
    session, *, kind, endpoint_id, default_conflict_strategy, service_account=None
):
    """Create or update the endpoint's connection; returns ``(projection, created)``.

    ``service_account`` follows ``_existing``: ``None`` keeps the stored default
    account, a one-tuple sets (or clears) it. The caller has already verified the
    account is bound to this endpoint.

    The endpoint is read ``FOR SHARE`` and its type validated first. An existing
    live connection only has its default conflict strategy (and ``updated_at``)
    changed; a stale one is refused with 409 and left byte-identical. A
    concurrent insert that wins the ``ON CONFLICT`` race is re-read and reported
    like any other existing connection. No commit happens here — the route owns
    the transaction.
    """
    if kind == SOURCE:
        endpoint = (
            await session.execute(_SOURCE_ENDPOINT_SQL, {"id": str(endpoint_id)})
        ).mappings().first()
        if endpoint is None or endpoint["type"] != "jira":
            raise HTTPException(404, "JIRA source not found")
        core_url = normalize_url(endpoint["source_url"])
        if core_url is None:
            raise HTTPException(422, "The Source URL is not a valid http(s) URL.")
        existing = await _existing(
            session,
            kind=kind,
            endpoint_id=endpoint_id,
            strategy=default_conflict_strategy,
            service_account=service_account,
        )
        if existing is not None:
            return existing
        inserted = (
            await session.execute(
                _INSERT_SOURCE_CONNECTION_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "source_id": str(endpoint_id),
                    "base_url": core_url,
                    "revision": endpoint["source_url_revision"],
                    "strategy": default_conflict_strategy,
                    "service_account_id": _uuid_or_none(
                        service_account[0] if service_account else None
                    ),
                },
            )
        ).first()
    else:
        endpoint = (
            await session.execute(_TARGET_ENDPOINT_SQL, {"id": str(endpoint_id)})
        ).mappings().first()
        if endpoint is None or endpoint["connector_type"] != "jira":
            raise HTTPException(404, "Jira target not found")
        core_url = normalize_url(endpoint["target_url"])
        if core_url is None:
            raise HTTPException(422, "The Target URL is not a valid http(s) URL.")
        existing = await _existing(
            session,
            kind=kind,
            endpoint_id=endpoint_id,
            strategy=default_conflict_strategy,
            service_account=service_account,
        )
        if existing is not None:
            return existing
        inserted = (
            await session.execute(
                _INSERT_TARGET_CONNECTION_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "target_id": str(endpoint_id),
                    "base_url": core_url,
                    "strategy": default_conflict_strategy,
                    "service_account_id": _uuid_or_none(
                        service_account[0] if service_account else None
                    ),
                },
            )
        ).first()

    if inserted is None:
        # A concurrent bind won the race: report its row, never a second one.
        raced = await _existing(
            session,
            kind=kind,
            endpoint_id=endpoint_id,
            strategy=default_conflict_strategy,
            service_account=service_account,
        )
        if raced is not None:
            return raced
        raise HTTPException(409, "The Jira connection could not be created.")

    row = (
        await session.execute(
            _SOURCE_CONNECTION_SQL if kind == SOURCE else _TARGET_CONNECTION_SQL,
            {"id": str(endpoint_id)},
        )
    ).mappings().first()
    return project_connection(row, kind=kind), True


# --- connection test ---------------------------------------------------------

# Phase 318 (K8S-LLM-EGRESS-01): on Kubernetes the backend/worker egress is
# default-deny, so an unreachable Jira usually means a missing egress rule.
JIRA_UNREACHABLE = "Jira could not be reached." + (
    " On Kubernetes, check that the NetworkPolicy egress admits this Jira"
    " (K8S_EGRESS_ALLOW, docs/CONFIGURATION.md)."
    if os.environ.get("INDRAMESH_RUNTIME_MODE") == "kubernetes"
    else ""
)

_TEST_MESSAGES = {
    "ok": "The Jira connection is healthy.",
    "auth": "Jira rejected the service account credentials.",
    "network": JIRA_UNREACHABLE,
    "permission": "The service account lacks the required Jira permissions.",
    "not_found": "The selected Jira schema was not found.",
    "error": "The Jira connection test failed.",
}


def _test_result(category, status_code):
    return {
        "ok": category == "ok",
        "category": category,
        "status_code": status_code,
        "message": _TEST_MESSAGES[category],
    }


async def run_connection_test(base_url, token, *, jira_schema_id=None, transport=None):
    """Probe one endpoint and classify the outcome into exactly one category.

    Probes ``/rest/api/2/myself``, the schema list and (only when a schema id is
    given) that one schema. The returned message is a fixed per-category string:
    no Jira response body and no token ever crosses this boundary (T-294-06).

    ``base_url`` must already have passed ``assert_target_allowed`` at the caller
    — ``routes.test_source_connection`` / ``test_target_connection`` guard it
    immediately before the call. This helper carries no endpoint id, so it cannot
    guard itself; never call it with an unguarded URL.
    """
    try:
        client = JiraPullClient(base_url=base_url, pat=token, transport=transport)
    except (JiraPullApiError, ValueError):
        return _test_result("error", None)

    try:
        await client.get_myself()
        await client.get_schemas()
        if jira_schema_id is not None:
            await client.get_schema(jira_schema_id)
    except httpx.TransportError:
        return _test_result("network", None)
    except JiraPullAuthError as exc:
        category = "auth" if exc.status_code == 401 else "permission"
        return _test_result(category, exc.status_code)
    except JiraPullApiError as exc:
        if jira_schema_id is not None and exc.status_code == 404:
            return _test_result("not_found", 404)
        return _test_result("error", exc.status_code)
    except ValueError:
        return _test_result("error", None)
    else:
        return _test_result("ok", 200)
    finally:
        await client.aclose()


# --- export pre-check --------------------------------------------------------


async def count_destination_object_types(
    base_url, token, jira_schema_id, *, transport=None
):
    """``{object_type_count, empty}`` for a Jira schema — the export precondition.

    ``base_url`` must already have passed ``assert_target_allowed`` at the caller
    — ``routes.get_target_object_type_count``, the only one, guards it immediately
    before the call. This helper carries no endpoint id, so it cannot guard
    itself; never call it with an unguarded URL.
    """
    client = JiraPullClient(base_url=base_url, pat=token, transport=transport)
    try:
        object_types = await client.get_object_types(jira_schema_id)
    finally:
        await client.aclose()
    count = len(object_types)
    return {"object_type_count": count, "empty": count == 0}


# --- schema sync status ------------------------------------------------------


def _run_projection(row):
    if row is None:
        return None
    error = row["error_message"]
    if error is not None:
        error = error[:_ERROR_TEXT_MAX]
    return {
        "task_id": str(row["id"]),
        "status": row["current_status"],
        "started_at": _iso(row["started_at"]),
        "completed_at": _iso(row["completed_at"]),
        "error_message": error,
    }


async def load_schema_sync_status(session, schema_id, user):
    """Per-schema link, latest real runs, open checkpoints and pending conflicts.

    Returns ``None`` for an unknown schema. Reads only the plugin link/connection
    tables and the Core ``tasks`` row's plugin parameters — never the dropped
    ``tasks.jira_schema_id`` column.

    ``user`` resolves the record permission of every conflict's Object before it
    contributes to a count (R3-03): the count matches the deny-wins-filtered
    conflict list exactly, so no count for an unreadable Object can leak. Each
    direction stops at ``COUNT_CAP`` and reports the cap through
    ``open_conflicts_capped`` so the UI can render "100+" instead of an unbounded
    (and expensive) count.
    """
    schema = (
        await session.execute(_SCHEMA_SQL, {"id": str(schema_id)})
    ).mappings().first()
    if schema is None:
        return None

    link_row = (
        await session.execute(_SCHEMA_LINK_SQL, {"schema_id": str(schema_id)})
    ).mappings().first()
    link = None
    linked_jira = None
    if link_row is not None:
        linked_jira = link_row["jira_schema_id"]
        stale = _stale_source(link_row, "source_url", "source_revision")
        link = {
            "jira_schema_id": linked_jira,
            "connection_id": str(link_row["connection_id"]),
            "binding": "stale" if stale else "live",
        }

    pull_params = {
        "tasktype": PULL_TASK_TYPE,
        "source_id": str(schema["source_id"]),
        "schema_id": str(schema_id),
        "linked_jira": linked_jira,
    }
    last_pull = _run_projection(
        (
            await session.execute(_LAST_PULL_SQL, pull_params)
        ).mappings().first()
    )
    if last_pull is None:
        source_wide = dict(pull_params)
        source_wide.pop("schema_id")
        last_pull = _run_projection(
            (
                await session.execute(_LAST_PULL_SOURCE_WIDE_SQL, source_wide)
            ).mappings().first()
        )

    last_push = _run_projection(
        (
            await session.execute(
                _LAST_PUSH_SQL,
                {"tasktype": PUSH_TASK_TYPE, "schema_id": str(schema_id)},
            )
        ).mappings().first()
    )

    checkpoints = []
    for row in (
        await session.execute(_OPEN_CHECKPOINTS_SQL, {"schema_id": str(schema_id)})
    ).mappings():
        retry_jira_ids = row["retry_jira_ids"] or []
        checkpoints.append(
            {
                "object_type_id": str(row["object_type_id"]),
                "last_page": row["last_page"],
                "retry_count": len(retry_jira_ids),
            }
        )

    conflicts = {"pull": 0, "push": 0}
    conflicts_capped = {"pull": False, "push": False}
    for row in (
        await session.execute(_OPEN_CONFLICTS_SQL, {"schema_id": str(schema_id)})
    ).mappings():
        direction = row["direction"]
        if direction not in conflicts or conflicts_capped[direction]:
            continue
        if await check_record_permission(user, "object", row["object_id"], "read"):
            conflicts[direction] += 1
            if conflicts[direction] >= COUNT_CAP:
                conflicts_capped[direction] = True
        if all(conflicts_capped.values()):
            break

    return {
        "schema_id": str(schema_id),
        "link": link,
        "last_pull": last_pull,
        "last_push": last_push,
        "open_checkpoints": checkpoints,
        "open_conflicts": conflicts,
        "open_conflicts_capped": conflicts_capped,
    }
