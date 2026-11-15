"""The three support tables: ``issues``, ``issue_events``, ``issue_attachments``.

They hang off their own ``SupportBase`` metadata, so they never mix with the host's models.
Create them with ``litestar_feedback.support.migration.create_tables`` inside an Alembic
migration, or with ``SupportBase.metadata.create_all`` in tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from litestar_feedback._sqltypes import JSONColumn, UTCDateTime

KEY_LENGTH = 64
"""Width of the ``scope_id`` and ``user_id`` columns. Fits a UUID or a big integer as text."""


class IssueStatus(StrEnum):
    """Where an issue is in its life. See ``ALLOWED_TRANSITIONS`` in ``service``."""

    OPEN = "open"
    WAITING_CUSTOMER = "waiting_customer"
    RESOLVED = "resolved"
    CLOSED = "closed"


class IssueKind(StrEnum):
    """What the issue is about. The operator sets it after triage; the widget may leave it empty."""

    BUG = "bug"
    FEATURE_REQUEST = "feature_request"
    QUESTION = "question"
    IDEA = "idea"


class Side(StrEnum):
    """Who wrote an event."""

    CUSTOMER = "customer"
    OPERATOR = "operator"


class EventType(StrEnum):
    """What an event records."""

    OPENED = "opened"
    COMMENT = "comment"
    STATUS_CHANGE = "status_change"


class SupportBase(DeclarativeBase):
    """Declarative base that owns the support tables' metadata."""


class Issue(SupportBase):
    """One customer issue: the header of a thread.

    Never write ``status`` directly. ``SupportService.set_status`` checks the transition and
    writes the ``status_change`` event.
    """

    __tablename__ = "issues"
    __table_args__ = (
        # One live issue per (scope, dedupe_key). Closed issues do not count. (ISSUES-STANDARD)
        Index(
            "uq_issues_scope_dedupe_live",
            "scope_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status <> 'closed' AND dedupe_key IS NOT NULL"),
            sqlite_where=text("status <> 'closed' AND dedupe_key IS NOT NULL"),
        ),
        # The timer sweep reads issues by status and age.
        Index("ix_issues_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(KEY_LENGTH), index=True)
    user_id: Mapped[str | None] = mapped_column(String(KEY_LENGTH), index=True)
    kind: Mapped[str | None] = mapped_column(String(24))
    title: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(20))
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    register_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class IssueEvent(SupportBase):
    """One item in a thread: the opening message, a comment, or a status change.

    ``seq`` numbers the events of one issue from 1. It orders the thread even when two events
    share a timestamp, and ``last_seen_event_id`` checks compare against the highest ``seq``.

    ``internal`` events are operator notes. Customer routes never return them.
    """

    __tablename__ = "issue_events"
    __table_args__ = (UniqueConstraint("issue_id", "seq", name="uq_issue_events_issue_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    author_side: Mapped[str] = mapped_column(String(10))
    event_type: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    internal: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notify_skipped_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class IssueAttachment(SupportBase):
    """An uploaded image, staged until an event claims it.

    The bytes live in the host's object store under ``object_key``. ``event_id`` is NULL while
    the upload is staged. ``scope_id`` and ``user_id`` record the uploader, so only they can
    attach it.
    """

    __tablename__ = "issue_attachments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope_id: Mapped[str] = mapped_column(String(KEY_LENGTH), index=True)
    user_id: Mapped[str | None] = mapped_column(String(KEY_LENGTH), index=True)
    issue_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("issue_events.id", ondelete="CASCADE"), index=True
    )
    object_key: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(60))
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
