"""Core write-protection markers tied to the effective sync direction (D-08/D-11).

An attribute whose EFFECTIVE direction is ``pull`` is written by Jira only, so
users must not edit it in Core. The plugin therefore marks exactly that
ObjectTypeAttribute ``externally_readonly`` with ``managed_by`` set to the
plugin's own name; Core then refuses a user-origin value write with a 409 while
still letting the plugin's own pull write through (the owner exemption from
294-01). The marker is ATTRIBUTE-scoped: no ObjectType marker is ever written,
so type/schema deletes and the non-Jira attributes of the same type stay
editable.

The desired state is derived from the SAME resolver the engines use
(``links.effective_direction``) — never a second evaluator. An inherited
attribute is protected only when its effective direction is ``pull`` for EVERY
concrete ObjectType that uses it (the defining type plus every inheriting
descendant), so a single non-pull child keeps it editable.

Detached objects are exempted through the Core per-object release (D-11): a
detached link gets a release row for the plugin owner, so users can edit that
object's pull-only values while an attached sibling stays 409. The engines
enforce the direction independently of the marker; the marker only adds the
user-facing lock and the release only lifts it for one object.

Server-generated system attributes (Key/Created/Updated) are never marked: their
values come from Jira itself and a user never edits them.
"""

import uuid

from sqlalchemy import text

from indramesh.sdk import (
    CoreDataConflict,
    CoreDataError,
    set_attribute_write_protection,
    set_object_write_protection_release,
)

from .links import effective_direction
from .system_attributes import is_jira_metadata

_SERVER_GENERATED_ATTRIBUTES = ("Key", "Created", "Updated")


# Every attribute link of the schema, expanded over each concrete ObjectType that
# uses the attribute (the defining type at depth 0 plus every inheriting
# descendant), with the attribute link direction, the defining type's direction,
# the schema direction and the concrete type's own direction. ``effective_direction``
# is then evaluated per concrete type — the single resolver, never a copy.
_PROTECTION_SCOPE_SQL = text(
    """
    WITH RECURSIVE attr_concrete AS (
        SELECT al.object_type_attribute_id AS attribute_id,
               ota.object_type_id          AS concrete_type_id,
               0                           AS depth
        FROM plugin_jira_attribute_links al
        JOIN object_type_attributes ota
          ON ota.id = al.object_type_attribute_id
        JOIN object_types dot ON dot.id = ota.object_type_id
        WHERE dot.schema_id = CAST(:schema_id AS uuid)
        UNION ALL
        SELECT ac.attribute_id, child.id, ac.depth + 1
        FROM attr_concrete ac
        JOIN object_types parent ON parent.id = ac.concrete_type_id
        JOIN object_types child ON child.parent_id = ac.concrete_type_id
        WHERE parent.inherits_attributes = true AND ac.depth < 100
    )
    SELECT ac.attribute_id            AS attribute_id,
           ota.name                   AS attribute_name,
           ota.is_system              AS is_system,
           ota.structure              AS structure,
           ota.object_type_id         AS defining_object_type_id,
           ota.externally_readonly    AS externally_readonly,
           ota.managed_by             AS managed_by,
           al.sync_direction          AS attribute_direction,
           dtl.sync_direction         AS type_direction,
           sl.sync_direction          AS schema_direction,
           ctl.sync_direction         AS concrete_type_direction
    FROM attr_concrete ac
    JOIN object_type_attributes ota ON ota.id = ac.attribute_id
    JOIN plugin_jira_attribute_links al
      ON al.object_type_attribute_id = ac.attribute_id
    JOIN object_types dot ON dot.id = ota.object_type_id
    LEFT JOIN plugin_jira_object_type_links dtl
      ON dtl.object_type_id = ota.object_type_id
    LEFT JOIN plugin_jira_object_type_links ctl
      ON ctl.object_type_id = ac.concrete_type_id
    LEFT JOIN plugin_jira_schema_links sl ON sl.schema_id = dot.schema_id
    WHERE dot.schema_id = CAST(:schema_id AS uuid)
      AND al.jira_type_attribute_id IS NOT NULL
    """
)

_SCHEMA_LINK_EXISTS_SQL = text(
    "SELECT 1 FROM plugin_jira_schema_links "
    "WHERE schema_id = CAST(:schema_id AS uuid) LIMIT 1"
)

# Every linked Object of one Core schema and its detach flag — the input to the
# D-11 per-object release reconciliation.
_SCHEMA_OBJECT_LINKS_SQL = text(
    """
    SELECT l.object_id, l.is_detached
    FROM plugin_jira_object_links l
    JOIN objects o ON o.id = l.object_id
    WHERE o.schema_id = CAST(:schema_id AS uuid)
    """
)

