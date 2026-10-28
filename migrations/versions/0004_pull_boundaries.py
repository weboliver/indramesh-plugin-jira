"""add_jira_pull_boundaries

Phase 293 plan 04 (D-02/D-03/D-07, F-07): the Jira plugin's fourth private
revision. It adds the per-type incremental window to
``plugin_jira_sync_checkpoints`` — no Core table or column is touched:

  * ``last_successful_at`` — the stable lower bound of an unfinished run. It moves
    only when a type completes (``finish_type_run``), so it IS the run boundary;
    there is deliberately no separate ``run_lower_bound_at`` column (F-07).
  * ``run_started_at`` — captured before the first remote read of the type and
    copied into ``last_successful_at`` at completion.
  * ``retry_jira_ids`` — the Jira ids an isolated error skipped, fetched
    explicitly on the next run even when older than the lower bound.

Revision ID: 0004_pull_boundaries
Revises: 0003_sync_state
Create Date: 2026-09-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_pull_boundaries"
down_revision: Union[str, None] = "0003_sync_state"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plugin_jira_sync_checkpoints",
        sa.Column("last_successful_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "plugin_jira_sync_checkpoints",
        sa.Column("run_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "plugin_jira_sync_checkpoints",
        sa.Column(
            "retry_jira_ids",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plugin_jira_sync_checkpoints", "retry_jira_ids")
    op.drop_column("plugin_jira_sync_checkpoints", "run_started_at")
    op.drop_column("plugin_jira_sync_checkpoints", "last_successful_at")
