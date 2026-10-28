"""attribute_scoped_jira_conflicts

Phase 294 plan 02 (D-02/D-03, JSYNC-06): the Jira plugin's fifth private
revision. A multi-value or reference attribute conflict represents the WHOLE
attribute, so a conflict needs an anchor that is not one ObjectValue/ObjectRef:
``object_type_attribute_id``. This revision makes the conflict table able to
hold attribute-scope conflicts and tightens pending uniqueness per D-02:

  * ``object_type_attribute_id`` (nullable, CASCADE to ``object_type_attributes``)
    plus its FK index.
  * the field XOR check gains the attribute-scope branch; a row with all three
    anchors NULL is refused.
  * ``uq_plugin_jira_conflicts_pending_field_connection`` is re-predicated so the
    all-NULL anchor rows no longer collide on ``(0-uuid, 0-uuid, connection_id)``;
    its three elements are unchanged.
  * ``uq_plugin_jira_conflicts_pending_value`` — one pending conflict per
    ObjectValue across both directions (a push and a pull can never hold two open
    conflicts for one value).
  * ``uq_plugin_jira_conflicts_pending_attribute`` — one pending conflict per
    ``(object, attribute)`` regardless of direction or connection.
  * ``plugin_jira_attribute_sync_state`` — the attribute-level baseline for
    multi-value scalars and reference groups.

Only ``plugin_jira_*`` objects are touched; no Core table or column is altered.

Revision ID: 0005_attribute_conflicts
Revises: 0004_pull_boundaries
Create Date: 2026-09-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0005_attribute_conflicts"
down_revision: Union[str, None] = "0004_pull_boundaries"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID = postgresql.UUID(as_uuid=True)

_NEW_XOR_CHECK = (
    "num_nonnulls(object_value_id, object_ref_id) = 1 OR "
    "(object_value_id IS NULL AND object_ref_id IS NULL "
    "AND object_type_attribute_id IS NOT NULL)"
)
_OLD_XOR_CHECK = (
    "(object_value_id IS NOT NULL AND object_ref_id IS NULL) OR "
    "(object_value_id IS NULL AND object_ref_id IS NOT NULL)"
)
_NEW_FIELD_PREDICATE = (
    "status = 'pending' AND "
    "(object_value_id IS NOT NULL OR object_ref_id IS NOT NULL)"
)


def upgrade() -> None:
    # Dev-only 293 residue in the old raw-repr snapshot format (no live data per
    # the project's 2026-09-24 decision). Any real divergence is re-detected by
    # the next sync.
    op.execute(text("DELETE FROM plugin_jira_conflicts"))

    op.add_column(
        "plugin_jira_conflicts",
        sa.Column("object_type_attribute_id", _UUID, nullable=True),
    )
    op.create_foreign_key(
        "fk_plugin_jira_conflicts_object_type_attribute_id",
        "plugin_jira_conflicts",
        "object_type_attributes",
        ["object_type_attribute_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_plugin_jira_conflicts_object_type_attribute_id",
        "plugin_jira_conflicts",
        ["object_type_attribute_id"],
    )

    op.drop_constraint(
        "ck_plugin_jira_conflicts_field_xor",
        "plugin_jira_conflicts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_plugin_jira_conflicts_field_xor",
        "plugin_jira_conflicts",
        _NEW_XOR_CHECK,
    )

    op.drop_index(
        "uq_plugin_jira_conflicts_pending_field_connection",
        table_name="plugin_jira_conflicts",
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_plugin_jira_conflicts_pending_field_connection "
            "ON plugin_jira_conflicts ("
            "  COALESCE(object_value_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "  COALESCE(object_ref_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "  connection_id"
            f") WHERE {_NEW_FIELD_PREDICATE}"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_plugin_jira_conflicts_pending_value "
            "ON plugin_jira_conflicts (object_value_id) "
            "WHERE status = 'pending' AND object_value_id IS NOT NULL"
        )
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_plugin_jira_conflicts_pending_attribute "
            "ON plugin_jira_conflicts (object_id, object_type_attribute_id) "
            "WHERE status = 'pending' AND object_value_id IS NULL "
            "AND object_ref_id IS NULL"
        )
    )

    op.create_table(
        "plugin_jira_attribute_sync_state",
        sa.Column("object_id", _UUID, nullable=False),
        sa.Column("object_type_attribute_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column("last_synced_set", sa.JSON(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.id"],
            name="fk_plugin_jira_attribute_sync_state_object_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_type_attribute_id"],
            ["object_type_attributes.id"],
            name="fk_plugin_jira_attribute_sync_state_object_type_attribute_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["plugin_jira_connections.id"],
            name="fk_plugin_jira_attribute_sync_state_connection_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "object_id",
            "object_type_attribute_id",
            name="pk_plugin_jira_attribute_sync_state",
        ),
    )
    op.create_index(
        "ix_plugin_jira_attribute_sync_state_connection_id",
        "plugin_jira_attribute_sync_state",
        ["connection_id"],
    )
    op.create_index(
        "ix_plugin_jira_attribute_sync_state_object_type_attribute_id",
        "plugin_jira_attribute_sync_state",
        ["object_type_attribute_id"],
    )


def downgrade() -> None:
    op.drop_table("plugin_jira_attribute_sync_state")

    # Attribute-scope rows cannot satisfy the restored XOR check, so remove them
    # before the check comes back.
    op.execute(
        text(
            "DELETE FROM plugin_jira_conflicts "
            "WHERE object_value_id IS NULL AND object_ref_id IS NULL"
        )
    )
    op.drop_index(
        "uq_plugin_jira_conflicts_pending_attribute",
        table_name="plugin_jira_conflicts",
    )
    op.drop_index(
        "uq_plugin_jira_conflicts_pending_value",
        table_name="plugin_jira_conflicts",
    )

    op.drop_index(
        "uq_plugin_jira_conflicts_pending_field_connection",
        table_name="plugin_jira_conflicts",
    )
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_plugin_jira_conflicts_pending_field_connection "
            "ON plugin_jira_conflicts ("
            "  COALESCE(object_value_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "  COALESCE(object_ref_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "  connection_id"
            ") WHERE status = 'pending'"
        )
    )

    op.drop_constraint(
        "ck_plugin_jira_conflicts_field_xor",
        "plugin_jira_conflicts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_plugin_jira_conflicts_field_xor",
        "plugin_jira_conflicts",
        _OLD_XOR_CHECK,
    )

    op.drop_index(
        "ix_plugin_jira_conflicts_object_type_attribute_id",
        table_name="plugin_jira_conflicts",
    )
    op.drop_constraint(
        "fk_plugin_jira_conflicts_object_type_attribute_id",
        "plugin_jira_conflicts",
        type_="foreignkey",
    )
    op.drop_column("plugin_jira_conflicts", "object_type_attribute_id")
