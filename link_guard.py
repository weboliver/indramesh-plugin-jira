"""Read-only stale-binding predicate for the Jira plugin (Phase 292-03, D-04).

A Source URL can be repointed to a different Jira instance and then changed back.
The URL alone therefore proves nothing: only a persisted URL *revision* can tell a
reverted-but-unchanged URL from a genuinely unchanged binding. ``is_stale_binding``
compares the current Core Source URL/revision against BOTH the connection's bind
snapshot and the object link's bind snapshot, and fails closed on anything it
cannot verify:

  * missing connection/link row,
  * a target-bound connection (no Source, no revision),
  * a NULL revision on either side,
  * a Core-lineage mismatch (the Object's Schema Source, the link's ``source_id``
    and the connection's ``source_id`` must all agree — a check the foreign keys
    cannot express across Core tables),
  * a URL mismatch between ``sources.source_url`` and the connection ``base_url``
    (normalized identically on both sides),
  * any revision mismatch.

This module is READ-ONLY and imports nothing from ``app.*`` (the plugin package
AST gate permits only ``indramesh.sdk`` plus third-party/stdlib imports). It is the
predicate 293/294 MUST call at plugin sync-write admission and before any
link-dependent commit; it does not itself write or dispatch anything.

Scope: only Object-link lineage is guarded here. The composite FK
``(connection_id, source_id) -> plugin_jira_connections (id, source_id)`` on the
other seven link tables (schema, object type, attribute, status, reference type,
comment, attachment) proves the connection belongs to the link's ``source_id``, NOT
that the linked Core entity belongs to that Source. Verifying that the entity's
owning Schema/Source equals the link's ``source_id`` is a hard admission obligation
of every 293/294 writer for those tables.

Normalization: scheme and host are lower-cased and a trailing path slash is
dropped, so ``https://Jira.Example.test/`` and ``https://jira.example.test`` compare
equal. A stamped bind must use the same normalization.
"""

import uuid
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import text

# One consistent read: current Core Source snapshot + connection bind snapshot +
# Object link bind snapshot + the Object -> Schema -> Source lineage.
_STALE_QUERY = text(
    """
    SELECT
        s.source_url                AS core_source_url,
        s.source_url_revision       AS core_revision,
        c.base_url                  AS connection_base_url,
        c.bound_source_url_revision AS connection_revision,
        c.source_id                 AS connection_source_id,
        c.target_id                 AS connection_target_id,
        l.source_id                 AS link_source_id,
        l.bound_source_url_revision AS link_revision,
        sc.source_id                AS object_source_id
    FROM plugin_jira_connections AS c
    JOIN plugin_jira_object_links AS l ON l.connection_id = c.id
    JOIN objects AS o ON o.id = l.object_id
    JOIN schemas AS sc ON sc.id = o.schema_id
    LEFT JOIN sources AS s ON s.id = c.source_id
    WHERE c.id = CAST(:connection_id AS uuid)
      AND l.object_id = CAST(:object_id AS uuid)
    """
)


def normalize_url(url: str | None) -> str | None:
    """Canonical form for Source URL / connection ``base_url`` comparison."""
    if url is None:
        return None
    stripped = url.strip()
    if not stripped:
        return None
    try:
        parts = urlsplit(stripped)
    except ValueError:
        return None
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


async def is_stale_binding(session, connection_id, object_id) -> bool:
    """Return ``True`` unless ``object_id``'s binding to ``connection_id`` is live.

    The Source is derived from the connection (never passed in), so a caller
    cannot accidentally pair the wrong Source with a connection. Any condition this
    predicate cannot positively verify makes it fail closed with ``True``.
    """
    try:
        connection_uuid = uuid.UUID(str(connection_id))
        object_uuid = uuid.UUID(str(object_id))
    except ValueError:
        return True

    row = (
        await session.execute(
            _STALE_QUERY,
            {
                "connection_id": str(connection_uuid),
                "object_id": str(object_uuid),
            },
        )
    ).mappings().first()

    if row is None:
        return True
    # A target-bound connection is never a valid Source-side binding.
    if row["connection_target_id"] is not None:
        return True

    core_revision = row["core_revision"]
    connection_revision = row["connection_revision"]
    link_revision = row["link_revision"]
    if core_revision is None or connection_revision is None or link_revision is None:
        return True

    connection_source = row["connection_source_id"]
    link_source = row["link_source_id"]
    object_source = row["object_source_id"]
    if connection_source is None or link_source is None or object_source is None:
        return True
    if not (connection_source == link_source == object_source):
        return True

    core_url = normalize_url(row["core_source_url"])
    connection_url = normalize_url(row["connection_base_url"])
    if core_url is None or connection_url is None or core_url != connection_url:
        return True

    if int(core_revision) != int(connection_revision):
        return True
    if int(core_revision) != int(link_revision):
        return True

    return False