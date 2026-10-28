"""Plugin-owned page-checkpoint transaction for the Jira pull (293-02, D-02/D-03).

A completed Jira page commits exactly one plugin transaction: the pending Object
links, the page's seen Jira ids and the advanced ``last_page``. Core SDK writes
stay separate commits (D-01): if the process dies between an SDK create and this
commit, the Core row is already durable, the page is not checkpointed, and the
replay re-reads the deterministic Core id instead of creating a duplicate.

The transaction re-checks the live Source URL/revision before it writes
(``link_guard`` normalization), so a page can never be checkpointed against a
Source whose binding changed mid-run. Object links are inserted with
``ON CONFLICT (object_id) DO NOTHING``: a replayed page neither duplicates nor
fails on an already-linked object.

Imports only the public ``indramesh.sdk`` surface plus third-party/stdlib (the
plugin AST gate); it never imports ``app.*``.
"""

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from indramesh.sdk import PluginTaskError

from .conflicts import build_snapshot, close_conflict, upsert_pending
from .link_guard import normalize_url

# Live Source + connection bind snapshot (a fresh read at commit time, never the
# run-start snapshot pull.py validated earlier).
_LIVE_BINDING_SQL = text(
    """
    SELECT s.source_url                AS core_source_url,
           s.source_url_revision       AS core_revision,
           c.base_url                  AS connection_base_url,
           c.bound_source_url_revision AS connection_revision,
           c.source_id                 AS connection_source_id
    FROM plugin_jira_connections AS c
    LEFT JOIN sources AS s ON s.id = c.source_id
    WHERE c.id = CAST(:connection_id AS uuid)
    """
)

