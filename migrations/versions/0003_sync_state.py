"""create_jira_sync_state

Phase 292 (JDATA-04/D-02/D-03): the Jira plugin's third private revision. It
creates the four plugin-owned sync-state families so 293/294 never have to write
the still-authoritative Core sync tables:

  * ``plugin_jira_value_sync_state`` — last-synced value/time per ObjectValue;
  * ``plugin_jira_conflicts`` — bidirectional pending/resolved conflicts,
    mirroring Core ``push_conflicts`` but anchored to a ``connection_id`` (the
    connection already identifies the Target for push or the Source for pull);
  * ``plugin_jira_sync_checkpoints`` — resumable pagination keyed by the legacy
    ``(source_id, object_type_id)`` arbiter;
  * ``plugin_jira_sync_audit_logs`` — connection-anchored audit rows (D-03).

Every connection column cascades with ``plugin_jira_connections`` so deleting a
Source or Target endpoint removes all dependent plugin state. The two partial
unique index predicates below are verbatim from the legacy Core migrations
(``0d3e8469fd1a`` and ``173_03_pull_conflict_support``) — 294 must insert with
``index_elements`` + ``index_where``, never ``constraint=`` (these are indexes,
not constraints).

Revision ID: 0003_sync_state
Revises: 0002_entity_links
Create Date: 2026-09-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision: str = "0003_sync_state"
down_revision: Union[str, None] = "0002_entity_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # --- value sync state ------------------------------------------------------
    op.create_table(
        "plugin_jira_value_sync_state",
        sa.Column("object_value_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column("last_synced_value", sa.JSON(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["object_value_id"],
            ["object_values.id"],
            name="fk_plugin_jira_value_sync_state_object_value_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["plugin_jira_connections.id"],
            name="fk_plugin_jira_value_sync_state_connection_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "object_value_id", name="pk_plugin_jira_value_sync_state"
        ),
    )
    op.create_index(
        "ix_plugin_jira_value_sync_state_connection_id",
        "plugin_jira_value_sync_state",
        ["connection_id"],
    )

    # --- conflicts -------------------------------------------------------------
    op.create_table(
        "plugin_jira_conflicts",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("object_id", _UUID, nullable=False),
        sa.Column("object_value_id", _UUID, nullable=True),
        sa.Column("object_ref_id", _UUID, nullable=True),
        sa.Column("task_id", _UUID, nullable=True),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column(
            "direction",
            sa.String(length=10),
            nullable=False,
            server_default=sa.text("'push'"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("local_value", sa.Text(), nullable=True),
        sa.Column("remote_value", sa.Text(), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_by", _UUID, nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "(object_value_id IS NOT NULL AND object_ref_id IS NULL) OR "
            "(object_value_id IS NULL AND object_ref_id IS NOT NULL)",
            name="ck_plugin_jira_conflicts_field_xor",
        ),
        sa.ForeignKeyConstraint(
            ["object_id"],
            ["objects.id"],
            name="fk_plugin_jira_conflicts_object_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_value_id"],
            ["object_values.id"],
            name="fk_plugin_jira_conflicts_object_value_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_ref_id"],
            ["object_refs.id"],
            name="fk_plugin_jira_conflicts_object_ref_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name="fk_plugin_jira_conflicts_task_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["plugin_jira_connections.id"],
            name="fk_plugin_jira_conflicts_connection_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name="fk_plugin_jira_conflicts_resolved_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_jira_conflicts"),
    )
    for column in (
        "object_id",
        "object_value_id",
        "object_ref_id",
        "task_id",
        "connection_id",
        "resolved_by",
    ):
        op.create_index(
            f"ix_plugin_jira_conflicts_{column}", "plugin_jira_conflicts", [column]
        )
    # Legacy predicate, but keyed on connection_id instead of target_id.
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
    op.execute(
        text(
            "CREATE UNIQUE INDEX uq_plugin_jira_pull_conflict_pending_ov "
            "ON plugin_jira_conflicts (object_value_id) "
            "WHERE status = 'pending' AND direction = 'pull'"
        )
    )

    # --- checkpoints -----------------------------------------------------------
    op.create_table(
        "plugin_jira_sync_checkpoints",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("source_id", _UUID, nullable=False),
        sa.Column("object_type_id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column(
            "last_page", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "seen_jira_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'IN_PROGRESS'"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_plugin_jira_sync_checkpoints_source_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["object_type_id"],
            ["object_types.id"],
            name="fk_plugin_jira_sync_checkpoints_object_type_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id", "source_id"],
            ["plugin_jira_connections.id", "plugin_jira_connections.source_id"],
            name="fk_plugin_jira_sync_checkpoints_connection_source",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_jira_sync_checkpoints"),
        sa.UniqueConstraint(
            "source_id",
            "object_type_id",
            name="uq_plugin_jira_sync_checkpoints_source_type",
        ),
    )
    op.create_index(
        "ix_plugin_jira_sync_checkpoints_connection_source",
        "plugin_jira_sync_checkpoints",
        ["connection_id", "source_id"],
    )
    op.create_index(
        "ix_plugin_jira_sync_checkpoints_object_type_id",
        "plugin_jira_sync_checkpoints",
        ["object_type_id"],
    )

    # --- audit logs ------------------------------------------------------------
    op.create_table(
        "plugin_jira_sync_audit_logs",
        sa.Column("id", _UUID, nullable=False),
        sa.Column("connection_id", _UUID, nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column("entity_id", sa.String(length=100), nullable=True),
        sa.Column("jira_updated_at", sa.String(length=50), nullable=True),
        sa.Column("indramesh_updated_at", sa.String(length=50), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["plugin_jira_connections.id"],
            name="fk_plugin_jira_sync_audit_logs_connection_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_jira_sync_audit_logs"),
    )
    op.create_index(
        "ix_plugin_jira_sync_audit_logs_connection_id",
        "plugin_jira_sync_audit_logs",
        ["connection_id"],
    )


def downgrade() -> None:
    op.drop_table("plugin_jira_sync_audit_logs")
    op.drop_table("plugin_jira_sync_checkpoints")
    op.drop_table("plugin_jira_conflicts")
    op.drop_table("plugin_jira_value_sync_state")