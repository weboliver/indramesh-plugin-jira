"""Opt-in Jira comment/attachment reconciliation through the connector SDK (293-06).

This is the only place the plugin writes Core media, and it does so exclusively
through the dedicated connector operations published on ``indramesh.sdk``
(``upsert_connector_comment`` / ``upsert_connector_file`` and their delete
counterparts). It writes no Core row directly and never imports an ``app.*``
module.

Contract (D-04/D-05/D-06/D-13, JSYNC-03):

* **Opt-in.** ``run_pull`` calls this only for ``include_comments_files=true``;
  otherwise zero comment/attachment GET requests are issued.
* **External + read-only.** Core stamps ``managed_by`` (the task plugin name),
  keeps ``author_id``/``uploader_id`` NULL and the external author text, so the
  persisted provenance — not the Jira link row — makes the row immutable to the
  native API. Replay is safe on the deterministic Core id derived from
  ``{source}:{object}:{Jira id}``.
* **Failed fetch is never an empty list (D-05).** A subresource list is swept
  only after its own complete, successful read. A failed list/download is
  audited and preserves the previously imported media; only an authoritative
  successful empty list may reconcile (delete) the surplus managed rows.
* **Fatal auth.** A 401/403 from the JSON list or the streamed blob aborts the
  whole task (``JiraPullAuthError`` is never downgraded to a per-item skip).
* **Detached objects untouched.** Only attached links of this Source/connection
  are read; a detached object is never fetched or mutated.
* **Pinned base.** Attachment bytes are fetched through the client's validated
  attachment path against the pinned Source URL; a forged host/credential/unrelated
  path is rejected before any request.
"""

import html
import re
import uuid

import httpx
from sqlalchemy import text

from indramesh.sdk import (
    ConnectorCommentCreate,
    ConnectorFileCreate,
    CoreDataError,
    CoreDataNotFound,
    delete_connector_comment,
    delete_connector_file,
    upsert_connector_comment,
    upsert_connector_file,
)

from .client import JiraPullApiError, JiraPullAuthError

# Attached objects of this Source/connection, as plugin links provide them.
_MEDIA_OBJECTS_SQL = text(
    """
    SELECT ol.object_id, ol.jira_object_id
    FROM plugin_jira_object_links ol
    WHERE ol.source_id = CAST(:source_id AS uuid)
      AND ol.connection_id = CAST(:connection_id AS uuid)
      AND ol.jira_object_id IS NOT NULL
      AND ol.is_detached = false
    ORDER BY ol.jira_object_id
    """
)

# The managed Core comments/files of one object, reached through the plugin link.
_COMMENT_LINKS_SQL = text(
    """
    SELECT cl.object_comment_id, cl.jira_comment_id
    FROM plugin_jira_comment_links cl
    JOIN object_comments oc ON oc.id = cl.object_comment_id
    WHERE cl.source_id = CAST(:source_id AS uuid)
      AND cl.connection_id = CAST(:connection_id AS uuid)
      AND oc.object_id = CAST(:object_id AS uuid)
      AND oc.managed_by IS NOT NULL
      AND cl.jira_comment_id IS NOT NULL
    """
)
_ATTACHMENT_LINKS_SQL = text(
    """
    SELECT al.object_file_id, al.jira_attachment_id
    FROM plugin_jira_attachment_links al
    JOIN object_files of ON of.id = al.object_file_id
    WHERE al.source_id = CAST(:source_id AS uuid)
      AND al.connection_id = CAST(:connection_id AS uuid)
      AND of.object_id = CAST(:object_id AS uuid)
      AND of.managed_by IS NOT NULL
      AND al.jira_attachment_id IS NOT NULL
    """
)