_SCHEMA_RELEASES_SQL = text(
    """
    SELECT r.object_id
    FROM object_write_protection_releases r
    JOIN objects o ON o.id = r.object_id
    WHERE o.schema_id = CAST(:schema_id AS uuid)
      AND r.managed_by = :owner
    """
)


def desired_protection(rows) -> dict:
    """``{attribute_id: readonly}`` from the per-concrete-type scope rows.

    An attribute is protected only when ``effective_direction`` is ``pull`` for
    EVERY concrete type that uses it; a single non-pull type keeps it editable.
    """
    grouped: dict[uuid.UUID, list] = {}
    for row in rows:
        grouped.setdefault(uuid.UUID(str(row["attribute_id"])), []).append(row)

    desired: dict[uuid.UUID, bool] = {}
    for attribute_id, group in grouped.items():
        readonly = True
        for row in group:
            direction, _origin = effective_direction(
                "pull" if is_jira_metadata(row.get("structure")) else row["attribute_direction"],
                row["concrete_type_direction"],
                row["schema_direction"],
            )
            if direction != "pull":
                readonly = False
                break
        desired[attribute_id] = readonly
    return desired


async def reconcile_write_protection(ctx, session, *, schema_id, user=None) -> dict:
    """Bring this plugin's markers and releases in line with the direction (D-08/D-11).

    Called right after a direction change and after each schema's metadata
    discovery in a pull, so a newly linked attribute under an inherited pull
    direction is protected in the same run. Only the plugin's OWN markers are
    ever set or lifted: a foreign or NULL-owner marker is left untouched and
    counted as ``skipped`` (never a failure). Server-generated system attributes
    are never marked. When the schema's Jira link is gone the desired state is
    "unprotected" for every plugin-owned marker of that schema and no release
    rows — a marker never outlives its reason.

    Returns ``{set, lifted, skipped, released, unreleased}``.
    """
    owner = getattr(ctx, "_plugin_name", None)
    result = {
        "set": 0,
        "lifted": 0,
        "skipped": 0,
        "released": 0,
        "unreleased": 0,
    }
    schema_uuid = uuid.UUID(str(schema_id))

    has_link = (
        await session.execute(_SCHEMA_LINK_EXISTS_SQL, {"schema_id": str(schema_uuid)})
    ).scalar() is not None
    rows = (
        await session.execute(
            _PROTECTION_SCOPE_SQL, {"schema_id": str(schema_uuid)}
        )
    ).mappings().all()

    current: dict[uuid.UUID, tuple[bool, str | None]] = {}
    names: dict[uuid.UUID, str | None] = {}
    for row in rows:
        attribute_id = uuid.UUID(str(row["attribute_id"]))
        current[attribute_id] = (
            bool(row["externally_readonly"]),
            row["managed_by"],
        )
        names[attribute_id] = row["attribute_name"]

    if has_link:
        desired = desired_protection(rows)
    else:
        # The schema link is gone: lift every plugin-owned marker of the schema.
        desired = {attribute_id: False for attribute_id in current}

    for attribute_id, readonly in desired.items():
        if names.get(attribute_id) in _SERVER_GENERATED_ATTRIBUTES:
            continue
        is_marked, marker_owner = current.get(attribute_id, (False, None))
        if readonly and is_marked and marker_owner == owner:
            continue
        if not readonly and not is_marked:
            continue
        try:
            changed = await set_attribute_write_protection(
                ctx, attribute_id, readonly=readonly, user=user
            )
        except CoreDataConflict:
            result["skipped"] += 1
            continue
        except CoreDataError:
            result["skipped"] += 1
            continue
        if not changed:
            continue
        if readonly:
            result["set"] += 1
        else:
            result["lifted"] += 1

    object_rows = (
        await session.execute(
            _SCHEMA_OBJECT_LINKS_SQL, {"schema_id": str(schema_uuid)}
        )
    ).mappings().all()
    released_ids: set[uuid.UUID] = set()
    if owner:
        released_ids = {
            uuid.UUID(str(row["object_id"]))
            for row in (
                await session.execute(
                    _SCHEMA_RELEASES_SQL,
                    {"schema_id": str(schema_uuid), "owner": owner},
                )
            ).mappings().all()
        }

    for row in object_rows:
        object_id = uuid.UUID(str(row["object_id"]))
        desired_released = bool(row["is_detached"]) and has_link
        is_released = object_id in released_ids
        if desired_released == is_released:
            continue
        try:
            changed = await set_object_write_protection_release(
                ctx, object_id, released=desired_released, user=user
            )
        except CoreDataConflict:
            result["skipped"] += 1
            continue
        except CoreDataError:
            result["skipped"] += 1
            continue
        if not changed:
            continue
        if desired_released:
            result["released"] += 1
        else:
            result["unreleased"] += 1

    return result
