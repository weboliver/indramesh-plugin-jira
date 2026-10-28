"""Adopting an existing empty Core schema as a first-pull target (quick 261004).

A first pull of an unlinked Jira schema normally creates a new Core schema named
after the Jira schema. The user may instead pick an existing schema of the same
Source so its name and keygen prefix stay theirs. Only a schema that has no
ObjectTypes (hence no Objects) and is not yet linked to any Jira schema can be
adopted; the check runs at admission and again, row-locked, right before the
link is written.
"""

from sqlalchemy import text

from indramesh.sdk import PluginTaskError

_ADOPTABLE_FILTER = """
    s.source_id = CAST(:source_id AS uuid)
    AND NOT EXISTS (SELECT 1 FROM object_types ot WHERE ot.schema_id = s.id)
    AND NOT EXISTS (
        SELECT 1 FROM plugin_jira_schema_links l WHERE l.schema_id = s.id
    )
"""

_ADOPTABLE_SCHEMAS_SQL = text(
    "SELECT s.id, s.name, s.keygen FROM schemas s WHERE"
    + _ADOPTABLE_FILTER
    + "ORDER BY s.name, s.id"
)

_ADOPTABLE_SCHEMA_SQL = text(
    "SELECT s.id FROM schemas s WHERE s.id = CAST(:schema_id AS uuid) AND"
    + _ADOPTABLE_FILTER
)

_LOCK_SCHEMA_SQL = text(
    "SELECT id FROM schemas WHERE id = CAST(:schema_id AS uuid) FOR UPDATE"
)


async def list_adoptable_schemas(session, source_id) -> list[dict]:
    """The Source's schemas a first pull may adopt, by name."""
    rows = (
        await session.execute(_ADOPTABLE_SCHEMAS_SQL, {"source_id": str(source_id)})
    ).mappings().all()
    return [
        {"id": str(row["id"]), "name": row["name"], "keygen": row["keygen"]}
        for row in rows
    ]


async def require_adoptable_schema(session, source_id, schema_id, *, lock=False):
    """Refuse a target schema that is foreign, non-empty or already linked."""
    if lock:
        await session.execute(_LOCK_SCHEMA_SQL, {"schema_id": str(schema_id)})
    row = (
        await session.execute(
            _ADOPTABLE_SCHEMA_SQL,
            {"schema_id": str(schema_id), "source_id": str(source_id)},
        )
    ).first()
    if row is None:
        raise PluginTaskError(
            "The selected target schema must be an empty schema of this Source "
            "that is not linked to a Jira schema yet."
        )


_JIRA_SCHEMA_LINKED_SQL = text(
    "SELECT schema_id FROM plugin_jira_schema_links "
    "WHERE source_id = CAST(:source_id AS uuid) AND jira_schema_id = :jira_schema_id "
    "LIMIT 1"
)

_INSERT_LINK_SQL = text(
    """
    INSERT INTO plugin_jira_schema_links
        (schema_id, source_id, connection_id, jira_schema_id)
    VALUES (CAST(:schema_id AS uuid), CAST(:source_id AS uuid),
            CAST(:connection_id AS uuid), :jira_schema_id)
    ON CONFLICT (schema_id) DO NOTHING
    RETURNING schema_id
    """
)


async def link_existing_schema(
    session, *, source_id, schema_id, connection_id, jira_schema_id
):
    """Link an empty, unlinked schema to a not-yet-linked Jira schema (no import).

    The caller holds the Source run lock, so no pull can bootstrap the same Jira
    schema concurrently; the next pull fills the linked schema.
    """
    await require_adoptable_schema(session, source_id, schema_id, lock=True)
    taken = (
        await session.execute(
            _JIRA_SCHEMA_LINKED_SQL,
            {"source_id": str(source_id), "jira_schema_id": str(jira_schema_id)},
        )
    ).first()
    if taken is not None:
        raise PluginTaskError(
            "This Jira schema is already linked to another schema of this Source."
        )
    inserted = (
        await session.execute(
            _INSERT_LINK_SQL,
            {
                "schema_id": str(schema_id),
                "source_id": str(source_id),
                "connection_id": str(connection_id),
                "jira_schema_id": str(jira_schema_id),
            },
        )
    ).first()
    if inserted is None:
        raise PluginTaskError("This schema is already linked to Jira.")