_COMMENT_LINK_DELETE_BY_JIRA_SQL = text(
    "DELETE FROM plugin_jira_comment_links "
    "WHERE source_id = CAST(:s AS uuid) AND jira_comment_id = :j"
)
_COMMENT_LINK_INSERT_SQL = text(
    """
    INSERT INTO plugin_jira_comment_links
    (object_comment_id, source_id, connection_id, jira_comment_id)
    VALUES (CAST(:cid AS uuid), CAST(:s AS uuid), CAST(:c AS uuid), :j)
    ON CONFLICT (object_comment_id)
    DO UPDATE SET jira_comment_id = EXCLUDED.jira_comment_id
    """
)
_COMMENT_LINK_DELETE_BY_ID_SQL = text(
    "DELETE FROM plugin_jira_comment_links "
    "WHERE object_comment_id = CAST(:cid AS uuid)"
)
_ATTACHMENT_LINK_DELETE_BY_JIRA_SQL = text(
    "DELETE FROM plugin_jira_attachment_links "
    "WHERE source_id = CAST(:s AS uuid) AND jira_attachment_id = :j"
)
_ATTACHMENT_LINK_INSERT_SQL = text(
    """
    INSERT INTO plugin_jira_attachment_links
    (object_file_id, source_id, connection_id, jira_attachment_id)
    VALUES (CAST(:fid AS uuid), CAST(:s AS uuid), CAST(:c AS uuid), :j)
    ON CONFLICT (object_file_id)
    DO UPDATE SET jira_attachment_id = EXCLUDED.jira_attachment_id
    """
)
_ATTACHMENT_LINK_DELETE_BY_ID_SQL = text(
    "DELETE FROM plugin_jira_attachment_links "
    "WHERE object_file_id = CAST(:fid AS uuid)"
)

_AUDIT_INSERT_SQL = text(
    """
    INSERT INTO plugin_jira_sync_audit_logs
    (id, connection_id, direction, status, entity_type, entity_id, details)
    VALUES (CAST(:id AS uuid), CAST(:c AS uuid), 'pull', :status, :et, :eid, :d)
    """
)

_HTML_BLOCK_BREAK = re.compile(r"(?i)<\s*(br|/p|/div|/li)\s*/?>")
_HTML_TAG = re.compile(r"<[^>]+>")


def _plain_text(raw) -> str | None:
    """Render a raw Jira comment body as readable plain text (parity port).

    A literal-HTML body has its block boundaries turned into newlines and its
    tags/entities removed; anything non-string is unusable.
    """
    if not isinstance(raw, str):
        return None
    rendered = _HTML_BLOCK_BREAK.sub("\n", raw)
    rendered = _HTML_TAG.sub("", rendered)
    rendered = html.unescape(rendered).strip()
    return rendered or None


def _comment_author(item: dict) -> str | None:
    actor = item.get("actor")
    name = None
    if isinstance(actor, dict):
        name = actor.get("displayName")
    elif isinstance(actor, str):
        name = actor
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()[:255]


def _attachment_author(item: dict) -> str | None:
    raw = item.get("author")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()[:255]


class _ChunkStream:
    """Adapt an async iterator of byte chunks to the SDK's ``read(size)`` stream.

    The Core file relay pulls fixed-size windows from this adapter, so at most
    one source chunk beyond the requested window is held in memory. The wrapped
    generator is never started unless the relay actually reads (a replay
    short-circuit stores no blob and issues no HTTP request).
    """

    def __init__(self, aiter) -> None:
        self._aiter = aiter
        self._buffer = bytearray()
        self._eof = False

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            async for chunk in self._aiter:
                self._buffer.extend(chunk)
            self._eof = True
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        while len(self._buffer) < size and not self._eof:
            try:
                chunk = await self._aiter.__anext__()
            except StopAsyncIteration:
                self._eof = True
                break
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data


async def _audit(session, connection_id, *, entity_type, entity_id, status, details):
    await session.execute(
        _AUDIT_INSERT_SQL,
        {
            "id": str(uuid.uuid4()),
            "c": str(connection_id),
            "status": status,
            "et": entity_type,
            "eid": None if entity_id is None else str(entity_id)[:100],
            "d": None if details is None else str(details)[:1000],
        },
    )


