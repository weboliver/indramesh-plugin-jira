"""Source-scoped Jira link mapping and the shared sync-direction resolver (293-03).

Two responsibilities, both owned by this plan (F-05):

* ``effective_direction`` — the ONE pure resolver every pull pass uses
  (metadata here, objects/values/refs/media in later plans). Direction is set on
  link rows: a more specific level wins, ``None`` inherits, and a set level is
  final (an explicit ``none`` on an Attribute does NOT fall through to the
  ObjectType/Schema). The default when nothing is set is ``both``.

* ``LinkStore`` + ``assert_link_lineage`` — the Source-lineage gate for the seven
  NON-object link families (schema, object_type, attribute, status,
  reference_type, comment, file). The composite FK ``(connection_id, source_id)``
  only proves the connection belongs to the link's Source; it does NOT prove the
  linked Core entity belongs to that Source (see ``link_guard`` docstring). Every
  insert therefore re-reads the Core entity's owning Schema/Source and fails
  closed when it does not match (T-293-03-01).

The store preloads only the links that already pass the lineage check, keyed by
the verified Core parent, so a lookup can never return a row from a foreign
Source. It re-checks the live Source URL/revision immediately before its commit
(``normalize_url``, the same canonicalization ``link_guard`` uses).

Imports only the public ``indramesh.sdk`` surface plus third-party/stdlib (the
plugin AST gate); it never imports ``app.*``.
"""

import uuid

from sqlalchemy import text

from indramesh.sdk import PluginTaskError

from .link_guard import normalize_url
from .system_attributes import is_jira_metadata

# The only supported explicit sync directions; NULL means "inherit from parent".
SYNC_DIRECTIONS = ("none", "pull", "push", "both")
DEFAULT_DIRECTION = "both"

# The seven non-object link families this module guards.
LINK_FAMILIES = (
    "schema",
    "object_type",
    "attribute",
    "status",
    "reference_type",
    "comment",
    "file",
)