_INSERT_OBJECT_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_object_links
        (object_id, source_id, connection_id, jira_object_id,
         is_detached, bound_source_url_revision)
    VALUES (CAST(:object_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_object_id,
            false, CAST(:revision AS bigint))
    ON CONFLICT (object_id) DO NOTHING
    """
)

_CHECKPOINT_ROW_SQL = text(
    """
    SELECT seen_jira_ids
    FROM plugin_jira_sync_checkpoints
    WHERE source_id = CAST(:source_id AS uuid)
      AND object_type_id = CAST(:object_type_id AS uuid)
    """
)

_UPSERT_CHECKPOINT_SQL = text(
    """
    INSERT INTO plugin_jira_sync_checkpoints
        (id, source_id, object_type_id, connection_id, last_page,
         seen_jira_ids, retry_jira_ids, run_started_at, status, updated_at)
    VALUES (CAST(:id AS uuid), CAST(:source_id AS uuid),
            CAST(:object_type_id AS uuid), CAST(:connection_id AS uuid),
            :last_page, CAST(:seen_jira_ids AS json), CAST(:retry_jira_ids AS json),
            CAST(:run_started_at AS timestamp), 'IN_PROGRESS', now())
    ON CONFLICT (source_id, object_type_id) DO UPDATE
        SET last_page = EXCLUDED.last_page,
            seen_jira_ids = EXCLUDED.seen_jira_ids,
            retry_jira_ids = COALESCE(
                EXCLUDED.retry_jira_ids,
                plugin_jira_sync_checkpoints.retry_jira_ids
            ),
            connection_id = EXCLUDED.connection_id,
            status = 'IN_PROGRESS',
            updated_at = now()
    """
)

# A fresh type run only resets ``run_started_at``/``seen_jira_ids`` when the
# previous run COMPLETED. An unfinished run keeps its ``run_started_at`` so the
# completion watermark cannot move forward past unprocessed changes.
_BEGIN_NEW_RUN_SQL = text(
    """
    UPDATE plugin_jira_sync_checkpoints
    SET run_started_at = now(),
        seen_jira_ids = '[]'::json,
        status = 'IN_PROGRESS',
        updated_at = now()
    WHERE source_id = CAST(:source_id AS uuid)
      AND object_type_id = CAST(:object_type_id AS uuid)
    """
)

_INSERT_CHECKPOINT_ROW_SQL = text(
    """
    INSERT INTO plugin_jira_sync_checkpoints
        (id, source_id, object_type_id, connection_id, last_page,
         seen_jira_ids, retry_jira_ids, run_started_at, status, updated_at)
    VALUES (CAST(:id AS uuid), CAST(:source_id AS uuid),
            CAST(:object_type_id AS uuid), CAST(:connection_id AS uuid),
            0, '[]'::json, '[]'::json, now(), 'IN_PROGRESS', now())
    """
)

_LOAD_CHECKPOINT_SQL = text(
    """
    SELECT last_page, seen_jira_ids, retry_jira_ids, run_started_at,
           last_successful_at, status
    FROM plugin_jira_sync_checkpoints
    WHERE source_id = CAST(:source_id AS uuid)
      AND object_type_id = CAST(:object_type_id AS uuid)
    """
)

# Appends deferred Jira ids to a type's retry channel WITHOUT touching the page
# or the completion watermark (293-05): a reference read that failed or was
# malformed is retried on a later run through the existing retry pass.
_RECORD_RETRY_SQL = text(
    """
    UPDATE plugin_jira_sync_checkpoints
    SET retry_jira_ids = CAST(:retry AS json), updated_at = now()
    WHERE source_id = CAST(:source_id AS uuid)
      AND object_type_id = CAST(:object_type_id AS uuid)
    """
)

_FINISH_TYPE_RUN_SQL = text(
    """
    UPDATE plugin_jira_sync_checkpoints
    SET last_successful_at = CAST(:run_started_at AS timestamp),
        last_page = 0,
        status = 'COMPLETED',
        updated_at = now()
    WHERE source_id = CAST(:source_id AS uuid)
      AND object_type_id = CAST(:object_type_id AS uuid)
    """
)

# One baseline row per synced ObjectValue (293-04, D-02/D-03). ``last_synced_value``
# carries the canonical value repr the three-way merge compares against; the
# legacy ``ObjectValue.last_synced_*`` columns are never written by the plugin.
_UPSERT_BASELINE_SQL = text(
    """
    INSERT INTO plugin_jira_value_sync_state
        (object_value_id, connection_id, last_synced_value, last_synced_at)
    VALUES (CAST(:object_value_id AS uuid), CAST(:connection_id AS uuid),
            CAST(:last_synced_value AS json), now())
    ON CONFLICT (object_value_id) DO UPDATE
        SET connection_id = EXCLUDED.connection_id,
            last_synced_value = EXCLUDED.last_synced_value,
            last_synced_at = now()
    """
)

# One attribute-level baseline row per (object, attribute) (294-02, D-02/D-03):
# the whole attribute's canonical value set the multi-value/reference merge
# compares against. Anchored on the Source link connection the caller passes.
_UPSERT_ATTRIBUTE_BASELINE_SQL = text(
    """
    INSERT INTO plugin_jira_attribute_sync_state
        (object_id, object_type_attribute_id, connection_id, last_synced_set,
         last_synced_at)
    VALUES (CAST(:object_id AS uuid), CAST(:object_type_attribute_id AS uuid),
            CAST(:connection_id AS uuid), CAST(:last_synced_set AS json), now())
    ON CONFLICT (object_id, object_type_attribute_id) DO UPDATE
        SET connection_id = EXCLUDED.connection_id,
            last_synced_set = EXCLUDED.last_synced_set,
            last_synced_at = now()
    """
)

_LOAD_ATTRIBUTE_BASELINES_SQL = text(
    """
    SELECT object_type_attribute_id, last_synced_set
    FROM plugin_jira_attribute_sync_state
    WHERE object_id = CAST(:object_id AS uuid)
    """
)


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _assert_live_binding(row) -> None:
    """Fail closed unless the live Source URL/revision matches the connection.

    ``normalize_url`` is the same canonicalization ``link_guard`` uses, so the
    two paths can never disagree on what "unchanged URL" means.
    """
    if row is None:
        raise PluginTaskError("The Jira connection binding is unavailable.")
    core_url = normalize_url(row["core_source_url"])
    connection_url = normalize_url(row["connection_base_url"])
    core_revision = row["core_revision"]
    connection_revision = row["connection_revision"]
    if (
        core_url is None
        or connection_url is None
        or core_url != connection_url
        or core_revision is None
        or connection_revision is None
        or int(core_revision) != int(connection_revision)
    ):
        raise PluginTaskError(
            "The Source URL changed after this Jira connection was bound."
        )


async def upsert_value_baseline(session, *, object_value_id, connection_id, canonical) -> None:
    """Upsert one ObjectValue baseline (the 293 statement, now shared, no commit).

    Public so push, the resolver and the pull all write the same row shape.
    """
    await session.execute(
        _UPSERT_BASELINE_SQL,
        {
            "object_value_id": str(object_value_id),
            "connection_id": str(connection_id),
            "last_synced_value": json.dumps({"canonical": canonical}),
        },
    )


async def upsert_attribute_baseline(
    session,
    *,
    object_id,
    object_type_attribute_id,
    connection_id,
    set_repr,
) -> None:
    """Upsert one attribute-level baseline for (object, attribute), no commit."""
    await session.execute(
        _UPSERT_ATTRIBUTE_BASELINE_SQL,
        {
            "object_id": str(object_id),
            "object_type_attribute_id": str(object_type_attribute_id),
            "connection_id": str(connection_id),
            "last_synced_set": set_repr,
        },
    )


async def load_attribute_baselines(session, object_id) -> dict:
    """``{object_type_attribute_id: canonical set repr}`` for one Object."""
    rows = (
        await session.execute(
            _LOAD_ATTRIBUTE_BASELINES_SQL, {"object_id": str(object_id)}
        )
    ).mappings().all()
    return {
        uuid.UUID(str(row["object_type_attribute_id"])): json.dumps(
            row["last_synced_set"]
        )
        for row in rows
    }


async def commit_attribute_effects(
    session,
    *,
    connection_id,
    task_id=None,
    attribute_baselines=(),
    attribute_conflicts=(),
    conflict_closures=(),
) -> None:
    """Stage whole-attribute baselines, conflicts and auto-closures (294-06).

    ``attribute_baselines`` is an iterable of ``(object_id,
    object_type_attribute_id, set_repr)``. ``attribute_conflicts`` are staged
    multi-value/reference divergences routed through ``upsert_pending`` with
    ``scope="attribute"`` (the anchor is ``(object_id,
    object_type_attribute_id)``, never an ObjectValue). ``conflict_closures`` are
    pending rows the pull proved obsolete, closed as ``resolved_auto``. Never
    commits — the caller owns the transaction. Used by the page commit and by the
    standalone reference pass.
    """
    connection_uuid = uuid.UUID(str(connection_id))
    for object_id, object_type_attribute_id, set_repr in attribute_baselines:
        await upsert_attribute_baseline(
            session,
            object_id=object_id,
            object_type_attribute_id=object_type_attribute_id,
            connection_id=connection_uuid,
            set_repr=set_repr,
        )
    for conflict in attribute_conflicts:
        local_items = conflict.get("local_items") or []
        remote_items = conflict.get("remote_items") or []
        kind = conflict.get("kind", "scalar")
        cardinality = conflict.get("cardinality", "multi")
        data_type = conflict.get("data_type")
        pending = await upsert_pending(
            session,
            scope="attribute",
            object_id=conflict["object_id"],
            object_type_attribute_id=conflict["object_type_attribute_id"],
            connection_id=connection_uuid,
            direction="pull",
            task_id=task_id,
            local_snapshot=build_snapshot(kind, cardinality, data_type, local_items),
            remote_snapshot=build_snapshot(kind, cardinality, data_type, remote_items),
        )
        if conflict.get("auto_resolve"):
            # D-05: a Source default strategy resolved this NEW conflict; insert
            # and close in one transaction so no pending row is ever observable.
            await close_conflict(
                session,
                conflict_id=pending.id,
                status="resolved_auto",
                resolved_by=None,
                connection_id=connection_uuid,
                direction="pull",
                object_id=conflict["object_id"],
                details="auto-resolved: source default strategy"[:255],
            )
    for closure in conflict_closures:
        await close_conflict(
            session,
            conflict_id=closure["conflict_id"],
            status="resolved_auto",
            resolved_by=None,
            connection_id=connection_uuid,
            direction="pull",
            object_id=closure["object_id"],
            details=closure.get("details"),
        )


async def commit_completed_page(
    session,
    *,
    connection_id,
    source_id,
    object_type_id,
    page: int,
    seen_jira_ids=(),
    object_links=(),
    value_baselines=(),
    conflicts=(),
    retry_jira_ids=None,
    run_started_at=None,
    task_id=None,
    attribute_baselines=(),
    attribute_conflicts=(),
    conflict_closures=(),
) -> int:
    """Commit one completed Jira page: links + baselines + conflicts + checkpoint.

    ``object_links`` is an iterable of ``(object_id, jira_object_id)`` pairs for
    Objects created/updated earlier in this page (each already an SDK commit).
    ``seen_jira_ids`` is the Jira id of every entry the page returned.
    ``value_baselines`` is an iterable of ``(object_value_id, canonical_repr)``
    baselines to upsert; ``attribute_baselines`` is an iterable of ``(object_id,
    object_type_attribute_id, set_repr)`` attribute baselines. ``conflicts`` is an
    iterable of staged pull-conflict mappings (``object_id``/``object_value_id``/
    ``object_type_attribute_id``/``data_type``/``local_value``/``remote_value``);
    each is routed through ``conflicts.upsert_pending`` (D-03) so a re-detected
    divergence updates its one pending row and carries the running ``task_id``.
    ``attribute_conflicts`` are whole-attribute (multi-value/reference) conflicts
    and ``conflict_closures`` are pending rows proven obsolete, both routed through
    ``commit_attribute_effects`` in this same transaction.
    ``retry_jira_ids`` (when not ``None``) replaces the checkpoint's retry list;
    ``run_started_at`` seeds the row on its first insert.
    Returns the number of Object links actually inserted (0 for a replayed page).

    One transaction, committed here. A rollback leaves the earlier Core SDK
    commits in place (D-02) and the page un-checkpointed, so the next run
    reprocesses it and the keyed creates replay to the same Core ids.
    """
    connection_uuid = uuid.UUID(str(connection_id))
    source_uuid = uuid.UUID(str(source_id))
    object_type_uuid = uuid.UUID(str(object_type_id))

    live = (
        await session.execute(
            _LIVE_BINDING_SQL, {"connection_id": str(connection_uuid)}
        )
    ).mappings().first()
    _assert_live_binding(live)
    revision = live["connection_revision"]

    inserted = 0
    for object_id, jira_object_id in object_links:
        result = await session.execute(
            _INSERT_OBJECT_LINK_SQL,
            {
                "object_id": str(object_id),
                "source_id": str(source_uuid),
                "connection_id": str(connection_uuid),
                "jira_object_id": str(jira_object_id),
                "revision": revision,
            },
        )
        inserted += result.rowcount or 0

    # Baselines and conflicts are staged in the SAME plugin transaction as the
    # links and the checkpoint (D-03): a crash before this commit leaves the Core
    # value write durable but the baseline unwritten, so the replay converges.
    for object_value_id, canonical in value_baselines:
        await upsert_value_baseline(
            session,
            object_value_id=object_value_id,
            connection_id=connection_uuid,
            canonical=canonical,
        )
    await commit_attribute_effects(
        session,
        connection_id=connection_uuid,
        task_id=task_id,
        attribute_baselines=attribute_baselines,
        attribute_conflicts=attribute_conflicts,
        conflict_closures=conflict_closures,
    )
    # D-03: a re-detected divergence updates its one pending row (never a second
    # one) through the single upsert, carrying this run's task attribution.
    for conflict in conflicts:
        local_repr = conflict.get("local_value")
        remote_repr = conflict.get("remote_value")
        pending = await upsert_pending(
            session,
            scope="value",
            object_id=conflict["object_id"],
            object_value_id=conflict["object_value_id"],
            object_type_attribute_id=conflict["object_type_attribute_id"],
            connection_id=connection_uuid,
            direction="pull",
            task_id=task_id,
            local_snapshot=build_snapshot(
                "scalar",
                "single",
                conflict["data_type"],
                [] if local_repr is None else [local_repr],
            ),
            remote_snapshot=build_snapshot(
                "scalar",
                "single",
                conflict["data_type"],
                [] if remote_repr is None else [remote_repr],
            ),
        )
        if conflict.get("auto_resolve"):
            # D-05: the Source default strategy resolved this NEW conflict.
            await close_conflict(
                session,
                conflict_id=pending.id,
                status="resolved_auto",
                resolved_by=None,
                connection_id=connection_uuid,
                direction="pull",
                object_id=conflict["object_id"],
                details="auto-resolved: source default strategy"[:255],
            )

    existing = (
        await session.execute(
            _CHECKPOINT_ROW_SQL,
            {
                "source_id": str(source_uuid),
                "object_type_id": str(object_type_uuid),
            },
        )
    ).mappings().first()
    merged: list[str] = list(existing["seen_jira_ids"] or []) if existing else []
    seen = set(merged)
    for jira_id in seen_jira_ids:
        key = str(jira_id)
        if key not in seen:
            merged.append(key)
            seen.add(key)

    await session.execute(
        _UPSERT_CHECKPOINT_SQL,
        {
            "id": str(uuid.uuid4()),
            "source_id": str(source_uuid),
            "object_type_id": str(object_type_uuid),
            "connection_id": str(connection_uuid),
            "last_page": int(page),
            "seen_jira_ids": json.dumps(merged),
            "retry_jira_ids": (
                None if retry_jira_ids is None else json.dumps(list(retry_jira_ids))
            ),
            "run_started_at": (
                run_started_at
                if run_started_at is not None
                else _now_naive_utc()
            ),
        },
    )
    await session.commit()
    return inserted


async def load_or_begin_type_run(
    session, *, connection_id, source_id, object_type_id
) -> dict:
    """Return the type's resumable window, beginning a new run when appropriate.

    An unfinished run (``status='IN_PROGRESS'``) keeps ``run_started_at`` and
    ``last_page`` so the replay resumes from the exact page and the completion
    watermark cannot move past unprocessed changes (D-02/D-03). A completed run
    starts a fresh window: ``run_started_at = now()`` and an empty ``seen`` list.
    The lower bound is always ``last_successful_at`` — it only moves on completion
    (F-07). The retry list survives across runs.
    """
    connection_uuid = uuid.UUID(str(connection_id))
    source_uuid = uuid.UUID(str(source_id))
    object_type_uuid = uuid.UUID(str(object_type_id))

    params = {
        "source_id": str(source_uuid),
        "object_type_id": str(object_type_uuid),
    }
    row = (await session.execute(_LOAD_CHECKPOINT_SQL, params)).mappings().first()

    if row is None:
        await session.execute(
            _INSERT_CHECKPOINT_ROW_SQL,
            {
                "id": str(uuid.uuid4()),
                "source_id": str(source_uuid),
                "object_type_id": str(object_type_uuid),
                "connection_id": str(connection_uuid),
            },
        )
        await session.commit()
        row = (
            await session.execute(_LOAD_CHECKPOINT_SQL, params)
        ).mappings().first()

    if row["status"] == "COMPLETED":
        await session.execute(_BEGIN_NEW_RUN_SQL, params)
        await session.commit()
        row = (
            await session.execute(_LOAD_CHECKPOINT_SQL, params)
        ).mappings().first()

    return {
        "last_page": int(row["last_page"] or 0),
        "seen_jira_ids": list(row["seen_jira_ids"] or []),
        "retry_jira_ids": list(row["retry_jira_ids"] or []),
        "run_started_at": row["run_started_at"],
        "last_successful_at": row["last_successful_at"],
    }


async def record_retry_ids(
    session, *, source_id, object_type_id, jira_ids
) -> None:
    """Union ``jira_ids`` into a type's retry list, leaving page/watermark intact.

    Used by the reference pass when a remote detail read is unavailable or
    malformed: the prior reference group is preserved and the id is re-read on a
    later run through the existing retry channel (D-05/D-07). A missing
    checkpoint row (type never begun) is a no-op.
    """
    params = {
        "source_id": str(uuid.UUID(str(source_id))),
        "object_type_id": str(uuid.UUID(str(object_type_id))),
    }
    row = (await session.execute(_LOAD_CHECKPOINT_SQL, params)).mappings().first()
    if row is None:
        return
    merged: list[str] = list(row["retry_jira_ids"] or [])
    seen = set(merged)
    for jira_id in jira_ids:
        key = str(jira_id)
        if key not in seen:
            merged.append(key)
            seen.add(key)
    await session.execute(
        _RECORD_RETRY_SQL, {**params, "retry": json.dumps(merged)}
    )
    await session.commit()


async def finish_type_run(
    session, *, source_id, object_type_id, run_started_at
) -> None:
    """Mark the type complete: watermark := ``run_started_at``, clear ``last_page``.

    ``last_successful_at`` only ever advances to the instant the run began, never
    to "now", so no change that landed while the run was in flight is skipped
    (D-02). The committed Core rows stay untouched (D-01/D-03).
    """
    await session.execute(
        _FINISH_TYPE_RUN_SQL,
        {
            "source_id": str(uuid.UUID(str(source_id))),
            "object_type_id": str(uuid.UUID(str(object_type_id))),
            "run_started_at": (
                run_started_at
                if run_started_at is not None
                else _now_naive_utc()
            ),
        },
    )
    await session.commit()