async def _upsert_comment_link(session, *, comment_id, source_id, connection_id, jira_id):
    params = {"s": str(source_id), "j": jira_id}
    await session.execute(_COMMENT_LINK_DELETE_BY_JIRA_SQL, params)
    await session.execute(
        _COMMENT_LINK_INSERT_SQL,
        {
            "cid": str(comment_id),
            "s": str(source_id),
            "c": str(connection_id),
            "j": jira_id,
        },
    )


async def _upsert_attachment_link(
    session, *, file_id, source_id, connection_id, jira_id
):
    await session.execute(
        _ATTACHMENT_LINK_DELETE_BY_JIRA_SQL, {"s": str(source_id), "j": jira_id}
    )
    await session.execute(
        _ATTACHMENT_LINK_INSERT_SQL,
        {
            "fid": str(file_id),
            "s": str(source_id),
            "c": str(connection_id),
            "j": jira_id,
        },
    )


async def _reconcile_comments(
    ctx, session, client, *, object_id, jira_object_id, source_id, connection_id
) -> tuple[int, int]:
    """Import/reconcile one object's comments; a failed list sweeps nothing."""
    try:
        items = await client.get_object_comments(str(jira_object_id))
    except JiraPullAuthError:
        raise
    except (JiraPullApiError, httpx.HTTPError) as exc:
        await _audit(
            session,
            connection_id,
            entity_type="object_comment",
            entity_id=str(object_id),
            status="failed",
            details=f"comment_list_failed: {str(exc)[:200]}",
        )
        await session.commit()
        return 0, 1

    imported = 0
    failed = 0
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        jira_id = str(item["id"])[:100]
        seen.add(jira_id)
        author = _comment_author(item)
        body = _plain_text(item.get("comment") or item.get("commentOutput"))
        if author is None or body is None:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_comment",
                entity_id=jira_id,
                status="failed",
                details="comment_invalid_item",
            )
            continue
        try:
            result = await upsert_connector_comment(
                ctx,
                ConnectorCommentCreate(
                    object_id=object_id,
                    external_id=jira_id,
                    body=body,
                    external_author_name=author,
                ),
            )
        except CoreDataError as exc:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_comment",
                entity_id=jira_id,
                status="failed",
                details=f"comment_import_failed: {exc.code}",
            )
            continue
        await _upsert_comment_link(
            session,
            comment_id=result.id,
            source_id=source_id,
            connection_id=connection_id,
            jira_id=jira_id,
        )
        imported += 1

    # Authoritative successful list: reconcile only THIS object's surplus managed
    # comments. A failed list returned above and never reaches this sweep.
    existing = (
        await session.execute(
            _COMMENT_LINKS_SQL,
            {
                "source_id": str(source_id),
                "connection_id": str(connection_id),
                "object_id": str(object_id),
            },
        )
    ).mappings().all()
    for link in existing:
        if link["jira_comment_id"] in seen:
            continue
        try:
            await delete_connector_comment(ctx, object_id, link["object_comment_id"])
        except CoreDataNotFound:
            pass
        except CoreDataError as exc:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_comment",
                entity_id=link["jira_comment_id"],
                status="failed",
                details=f"comment_delete_failed: {exc.code}",
            )
            continue
        await session.execute(
            _COMMENT_LINK_DELETE_BY_ID_SQL,
            {"cid": str(link["object_comment_id"])},
        )
    return imported, failed