# Each family's Core entity -> owning Schema -> Source join. The composite plugin
# FK cannot express this; the check is what makes a link Source-bound.
_LINEAGE_SQL = {
    "schema": text(
        "SELECT 1 FROM schemas WHERE id = CAST(:entity_id AS uuid) "
        "AND source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "object_type": text(
        "SELECT 1 FROM object_types ot JOIN schemas s ON s.id = ot.schema_id "
        "WHERE ot.id = CAST(:entity_id AS uuid) "
        "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "attribute": text(
        "SELECT 1 FROM object_type_attributes ota "
        "JOIN object_types ot ON ot.id = ota.object_type_id "
        "JOIN schemas s ON s.id = ot.schema_id "
        "WHERE ota.id = CAST(:entity_id AS uuid) "
        "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "status": text(
        "SELECT 1 FROM statuses st JOIN schemas s ON s.id = st.schema_id "
        "WHERE st.id = CAST(:entity_id AS uuid) "
        "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "reference_type": text(
        "SELECT 1 FROM reference_types rt "
        "WHERE rt.id = CAST(:entity_id AS uuid) "
        "AND rt.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "comment": text(
        "SELECT 1 FROM object_comments oc JOIN objects o ON o.id = oc.object_id "
        "JOIN schemas s ON s.id = o.schema_id "
        "WHERE oc.id = CAST(:entity_id AS uuid) "
        "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
    "file": text(
        "SELECT 1 FROM object_files of JOIN objects o ON o.id = of.object_id "
        "JOIN schemas s ON s.id = o.schema_id "
        "WHERE of.id = CAST(:entity_id AS uuid) "
        "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
    ),
}

# Live Source + connection bind snapshot, re-read at commit time.
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

# Existing links scoped to this Source/connection, each joined to the Core
# entity's owning Schema so the Source can be verified before the row is trusted.
_SCHEMA_LINKS_SQL = text(
    """
    SELECT l.schema_id, l.jira_schema_id, l.sync_direction,
           s.source_id AS entity_source_id
    FROM plugin_jira_schema_links l
    JOIN schemas s ON s.id = l.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_schema_id IS NOT NULL
    """
)
_TYPE_LINKS_SQL = text(
    """
    SELECT l.object_type_id, l.jira_type_id, l.sync_direction,
           ot.schema_id AS entity_schema_id,
           sc.source_id AS entity_source_id
    FROM plugin_jira_object_type_links l
    JOIN object_types ot ON ot.id = l.object_type_id
    JOIN schemas sc ON sc.id = ot.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_type_id IS NOT NULL
    """
)
_ATTRIBUTE_LINKS_SQL = text(
    """
    SELECT l.object_type_attribute_id, l.jira_type_attribute_id, l.sync_direction,
           ota.object_type_id AS object_type_id, ota.structure,
           sc.source_id AS entity_source_id
    FROM plugin_jira_attribute_links l
    JOIN object_type_attributes ota ON ota.id = l.object_type_attribute_id
    JOIN object_types ot ON ot.id = ota.object_type_id
    JOIN schemas sc ON sc.id = ot.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_type_attribute_id IS NOT NULL
    """
)
_STATUS_LINKS_SQL = text(
    """
    SELECT l.status_id, l.jira_status_id, st.schema_id AS entity_schema_id,
           sc.source_id AS entity_source_id
    FROM plugin_jira_status_links l
    JOIN statuses st ON st.id = l.status_id
    JOIN schemas sc ON sc.id = st.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_status_id IS NOT NULL
    """
)
_REFERENCE_LINKS_SQL = text(
    """
    SELECT l.reference_type_id, l.jira_reference_type_id,
           rt.schema_id AS entity_schema_id, rt.source_id AS entity_source_id
    FROM plugin_jira_reference_type_links l
    JOIN reference_types rt ON rt.id = l.reference_type_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_reference_type_id IS NOT NULL
    """
)

# The one deliberate cross-Source read in this module. A schema-less (global)
# reference type is unique by NAME across every Source
# (``uq_reference_types_reference_null_schema``), so a row found here is the very
# entity a second Source's pull must adopt — its identity is the name, and the
# per-Source identity used everywhere else cannot exist for it (the link table is
# 1:1 with the reference type, so only its creating Source can hold a link row).
_GLOBAL_REFERENCE_TYPE_BY_NAME_SQL = text(
    """
    SELECT rt.id FROM reference_types rt
    WHERE rt.schema_id IS NULL
      AND rt.reference = :reference
    LIMIT 1
    """
)

# --- Object link reads (293-05) ----------------------------------------------
#
# Every object read joins the Core Object to its owning Schema so the Source
# lineage is PROVEN, never inferred from the composite plugin FK (WR-04,
# T-293-05-01). ``is_detached = false`` is the detach gate: a detached object
# keeps its row but is never a reference source or target and is never swept.

# Source-wide target map: every attached, linked Object of this Source/connection.
_SOURCE_OBJECT_LINKS_SQL = text(
    """
    SELECT l.object_id AS object_id, l.jira_object_id AS jira_object_id
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_object_id IS NOT NULL
      AND l.is_detached = false
      AND s.source_id = CAST(:source_id AS uuid)
    """
)

# From-objects to reconcile for ONE selected Core type (the reference source
# scope is the current selected type only; targets may span the whole Source).
_TYPE_OBJECT_LINKS_SQL = text(
    """
    SELECT l.object_id AS object_id, l.jira_object_id AS jira_object_id
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_object_id IS NOT NULL
      AND l.is_detached = false
      AND s.source_id = CAST(:source_id AS uuid)
      AND o.object_type_id = CAST(:object_type_id AS uuid)
    ORDER BY l.jira_object_id
    """
)

# Proven-stale deletion candidates for ONE type (293-05, D-04/D-07). Every
# predicate is a deletion authority gate: same connection+Source, attached,
# non-null Jira id, matching bind-time URL revision and proven Core lineage.
_STALE_CANDIDATES_SQL = text(
    """
    SELECT l.object_id AS object_id, l.jira_object_id AS jira_object_id
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    JOIN schemas s ON s.id = o.schema_id
    WHERE l.source_id = CAST(:source_id AS uuid)
      AND l.connection_id = CAST(:connection_id AS uuid)
      AND l.jira_object_id IS NOT NULL
      AND l.is_detached = false
      AND l.bound_source_url_revision = CAST(:revision AS bigint)
      AND s.source_id = CAST(:source_id AS uuid)
      AND o.object_type_id = CAST(:object_type_id AS uuid)
    """
)

_OBJECT_LINEAGE_SQL = text(
    "SELECT 1 FROM objects o JOIN schemas s ON s.id = o.schema_id "
    "WHERE o.id = CAST(:object_id AS uuid) "
    "AND s.source_id = CAST(:source_id AS uuid) LIMIT 1"
)

_INSERT_TYPE_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_object_type_links
        (object_type_id, source_id, connection_id, jira_type_id, sync_direction)
    VALUES (CAST(:entity_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_id, NULL)
    ON CONFLICT (object_type_id) DO NOTHING
    """
)
_INSERT_ATTRIBUTE_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_attribute_links
        (object_type_attribute_id, source_id, connection_id,
         jira_type_attribute_id, sync_direction)
    VALUES (CAST(:entity_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_id, NULL)
    ON CONFLICT (object_type_attribute_id) DO NOTHING
    """
)
_INSERT_STATUS_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_status_links
        (status_id, source_id, connection_id, jira_status_id)
    VALUES (CAST(:entity_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_id)
    ON CONFLICT (status_id) DO NOTHING
    """
)
_INSERT_REFERENCE_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_reference_type_links
        (reference_type_id, source_id, connection_id, jira_reference_type_id)
    VALUES (CAST(:entity_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_id)
    ON CONFLICT (reference_type_id) DO NOTHING
    """
)


class LinkLineageError(PluginTaskError):
    """A plugin link row would bind a Core entity outside the link's Source."""


def effective_direction(attribute_link, type_link, schema_link) -> tuple[str, str]:
    """Resolve ``(direction, origin)`` from the most specific set link level.

    Precedence Attribute -> ObjectType -> Schema -> default ``both``. ``None``
    inherits; any set value (including an explicit ``none``) is final and is
    returned with the level it came from. An unknown value is a typed error
    rather than a silent default.
    """
    for value, origin in (
        (attribute_link, "attribute"),
        (type_link, "object_type"),
        (schema_link, "schema"),
    ):
        if value is None:
            continue
        if value not in SYNC_DIRECTIONS:
            raise ValueError(f"Unknown sync direction {value!r}")
        return value, origin
    return DEFAULT_DIRECTION, "default"


async def is_source_bound(session, family: str, entity_id, source_id) -> bool:
    """Return ``True`` iff the Core entity belongs to ``source_id``.

    Fail-closed: an unknown family or an unverifiable row returns ``False``.
    """
    sql = _LINEAGE_SQL.get(family)
    if sql is None:
        raise ValueError(f"Unknown link family {family!r}")
    try:
        uuid.UUID(str(entity_id))
        uuid.UUID(str(source_id))
    except ValueError:
        return False
    row = (
        await session.execute(
            sql, {"entity_id": str(entity_id), "source_id": str(source_id)}
        )
    ).first()
    return row is not None


async def assert_link_lineage(session, family: str, entity_id, source_id) -> None:
    """Raise ``LinkLineageError`` unless the Core entity is Source-bound."""
    if not await is_source_bound(session, family, entity_id, source_id):
        raise LinkLineageError(
            f"The {family} Core binding is not scoped to this Source."
        )


async def assert_object_lineage(session, object_id, source_id) -> None:
    """Raise ``LinkLineageError`` unless the Object's Schema belongs to ``source_id``."""
    try:
        uuid.UUID(str(object_id))
        uuid.UUID(str(source_id))
    except ValueError:
        raise LinkLineageError(
            "The object Core binding is not scoped to this Source."
        ) from None
    row = (
        await session.execute(
            _OBJECT_LINEAGE_SQL,
            {"object_id": str(object_id), "source_id": str(source_id)},
        )
    ).first()
    if row is None:
        raise LinkLineageError(
            "The object Core binding is not scoped to this Source."
        )


async def load_source_object_links(
    session, *, source_id, connection_id
) -> dict[str, uuid.UUID]:
    """Source-wide ``jira_object_id -> Core object id`` for attached linked Objects.

    Only rows whose Core Object lineage proves the Source are returned, so a
    lookup can never hand back a foreign-Source target (T-293-05-01).
    """
    rows = (
        await session.execute(
            _SOURCE_OBJECT_LINKS_SQL,
            {"source_id": str(source_id), "connection_id": str(connection_id)},
        )
    ).mappings().all()
    return {str(row["jira_object_id"]): row["object_id"] for row in rows}


async def load_type_object_links(
    session, *, source_id, connection_id, object_type_id
) -> list[tuple[uuid.UUID, str]]:
    """Attached linked ``(object_id, jira_object_id)`` rows of ONE Core type."""
    rows = (
        await session.execute(
            _TYPE_OBJECT_LINKS_SQL,
            {
                "source_id": str(source_id),
                "connection_id": str(connection_id),
                "object_type_id": str(object_type_id),
            },
        )
    ).mappings().all()
    return [(row["object_id"], str(row["jira_object_id"])) for row in rows]


async def load_stale_candidates(
    session, *, source_id, connection_id, object_type_id, revision
) -> list[tuple[uuid.UUID, str]]:
    """Attached, Jira-owned, lineage-proven objects of one type eligible for sweep."""
    rows = (
        await session.execute(
            _STALE_CANDIDATES_SQL,
            {
                "source_id": str(source_id),
                "connection_id": str(connection_id),
                "object_type_id": str(object_type_id),
                "revision": int(revision),
            },
        )
    ).mappings().all()
    return [(row["object_id"], str(row["jira_object_id"])) for row in rows]


class LinkStore:
    """Source-scoped link lookups + lineage-checked writes for one run.

    Built once per pull run and preloaded with only the links whose Core entity
    lineage verifies, so ``find_*`` can never hand back a cross-Source row. Every
    write re-asserts lineage and the whole batch is committed after a live
    URL/revision re-check.
    """

    def __init__(
        self,
        session,
        *,
        source_id,
        connection_id,
        connection_revision,
        core_source_url,
        connection_url,
    ) -> None:
        self._session = session
        self.source_id = uuid.UUID(str(source_id))
        self.connection_id = uuid.UUID(str(connection_id))
        self.connection_revision = connection_revision
        self._core_source_url = core_source_url
        self._connection_url = connection_url
        self.schema_by_jira: dict[str, uuid.UUID] = {}
        # (schema_id, jira_type_id) -> Core object_type id
        self.type_by_jira: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        # (object_type_id, jira_attribute_id) -> Core attribute id
        self.attribute_by_jira: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        # (schema_id, jira_status_id) -> Core status id
        self.status_by_jira: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        # Core status id -> jira_status_id (push/export need the Jira id)
        self.status_jira_by_id: dict[uuid.UUID, str] = {}
        # (schema_id or None (Source-global), jira_reference_type_id) -> id
        self.reference_by_jira: dict[tuple[uuid.UUID | None, str], uuid.UUID] = {}
        # Explicit ``sync_direction`` per link row (NULL inherits) — the input to
        # ``effective_direction`` for every pull pass (F-05).
        self.schema_direction: dict[uuid.UUID, str | None] = {}
        self.type_direction: dict[tuple[uuid.UUID, str], str | None] = {}
        self.attribute_direction: dict[tuple[uuid.UUID, str], str | None] = {}
        self._preloaded = False

    async def preload(self) -> None:
        """Load the Source/connection links whose Core lineage verifies."""
        params = {
            "source_id": str(self.source_id),
            "connection_id": str(self.connection_id),
        }
        own_source = str(self.source_id)

        for row in (await self._session.execute(_SCHEMA_LINKS_SQL, params)).mappings():
            if str(row["entity_source_id"]) != own_source:
                continue
            self.schema_by_jira[str(row["jira_schema_id"])] = row["schema_id"]
            self.schema_direction[row["schema_id"]] = row["sync_direction"]

        for row in (await self._session.execute(_TYPE_LINKS_SQL, params)).mappings():
            if str(row["entity_source_id"]) != own_source:
                continue
            key = (row["entity_schema_id"], str(row["jira_type_id"]))
            self.type_by_jira[key] = row["object_type_id"]
            self.type_direction[key] = row["sync_direction"]

        for row in (
            await self._session.execute(_ATTRIBUTE_LINKS_SQL, params)
        ).mappings():
            if str(row["entity_source_id"]) != own_source:
                continue
            key = (row["object_type_id"], str(row["jira_type_attribute_id"]))
            self.attribute_by_jira[key] = row["object_type_attribute_id"]
            self.attribute_direction[key] = "pull" if is_jira_metadata(row["structure"]) else row["sync_direction"]

        for row in (await self._session.execute(_STATUS_LINKS_SQL, params)).mappings():
            if str(row["entity_source_id"]) != own_source:
                continue
            key = (row["entity_schema_id"], str(row["jira_status_id"]))
            self.status_by_jira[key] = row["status_id"]
            self.status_jira_by_id[row["status_id"]] = str(row["jira_status_id"])

        for row in (
            await self._session.execute(_REFERENCE_LINKS_SQL, params)
        ).mappings():
            if str(row["entity_source_id"]) != own_source:
                continue
            key = (row["entity_schema_id"], str(row["jira_reference_type_id"]))
            self.reference_by_jira[key] = row["reference_type_id"]

        self._preloaded = True

    # --- lookups -------------------------------------------------------------

    def find_schema(self, jira_schema_id) -> uuid.UUID | None:
        return self.schema_by_jira.get(str(jira_schema_id))

    def find_type(self, schema_id, jira_type_id) -> uuid.UUID | None:
        return self.type_by_jira.get((uuid.UUID(str(schema_id)), str(jira_type_id)))

    def find_attribute(self, object_type_id, jira_attribute_id) -> uuid.UUID | None:
        return self.attribute_by_jira.get(
            (uuid.UUID(str(object_type_id)), str(jira_attribute_id))
        )

    def find_status(self, schema_id, jira_status_id) -> uuid.UUID | None:
        return self.status_by_jira.get(
            (uuid.UUID(str(schema_id)), str(jira_status_id))
        )

    def find_status_jira(self, status_id) -> str | None:
        """The Jira status id for a Core status id (push/export mapping)."""
        if status_id is None:
            return None
        return self.status_jira_by_id.get(uuid.UUID(str(status_id)))

    def find_reference_type(self, schema_id, jira_reference_type_id) -> uuid.UUID | None:
        key = (
            uuid.UUID(str(schema_id)) if schema_id is not None else None,
            str(jira_reference_type_id),
        )
        return self.reference_by_jira.get(key)

    async def find_global_reference_type(self, reference_name: str):
        """The id of the schema-less ReferenceType with this exact name, if any.

        A schema-less (global) reference type is unique by NAME across all
        Sources, so a row found here IS the entity this run must adopt instead of
        inserting a colliding duplicate — the deliberate exception to this
        store's Source scope.
        """
        row = (
            await self._session.execute(
                _GLOBAL_REFERENCE_TYPE_BY_NAME_SQL, {"reference": reference_name}
            )
        ).first()
        return row[0] if row is not None else None

    def adopt_global_reference_type(
        self, *, reference_type_id, jira_reference_type_id
    ) -> None:
        """Resolve a pre-existing global reference type for this run — no write.

        Its link row belongs to the Source that created it and the link table is
        1:1 with the reference type, so a second Source can never own one:
        writing here would only steal that row. The adopted id is remembered for
        this run's lookups; the next pull re-resolves it by the same global name.
        """
        self.reference_by_jira[(None, str(jira_reference_type_id))] = reference_type_id

    def find_attribute_by_jira(self, jira_attribute_id):
        """Locate an attribute link by Jira attribute id alone.

        Jira attribute ids are unique per instance, so a scan of the loaded
        ``(defining ObjectType, jira id)`` map resolves the defining Core OTA and
        the ObjectType it is bound to. Returns ``(attribute_id, object_type_id)``
        or ``(None, None)``. The store only ever holds lineage-verified rows, so a
        foreign-Source attribute can never be returned.
        """
        key = str(jira_attribute_id)
        for (object_type_id, jira_id), attribute_id in self.attribute_by_jira.items():
            if jira_id == key:
                return attribute_id, object_type_id
        return None, None

    def effective_attribute_direction(
        self, *, schema_id, jira_type_id, defining_object_type_id, jira_attribute_id
    ) -> tuple[str, str]:
        """Resolve an attribute's effective direction (F-05)."""
        attribute_link = self.attribute_direction.get(
            (uuid.UUID(str(defining_object_type_id)), str(jira_attribute_id))
        )
        type_link = self.type_direction.get(
            (uuid.UUID(str(schema_id)), str(jira_type_id))
        )
        schema_link = self.schema_direction.get(uuid.UUID(str(schema_id)))
        return effective_direction(attribute_link, type_link, schema_link)

    def effective_type_direction(
        self, *, schema_id, jira_type_id
    ) -> tuple[str, str]:
        """Resolve a whole type's effective direction (ObjectType -> Schema)."""
        type_link = self.type_direction.get(
            (uuid.UUID(str(schema_id)), str(jira_type_id))
        )
        schema_link = self.schema_direction.get(uuid.UUID(str(schema_id)))
        return effective_direction(None, type_link, schema_link)

    # --- writes --------------------------------------------------------------

    async def link_object_type(self, *, object_type_id, schema_id, jira_type_id):
        await assert_link_lineage(
            self._session, "object_type", object_type_id, self.source_id
        )
        await self._session.execute(
            _INSERT_TYPE_LINK_SQL,
            {
                "entity_id": str(object_type_id),
                "source_id": str(self.source_id),
                "connection_id": str(self.connection_id),
                "jira_id": str(jira_type_id),
            },
        )
        self.type_by_jira[(uuid.UUID(str(schema_id)), str(jira_type_id))] = (
            object_type_id
        )

    async def link_attribute(self, *, attribute_id, object_type_id, jira_attribute_id):
        await assert_link_lineage(
            self._session, "attribute", attribute_id, self.source_id
        )
        await self._session.execute(
            _INSERT_ATTRIBUTE_LINK_SQL,
            {
                "entity_id": str(attribute_id),
                "source_id": str(self.source_id),
                "connection_id": str(self.connection_id),
                "jira_id": str(jira_attribute_id),
            },
        )
        self.attribute_by_jira[
            (uuid.UUID(str(object_type_id)), str(jira_attribute_id))
        ] = attribute_id

    async def link_status(self, *, status_id, schema_id, jira_status_id):
        await assert_link_lineage(self._session, "status", status_id, self.source_id)
        await self._session.execute(
            _INSERT_STATUS_LINK_SQL,
            {
                "entity_id": str(status_id),
                "source_id": str(self.source_id),
                "connection_id": str(self.connection_id),
                "jira_id": str(jira_status_id),
            },
        )
        self.status_by_jira[(uuid.UUID(str(schema_id)), str(jira_status_id))] = (
            status_id
        )
        self.status_jira_by_id[uuid.UUID(str(status_id))] = str(jira_status_id)

    async def link_reference_type(self, *, reference_type_id, schema_id, jira_reference_type_id):
        await assert_link_lineage(
            self._session, "reference_type", reference_type_id, self.source_id
        )
        await self._session.execute(
            _INSERT_REFERENCE_LINK_SQL,
            {
                "entity_id": str(reference_type_id),
                "source_id": str(self.source_id),
                "connection_id": str(self.connection_id),
                "jira_id": str(jira_reference_type_id),
            },
        )
        key = (
            uuid.UUID(str(schema_id)) if schema_id is not None else None,
            str(jira_reference_type_id),
        )
        self.reference_by_jira[key] = reference_type_id

    # --- commit --------------------------------------------------------------

    async def assert_live_binding(self) -> None:
        """Re-read the Source URL/revision and fail closed on any drift."""
        row = (
            await self._session.execute(
                _LIVE_BINDING_SQL, {"connection_id": str(self.connection_id)}
            )
        ).mappings().first()
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

    async def commit(self) -> None:
        """Live-binding re-check, then one plugin commit for the link batch."""
        await self.assert_live_binding()
        await self._session.commit()
