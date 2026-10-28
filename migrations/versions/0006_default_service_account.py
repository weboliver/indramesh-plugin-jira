"""add_jira_connection_default_service_account

Quick 261004 (user request 2026-10-04): the Jira plugin's sixth private
revision. A connection remembers the ServiceAccount chosen in the connector
editor, so the connection test and the pull/push task forms can preselect it:

  * ``default_service_account_id`` — nullable FK to ``service_accounts.id``
    with ``ON DELETE SET NULL`` (deleting the account only clears the default),
    plus its FK index.

Only ``plugin_jira_*`` objects are touched; no Core table or column is altered.

Revision ID: 0006_default_service_account
Revises: 0005_attribute_conflicts
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0006_default_service_account"
down_revision: Union[str, None] = "0005_attribute_conflicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "plugin_jira_connections",
        sa.Column(
            "default_service_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "service_accounts.id",
                ondelete="SET NULL",
                name="fk_plugin_jira_connections_default_service_account",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_plugin_jira_connections_default_service_account_id",
        "plugin_jira_connections",
        ["default_service_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_jira_connections_default_service_account_id",
        table_name="plugin_jira_connections",
    )
    op.drop_column("plugin_jira_connections", "default_service_account_id")