async def _reconcile_attachments(
    ctx, session, client, *, object_id, jira_object_id, source_id, connection_id
) -> tuple[int, int]:
    """Import/reconcile one object's attachments; a failed list sweeps nothing."""
    try:
        items = await client.get_object_attachments(str(jira_object_id))
    except JiraPullAuthError:
        raise
    except (JiraPullApiError, httpx.HTTPError) as exc:
        await _audit(
            session,
            connection_id,
            entity_type="object_file",
            entity_id=str(object_id),
            status="failed",
            details=f"attachment_list_failed: {str(exc)[:200]}",
        )
        await session.commit()
        return 0, 1

    imported = 0
    failed = 0
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        jira_id = str(item["id"])[:100]
        seen.add(jira_id)
        raw_filename = item.get("filename")
        filename = str(raw_filename if raw_filename else f"attachment-{jira_id}")[:500]
        mime = item.get("mimeType")
        content_type = (
            mime if isinstance(mime, str) and mime.strip() else None
        )
        ref = item.get("url")
        if not isinstance(ref, str) or not ref.strip():
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_file",
                entity_id=jira_id,
                status="failed",
                details="attachment_missing_url",
            )
            continue

        aiter = client.stream_attachment_blob(ref)
        stream = _ChunkStream(aiter)
        try:
            result = await upsert_connector_file(
                ctx,
                ConnectorFileCreate(
                    object_id=object_id,
                    external_id=jira_id,
                    filename=filename,
                    content_type=content_type,
                    external_author_name=_attachment_author(item),
                ),
                stream,
            )
        except JiraPullAuthError:
            await aiter.aclose()
            raise
        except (JiraPullApiError, httpx.HTTPError) as exc:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_file",
                entity_id=jira_id,
                status="failed",
                details=f"attachment_download_failed: {str(exc)[:200]}",
            )
            continue
        except CoreDataError as exc:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_file",
                entity_id=jira_id,
                status="failed",
                details=f"attachment_import_failed: {exc.code}",
            )
            continue
        finally:
            await aiter.aclose()
        await _upsert_attachment_link(
            session,
            file_id=result.id,
            source_id=source_id,
            connection_id=connection_id,
            jira_id=jira_id,
        )
        imported += 1

    existing = (
        await session.execute(
            _ATTACHMENT_LINKS_SQL,
            {
                "source_id": str(source_id),
                "connection_id": str(connection_id),
                "object_id": str(object_id),
            },
        )
    ).mappings().all()
    for link in existing:
        if link["jira_attachment_id"] in seen:
            continue
        try:
            await delete_connector_file(ctx, object_id, link["object_file_id"])
        except CoreDataNotFound:
            pass
        except CoreDataError as exc:
            failed += 1
            await _audit(
                session,
                connection_id,
                entity_type="object_file",
                entity_id=link["jira_attachment_id"],
                status="failed",
                details=f"attachment_delete_failed: {exc.code}",
            )
            continue
        await session.execute(
            _ATTACHMENT_LINK_DELETE_BY_ID_SQL,
            {"fid": str(link["object_file_id"])},
        )
    return imported, failed


async def reconcile_object_media(
    ctx, session, client, *, source_id, connection_id
) -> dict:
    """Reconcile every attached object's opt-in Jira media (293-06).

    Runs only when the pull was asked to include comments/files. Detached objects
    are excluded by the link query, so they are neither fetched nor mutated.
    Comment and attachment lists are independent: one failed subresource never
    sweeps the other.
    """
    objects = (
        await session.execute(
            _MEDIA_OBJECTS_SQL,
            {"source_id": str(source_id), "connection_id": str(connection_id)},
        )
    ).mappings().all()

    comments = 0
    attachments = 0
    failed = 0
    for row in objects:
        imported, sub_failed = await _reconcile_comments(
            ctx,
            session,
            client,
            object_id=row["object_id"],
            jira_object_id=row["jira_object_id"],
            source_id=source_id,
            connection_id=connection_id,
        )
        comments += imported
        failed += sub_failed
        imported, sub_failed = await _reconcile_attachments(
            ctx,
            session,
            client,
            object_id=row["object_id"],
            jira_object_id=row["jira_object_id"],
            source_id=source_id,
            connection_id=connection_id,
        )
        attachments += imported
        failed += sub_failed
        await session.commit()

    return {
        "objects": len(objects),
        "comments": comments,
        "attachments": attachments,
        "failed": failed,
    }
