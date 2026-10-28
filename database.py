"""Plugin-owned declarative metadata for the Jira plugin (Phase 292, JDATA-02/03/04).

This module declares the plugin's OWN ``DeclarativeBase``/``MetaData`` — it never
imports ``app.db.base.Base``. ``JiraPlugin.database_metadata`` exposes
``Base.metadata`` to the core runner, which passes it to the shared Alembic env as
``target_metadata``.

A plugin-owned table may reference Core tables; every such FK states its
``ON DELETE`` behaviour explicitly (no Core-wide default). Credentials are NOT
part of this model: they live in the Core ``ServiceAccount`` records, and
``Source.source_url`` / ``Target.target_url`` stay generic Core fields.

Known limitation (mirrors ``plugins/example/database.py``): a Core FK declared by
string inside a PRIVATE ``MetaData`` is resolved lazily, so this metadata is
documentation/fidelity only — every revision is hand-written.

The eight ``plugin_jira_*_links`` tables bind one Core entity each to a
source-bound connection (D-01): the Core entity ID is the primary key AND the
``ON DELETE CASCADE`` foreign key, ``source_id`` points at the Core Source, and
the composite ``(connection_id, source_id)`` FK proves the link can only target a
source-bound connection of the *same* Source. Object links additionally carry the
``bound_source_url_revision`` snapshot the D-04 stale-binding guard reads.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The only supported explicit sync directions; NULL means "inherit from parent".
SYNC_DIRECTIONS = ("none", "pull", "push", "both")

_DIRECTION_CHECK = "sync_direction IN ('none', 'pull', 'push', 'both')"


class Base(DeclarativeBase):
    pass


class JiraConnection(Base):
    """One endpoint-bound Jira connection — exactly one Source OR Target (D-01).

    ``base_url`` is a normalized snapshot of the endpoint URL at bind time; live
    Jira calls keep using the Core URL. ``bound_source_url_revision`` is stamped
    only for source-bound connections (the composite link FKs below reference
    ``(id, source_id)``). ``default_conflict_strategy`` has NO default: NULL means
    a conflict stays open for manual resolution (D-02).
    """

    __tablename__ = "plugin_jira_connections"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(source_id, target_id) = 1",
            name="ck_plugin_jira_connections_endpoint_xor",
        ),
        CheckConstraint(
            "(source_id IS NULL) = (bound_source_url_revision IS NULL)",
            name="ck_plugin_jira_connections_bound_revision",
        ),
        CheckConstraint(
            "default_conflict_strategy IN ('prefer_local', 'prefer_remote')",
            name="ck_plugin_jira_connections_conflict_strategy",
        ),
        UniqueConstraint(
            "source_id", name="uq_plugin_jira_connections_source_id"
        ),
        UniqueConstraint(
            "target_id", name="uq_plugin_jira_connections_target_id"
        ),
        # The referenced key for Phase 292-03's composite link FKs.
        UniqueConstraint(
            "id", "source_id", name="uq_plugin_jira_connections_id_source_id"
        ),
        Index(
            "ix_plugin_jira_connections_default_service_account_id",
            "default_service_account_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=True,
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("targets.id", ondelete="CASCADE"),
        nullable=True,
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    bound_source_url_revision: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    default_conflict_strategy: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    # Quick 261004: the account chosen in the connector editor, preselected by
    # the connection test and the pull/push task forms.
    default_service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "service_accounts.id",
            ondelete="SET NULL",
            name="fk_plugin_jira_connections_default_service_account",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


# --- Eight Core-entity link tables (JDATA-02, D-01/D-03) ----------------------
#
# Every link table repeats the same shape:
#   * Core entity id = PK (and therefore the FK index) -> Core table CASCADE
#   * source_id     -> sources.id CASCADE
#   * connection_id -> composite (connection_id, source_id)
#                        -> plugin_jira_connections (id, source_id) CASCADE
#   * jira_*_id     VARCHAR(100) NULL (legacy width of every jira_*_id column)
# The composite FK is what restricts a link to a SOURCE-BOUND connection of the
# SAME Source (a Target-bound connection has source_id NULL and cannot match).


class JiraSchemaLink(Base):
    """Schema <-> Jira schema binding; ``sync_direction`` NULL inherits (D-02)."""

    __tablename__ = "plugin_jira_schema_links"
    __table_args__ = (
        CheckConstraint(_DIRECTION_CHECK, name="ck_plugin_jira_schema_links_direction"),
        Index(
            "ix_plugin_jira_schema_links_connection_source",
            "connection_id",
            "source_id",
        ),
        Index("ix_plugin_jira_schema_links_source_id", "source_id"),
    )

    schema_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_schema_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sync_direction: Mapped[str | None] = mapped_column(String(10), nullable=True)


class JiraObjectTypeLink(Base):
    """ObjectType <-> Jira type binding; ``sync_direction`` NULL inherits."""

    __tablename__ = "plugin_jira_object_type_links"
    __table_args__ = (
        CheckConstraint(
            _DIRECTION_CHECK, name="ck_plugin_jira_object_type_links_direction"
        ),
        Index(
            "ix_plugin_jira_object_type_links_connection_source",
            "connection_id",
            "source_id",
        ),
        Index("ix_plugin_jira_object_type_links_source_id", "source_id"),
    )

    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_type_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sync_direction: Mapped[str | None] = mapped_column(String(10), nullable=True)


class JiraAttributeLink(Base):
    """ObjectTypeAttribute <-> Jira attribute binding; NULL inherits."""

    __tablename__ = "plugin_jira_attribute_links"
    __table_args__ = (
        CheckConstraint(_DIRECTION_CHECK, name="ck_plugin_jira_attribute_links_direction"),
        Index(
            "ix_plugin_jira_attribute_links_connection_source",
            "connection_id",
            "source_id",
        ),
        Index("ix_plugin_jira_attribute_links_source_id", "source_id"),
    )

    object_type_attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_type_attribute_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    sync_direction: Mapped[str | None] = mapped_column(String(10), nullable=True)


class JiraStatusLink(Base):
    """Status <-> Jira status binding (no unique key: none exists today)."""

    __tablename__ = "plugin_jira_status_links"
    __table_args__ = (
        Index(
            "ix_plugin_jira_status_links_connection_source",
            "connection_id",
            "source_id",
        ),
        Index("ix_plugin_jira_status_links_source_id", "source_id"),
    )

    status_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_status_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class JiraReferenceTypeLink(Base):
    """ReferenceType <-> Jira reference-type binding (no unique key today)."""

    __tablename__ = "plugin_jira_reference_type_links"
    __table_args__ = (
        Index(
            "ix_plugin_jira_reference_type_links_connection_source",
            "connection_id",
            "source_id",
        ),
        Index("ix_plugin_jira_reference_type_links_source_id", "source_id"),
    )

    reference_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_reference_type_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )


class JiraObjectLink(Base):
    """Object <-> Jira object binding; the D-04 binding-revision anchor.

    ``bound_source_url_revision`` is the Core ``sources.source_url_revision``
    snapshot taken when the link was bound. ``link_guard.is_stale_binding``
    compares it (plus the connection snapshot) against the current Core revision,
    so a changed-then-reverted Source URL can never silently re-authorize an old
    link. ``jira_object_id`` is nullable and the ``(source_id, jira_object_id)``
    unique key (default NULLS DISTINCT) allows many unbound rows per Source while
    forbidding two live bindings for the same Jira object.
    """

    __tablename__ = "plugin_jira_object_links"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "jira_object_id",
            name="uq_plugin_jira_object_links_source_jira",
        ),
        Index(
            "ix_plugin_jira_object_links_connection_source",
            "connection_id",
            "source_id",
        ),
    )

    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_object_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_detached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    bound_source_url_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)


class JiraCommentLink(Base):
    """ObjectComment <-> Jira comment binding; source-scoped Jira id unique.

    Two Jira instances can reuse IDs, so the unique key is ``(source_id,
    jira_comment_id)`` (NULLS DISTINCT allows any number of unbound rows).
    """

    __tablename__ = "plugin_jira_comment_links"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "jira_comment_id",
            name="uq_plugin_jira_comment_links_source_jira",
        ),
        Index(
            "ix_plugin_jira_comment_links_connection_source",
            "connection_id",
            "source_id",
        ),
    )

    object_comment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_comment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class JiraAttachmentLink(Base):
    """ObjectFile <-> Jira attachment binding; source-scoped Jira id unique."""

    __tablename__ = "plugin_jira_attachment_links"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "jira_attachment_id",
            name="uq_plugin_jira_attachment_links_source_jira",
        ),
        Index(
            "ix_plugin_jira_attachment_links_connection_source",
            "connection_id",
            "source_id",
        ),
    )

    object_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    jira_attachment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


# --- Four plugin-owned sync-state families (JDATA-04, D-02/D-03) --------------
#
# These replace the legacy Core sync tables for plugin writes. Every state row is
# anchored to its connection and cascades away with it (D-03). 294 must consume
# the partial unique indexes with ``index_elements`` + ``index_where`` — never
# ``constraint=``: every one of them is an INDEX, not a constraint, so
# ``ON CONFLICT ON CONSTRAINT`` fails live with
# ``constraint "..." for table "..." does not exist`` (294-RESEARCH bug 1). The
# legacy ``constraint="uq_push_conflict_pending_field_target"`` never actually
# named a constraint either; PostgreSQL only resolved it when the name happened
# to match an inferable index.


class JiraValueSyncState(Base):
    """Last-synced value/time per ObjectValue and connection (JDATA-04)."""

    __tablename__ = "plugin_jira_value_sync_state"
    __table_args__ = (
        Index(
            "ix_plugin_jira_value_sync_state_connection_id", "connection_id"
        ),
    )

    object_value_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plugin_jira_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_synced_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JiraAttributeSyncState(Base):
    """Attribute-level baseline per ``(object, attribute)`` (D-02, 294).

    Multi-value scalar and reference conflicts replace the WHOLE attribute, so
    their baseline is the canonical value set, not one row's last-synced value.
    Anchored to the Source link's connection and cascading away with the Object,
    the ObjectTypeAttribute or the connection.
    """

    __tablename__ = "plugin_jira_attribute_sync_state"
    __table_args__ = (
        Index(
            "ix_plugin_jira_attribute_sync_state_connection_id", "connection_id"
        ),
        Index(
            "ix_plugin_jira_attribute_sync_state_object_type_attribute_id",
            "object_type_attribute_id",
        ),
    )

    object_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    object_type_attribute_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plugin_jira_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    last_synced_set: Mapped[dict] = mapped_column(JSON, nullable=False)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JiraConflict(Base):
    """Plugin-owned conflict row mirroring Core ``push_conflicts`` (D-02/D-03).

    A conflict is anchored on exactly one of three scopes: a single ObjectValue
    (``object_value_id``), a single ObjectRef (``object_ref_id``) or — for a
    multi-value/reference attribute — the whole attribute
    (``object_type_attribute_id`` with both value/ref ids NULL).

    Every upsert names its partial index by elements + verbatim predicate, never
    ``ON CONFLICT ON CONSTRAINT``: these are indexes, not constraints
    (294-RESEARCH bug 1). ``uq_plugin_jira_conflicts_pending_value`` makes one
    pending conflict per ObjectValue unique across BOTH directions;
    ``uq_plugin_jira_conflicts_pending_attribute`` makes one pending conflict per
    ``(object, attribute)`` unique regardless of direction/connection;
    ``uq_plugin_jira_conflicts_pending_field_connection`` keeps the legacy
    element shape but only applies to value/ref rows, so attribute rows cannot
    collide on the all-NULL COALESCE sentinel.
    """

    __tablename__ = "plugin_jira_conflicts"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(object_value_id, object_ref_id) = 1 OR "
            "(object_value_id IS NULL AND object_ref_id IS NULL "
            "AND object_type_attribute_id IS NOT NULL)",
            name="ck_plugin_jira_conflicts_field_xor",
        ),
        Index("ix_plugin_jira_conflicts_object_id", "object_id"),
        Index("ix_plugin_jira_conflicts_object_value_id", "object_value_id"),
        Index("ix_plugin_jira_conflicts_object_ref_id", "object_ref_id"),
        Index(
            "ix_plugin_jira_conflicts_object_type_attribute_id",
            "object_type_attribute_id",
        ),
        Index("ix_plugin_jira_conflicts_task_id", "task_id"),
        Index("ix_plugin_jira_conflicts_connection_id", "connection_id"),
        Index("ix_plugin_jira_conflicts_resolved_by", "resolved_by"),
        Index(
            "uq_plugin_jira_conflicts_pending_field_connection",
            text(
                "COALESCE(object_value_id, "
                "'00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            text(
                "COALESCE(object_ref_id, "
                "'00000000-0000-0000-0000-000000000000'::uuid)"
            ),
            "connection_id",
            unique=True,
            postgresql_where=text(
                "status = 'pending' AND "
                "(object_value_id IS NOT NULL OR object_ref_id IS NOT NULL)"
            ),
        ),
        Index(
            "uq_plugin_jira_pull_conflict_pending_ov",
            "object_value_id",
            unique=True,
            postgresql_where=text("status = 'pending' AND direction = 'pull'"),
        ),
        Index(
            "uq_plugin_jira_conflicts_pending_value",
            "object_value_id",
            unique=True,
            postgresql_where=text(
                "status = 'pending' AND object_value_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_plugin_jira_conflicts_pending_attribute",
            "object_id",
            "object_type_attribute_id",
            unique=True,
            postgresql_where=text(
                "status = 'pending' AND object_value_id IS NULL "
                "AND object_ref_id IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    object_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("objects.id", ondelete="CASCADE"),
        nullable=False,
    )
    object_value_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_values.id", ondelete="CASCADE"),
        nullable=True,
    )
    object_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_refs.id", ondelete="CASCADE"),
        nullable=True,
    )
    object_type_attribute_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_type_attributes.id", ondelete="CASCADE"),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plugin_jira_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False, default="push")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    local_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class JiraSyncCheckpoint(Base):
    """Resumable Jira pagination per ``(source, object_type)`` (JDATA-04).

    Keeps the legacy ``(source_id, object_type_id)`` arbiter so a resumable pull
    behaves the same as the Core engine today, and adds the connection anchor.
    """

    __tablename__ = "plugin_jira_sync_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "object_type_id",
            name="uq_plugin_jira_sync_checkpoints_source_type",
        ),
        Index(
            "ix_plugin_jira_sync_checkpoints_connection_source",
            "connection_id",
            "source_id",
        ),
        Index(
            "ix_plugin_jira_sync_checkpoints_object_type_id", "object_type_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    object_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("object_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    last_page: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seen_jira_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 293-04 run boundaries (D-02/D-03/D-07). ``last_successful_at`` moves only
    # when a type completes and IS the stable lower bound of an unfinished run
    # (no separate ``run_lower_bound_at`` — F-07). ``run_started_at`` is captured
    # before the first remote read and copied into ``last_successful_at`` at
    # completion. ``retry_jira_ids`` lists entries an isolated error skipped so
    # the next run can fetch them explicitly, even when older than the bound.
    last_successful_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    run_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retry_jira_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::json")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="IN_PROGRESS")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )


class JiraSyncAuditLog(Base):
    """Connection-anchored sync audit (D-03).

    Uses ``connection_id`` instead of the legacy ``source_id`` so a Target push
    audit cascades away with its Target endpoint too. Audit history is not
    required to outlive the endpoint (D-03).
    """

    __tablename__ = "plugin_jira_sync_audit_logs"
    __table_args__ = (
        Index(
            "ix_plugin_jira_sync_audit_logs_connection_id", "connection_id"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plugin_jira_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    jira_updated_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    indramesh_updated_at: Mapped[str | None] = mapped_column(String(50), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("now()"),
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )