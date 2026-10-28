"""Source-scoped admission and run-lifetime locking for the Jira pull (JSYNC-02).

Two distinct safeguards (research pattern 1):

* :func:`admit_pull` runs inside Core's pre-persistence admission transaction. It
  takes a transaction-scoped advisory lock keyed to the SOURCE and refuses a
  start while a sibling pull of the same task type and Source is pending or
  running. A permanent, schedule-less row is a schedule TEMPLATE, never a run in
  flight, so it is excluded. Refusing here persists no Task row and Core maps the
  bounded :class:`TaskAdmissionRejected` reason to a 409.

* :func:`source_run_lock` is the RUN-lifetime lock. The admitting lock is
  transaction-scoped and releases when the admission transaction ends, so it
  cannot protect the multi-commit run. The run lock uses ``pg_try_advisory_lock``
  on a DEDICATED ``AsyncConnection`` taken from the plugin session's engine and
  holds it for the whole run — an ``AsyncSession`` returns its connection to the
  pool at every commit, so a session-level lock on it would leak onto a pooled
  connection. The same connection releases the lock in ``finally``, cancellation
  included; closing the connection is the final safety net.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from sqlalchemy import text

from indramesh.sdk import PluginTaskError, TaskAdmissionRejected

from .link_guard import normalize_url

# The two advisory-lock namespaces are deliberately distinct: the admission lock
# serializes (short) admitting transactions, while the run lock tries once and a
# held run lock must never block a new admission behind it.
_ADMISSION_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:key))")
_RUN_LOCK_TRY_SQL = text("SELECT pg_try_advisory_lock(hashtext(:key))")
_RUN_LOCK_RELEASE_SQL = text("SELECT pg_advisory_unlock(hashtext(:key))")

# A sibling is a pending/running run of the SAME sync family and Source. The
# family spans pull AND push (Pitfall 7): both write baselines/conflicts of the
# same Source. A permanent, schedule-less row IS the template a scheduled run is
# cloned FROM, so it is not "in flight" and must not block a start.
_SIBLING_RUN_SQL = text(
    "SELECT 1 FROM tasks WHERE tasktype = ANY(CAST(:family AS text[])) "
    "AND source_id = CAST(:source_id AS uuid) "
    "AND current_status IN ('PENDING', 'RUNNING') "
    "AND NOT (is_permanent AND schedule_id IS NULL) "
    "LIMIT 1"
)

# Push admission reads (DB only, never a remote call).
_SCHEMA_SOURCE_SQL = text(
    "SELECT source_id FROM schemas WHERE id = CAST(:schema_id AS uuid)"
)
_SCHEMA_LINK_SQL = text(
    "SELECT source_id, jira_schema_id FROM plugin_jira_schema_links "
    "WHERE schema_id = CAST(:schema_id AS uuid)"
)
_SOURCE_SQL = text(
    "SELECT source_url, source_url_revision FROM sources "
    "WHERE id = CAST(:source_id AS uuid)"
)
_SOURCE_CONNECTION_SQL = text(
    "SELECT id, base_url, bound_source_url_revision "
    "FROM plugin_jira_connections "
    "WHERE source_id = CAST(:source_id AS uuid) AND target_id IS NULL"
)
_PULL_ACCOUNT_SQL = text(
    "SELECT source_id, is_active, expires_at, "
    "source_bearer_token IS NOT NULL AS has_token "
    "FROM service_accounts WHERE id = CAST(:account_id AS uuid)"
)
_TARGET_SQL = text(
    "SELECT id, target_url, connector_type, is_active, schema_id FROM targets "
    "WHERE id = CAST(:target_id AS uuid)"
)
_TARGET_CONNECTION_SQL = text(
    "SELECT id, base_url FROM plugin_jira_connections "
    "WHERE target_id = CAST(:target_id AS uuid)"
)
_OBJECT_TYPE_IN_SCHEMA_SQL = text(
    "SELECT 1 FROM object_types WHERE id = CAST(:object_type_id AS uuid) "
    "AND schema_id = CAST(:schema_id AS uuid)"
)

# Export is Target-scoped and single-flight: no two exports may write the same
# Target at once. A permanent, schedule-less row is a TEMPLATE, never a run.
_EXPORT_SIBLING_SQL = text(
    "SELECT 1 FROM tasks WHERE tasktype = :tasktype "
    "AND target_id = CAST(:target_id AS uuid) "
    "AND current_status IN ('PENDING', 'RUNNING') "
    "AND NOT (is_permanent AND schedule_id IS NULL) "
    "LIMIT 1"
)


def _admission_key(source_id) -> str:
    # Shared by pull and push so their admitting transactions serialize together.
    return f"jira.sync:admit:{source_id}"


def _run_key(source_id) -> str:
    # One run lock for a Source (Pitfall 7): a pull and a push of the same Source
    # can never overlap.
    return f"jira.sync:run:{source_id}"


def sync_task_family(tasktype: str | None) -> tuple[str, ...]:
    """The pull/push task literals sharing one task-type prefix.

    Derived from the task's OWN prefix so a re-namespaced test node
    (``<node>.sync-pull``) matches its sibling (``<node>.sync-push``) exactly
    like the production ``jira`` prefix.
    """
    if not tasktype or "." not in tasktype:
        return (tasktype,) if tasktype else ()
    prefix = tasktype.split(".", 1)[0]
    return (f"{prefix}.sync-pull", f"{prefix}.sync-push")


async def _sibling_exists(session, task_data, source_id):
    family = list(sync_task_family(getattr(task_data, "tasktype", None)))
    if not family:
        return None
    return (
        await session.execute(
            _SIBLING_RUN_SQL,
            {"family": family, "source_id": str(source_id)},
        )
    ).first()


async def admit_pull(session, task_data) -> None:
    """Refuse a pull that is already running or can only fail at run time.

    The transaction-scoped lock closes the read-then-insert race between two
    concurrent admits: the second one waits for the first admitting transaction
    (which inserts the Task row) and then sees it. Every precondition the run
    checks before its first remote call is mirrored here as a pure DB read, so a
    doomed pull persists no Task row and Core maps the reason to a 409. Schema
    resolution uses the run's own read-only planner. The runner re-checks all of
    it because the URL, links or account can change between admission and run.
    """
    # Imported here: pull.py imports this module for its run lock.
    from .pull import _load_connection, _plan_scopes, _validate_source_binding

    source_id = getattr(task_data, "source_id", None)
    if source_id is None:
        raise TaskAdmissionRejected(
            "A Jira sync pull requires a Source scope."
        )

    await session.execute(_ADMISSION_LOCK_SQL, {"key": _admission_key(source_id)})
    if await _sibling_exists(session, task_data, source_id) is not None:
        raise TaskAdmissionRejected(
            "Another Jira sync is already pending or running for this Source."
        )

    connection = await _load_connection(session, source_id)
    if connection is None:
        raise TaskAdmissionRejected(
            "This Source has no Jira connection; set it up under Jira "
            "connections first."
        )
    try:
        _validate_source_binding(connection)
    except PluginTaskError:
        raise TaskAdmissionRejected(
            "The Source URL changed after its Jira connection was bound; "
            "re-bind the Jira connection first."
        ) from None

    account_id = getattr(task_data, "service_account_id", None)
    if account_id is None:
        raise TaskAdmissionRejected(
            "A Jira sync pull requires a service account bound to this Source."
        )
    account = (
        await session.execute(_PULL_ACCOUNT_SQL, {"account_id": str(account_id)})
    ).mappings().first()
    if account is None or str(account["source_id"]) != str(source_id):
        raise TaskAdmissionRejected(
            "The selected service account is not bound to this Source."
        )
    if not account["is_active"]:
        raise TaskAdmissionRejected("The selected service account is inactive.")
    if account["expires_at"] is not None and account["expires_at"] < datetime.now(
        timezone.utc
    ).replace(tzinfo=None):
        raise TaskAdmissionRejected("The selected service account is expired.")
    if not account["has_token"]:
        raise TaskAdmissionRejected(
            "The selected service account has no Jira token; add one under "
            "service accounts first."
        )

    parameters = getattr(task_data, "plugin_parameters", None) or {}
    jira_schema_id = (
        parameters.get("jira_schema_id") if isinstance(parameters, dict) else None
    )
    try:
        await _plan_scopes(
            session,
            connection,
            getattr(task_data, "schema_id", None),
            jira_schema_id,
            parameters.get("target_schema_id") or None
            if isinstance(parameters, dict)
            else None,
        )
    except PluginTaskError as exc:
        raise TaskAdmissionRejected(str(exc)) from None


async def admit_push(session, task_data) -> None:
    """Refuse a push that cannot safely reach exactly one Jira instance.

    Every check is a pure DB read inside the admitting transaction; a rejection
    persists no Task row and Core maps the bounded reason to a 409. The runner
    re-checks all of it because the URL can change between admission and run.
    """
    schema_id = getattr(task_data, "schema_id", None)
    target_id = getattr(task_data, "target_id", None)
    if schema_id is None:
        raise TaskAdmissionRejected("A Jira sync push requires a schema scope.")
    if target_id is None:
        raise TaskAdmissionRejected("A Jira sync push requires a Target scope.")

    schema_row = (
        await session.execute(_SCHEMA_SOURCE_SQL, {"schema_id": str(schema_id)})
    ).mappings().first()
    if schema_row is None:
        raise TaskAdmissionRejected("The selected Core schema does not exist.")
    source_id = schema_row["source_id"]

    await session.execute(_ADMISSION_LOCK_SQL, {"key": _admission_key(source_id)})

    if await _sibling_exists(session, task_data, source_id) is not None:
        raise TaskAdmissionRejected(
            "Another Jira sync is already pending or running for this Source."
        )

    link = (
        await session.execute(_SCHEMA_LINK_SQL, {"schema_id": str(schema_id)})
    ).mappings().first()
    if link is None or link["jira_schema_id"] is None:
        raise TaskAdmissionRejected(
            "The schema is not linked to a Jira schema; link the schema by "
            "pulling first; use jira.sync-export for an empty Jira schema."
        )

    source_row = (
        await session.execute(_SOURCE_SQL, {"source_id": str(source_id)})
    ).mappings().first()
    source_conn = (
        await session.execute(
            _SOURCE_CONNECTION_SQL, {"source_id": str(source_id)}
        )
    ).mappings().first()
    if source_row is None or source_conn is None:
        raise TaskAdmissionRejected(
            "The Source has no bound Jira connection; bind it before pushing."
        )
    if not _binding_live(
        source_row["source_url"],
        source_row["source_url_revision"],
        source_conn["base_url"],
        source_conn["bound_source_url_revision"],
    ):
        raise TaskAdmissionRejected(
            "The Source URL changed after this Jira connection was bound."
        )

    target = (
        await session.execute(_TARGET_SQL, {"target_id": str(target_id)})
    ).mappings().first()
    if target is None:
        raise TaskAdmissionRejected("The push Target does not exist.")
    if target["connector_type"] != "jira":
        raise TaskAdmissionRejected("The push Target is not a Jira connector.")
    if not target["is_active"]:
        raise TaskAdmissionRejected("The push Target is inactive.")
    if target["schema_id"] is not None and str(target["schema_id"]) != str(
        schema_id
    ):
        raise TaskAdmissionRejected(
            "The Target is bound to a different Core schema."
        )

    target_conn = (
        await session.execute(
            _TARGET_CONNECTION_SQL, {"target_id": str(target_id)}
        )
    ).mappings().first()
    if target_conn is None:
        raise TaskAdmissionRejected(
            "The Target has no bound Jira connection; bind it before pushing."
        )
    target_url = normalize_url(target["target_url"])
    if target_url is None or target_url != normalize_url(target_conn["base_url"]):
        raise TaskAdmissionRejected(
            "The Target URL changed after this Jira connection was bound."
        )

    instances = {
        normalize_url(target["target_url"]),
        normalize_url(target_conn["base_url"]),
        normalize_url(source_conn["base_url"]),
        normalize_url(source_row["source_url"]),
    }
    if None in instances or len(instances) != 1:
        raise TaskAdmissionRejected(
            "The push Target and the schema's Source are on different Jira "
            "instances."
        )

    parameters = getattr(task_data, "plugin_parameters", None) or {}
    object_type_id = (
        parameters.get("object_type_id")
        if isinstance(parameters, dict)
        else None
    )
    if object_type_id:
        scope_row = (
            await session.execute(
                _OBJECT_TYPE_IN_SCHEMA_SQL,
                {
                    "object_type_id": str(object_type_id),
                    "schema_id": str(schema_id),
                },
            )
        ).first()
        if scope_row is None:
            raise TaskAdmissionRejected(
                "The requested ObjectType is not part of the pushed schema."
            )


async def admit_export(session, task_data) -> None:
    """Refuse an export that cannot safely write exactly one empty Jira schema.

    Target-scoped and single-flight (JSYNC-05/JSYNC-12): the destination
    emptiness check is an HTTP call and therefore belongs to the runner, while
    every DB check that must hold before persisting a Task is done here. The
    transaction-scoped lock keyed by the Target closes the read-then-insert race
    between two concurrent admits.
    """
    schema_id = getattr(task_data, "schema_id", None)
    target_id = getattr(task_data, "target_id", None)
    if schema_id is None:
        raise TaskAdmissionRejected("A Jira sync export requires a schema scope.")
    if target_id is None:
        raise TaskAdmissionRejected("A Jira sync export requires a Target scope.")

    parameters = getattr(task_data, "plugin_parameters", None) or {}
    jira_schema_id = (
        parameters.get("jira_schema_id") if isinstance(parameters, dict) else None
    )
    if not jira_schema_id:
        raise TaskAdmissionRejected(
            "A Jira sync export requires a destination jira_schema_id."
        )

    await session.execute(
        _ADMISSION_LOCK_SQL, {"key": f"jira.sync:admit-target:{target_id}"}
    )

    target = (
        await session.execute(_TARGET_SQL, {"target_id": str(target_id)})
    ).mappings().first()
    if target is None:
        raise TaskAdmissionRejected("The export Target does not exist.")
    if target["connector_type"] != "jira":
        raise TaskAdmissionRejected("The export Target is not a Jira connector.")
    if not target["is_active"]:
        raise TaskAdmissionRejected("The export Target is inactive.")
    if target["schema_id"] is not None and str(target["schema_id"]) != str(
        schema_id
    ):
        raise TaskAdmissionRejected(
            "The Target is bound to a different Core schema."
        )

    target_conn = (
        await session.execute(
            _TARGET_CONNECTION_SQL, {"target_id": str(target_id)}
        )
    ).mappings().first()
    if target_conn is None:
        raise TaskAdmissionRejected(
            "The Target has no bound Jira connection; bind it before exporting."
        )
    target_url = normalize_url(target["target_url"])
    if target_url is None or target_url != normalize_url(target_conn["base_url"]):
        raise TaskAdmissionRejected(
            "The Target URL changed after this Jira connection was bound."
        )

    sibling = (
        await session.execute(
            _EXPORT_SIBLING_SQL,
            {
                "tasktype": getattr(task_data, "tasktype", None),
                "target_id": str(target_id),
            },
        )
    ).first()
    if sibling is not None:
        raise TaskAdmissionRejected(
            "Another Jira export is already pending or running for this Target."
        )


def _binding_live(source_url, source_revision, connection_url, connection_revision):
    """Fail-closed URL+revision comparison shared by both admission paths."""
    core_url = normalize_url(source_url)
    connection = normalize_url(connection_url)
    if core_url is None or connection is None or core_url != connection:
        return False
    if source_revision is None or connection_revision is None:
        return False
    return int(source_revision) == int(connection_revision)


@asynccontextmanager
async def source_run_lock(session, source_id) -> AsyncIterator[bool]:
    """Hold a per-Source run-lifetime advisory lock on a dedicated connection.

    Yields ``True`` when this run owns the Source and ``False`` when another run
    already holds it. The lock is released on every exit path, including
    cancellation, using the same pinned connection it was taken on.
    """
    source_key = _run_key(uuid.UUID(str(source_id)))
    engine = session.bind
    connection = await engine.connect()
    acquired = False
    try:
        result = await connection.execute(_RUN_LOCK_TRY_SQL, {"key": source_key})
        acquired = bool(result.scalar())
        yield acquired
    finally:
        if acquired:
            try:
                await connection.execute(
                    _RUN_LOCK_RELEASE_SQL, {"key": source_key}
                )
            except Exception:  # noqa: BLE001 - close() below still releases it
                pass
        await connection.close()
