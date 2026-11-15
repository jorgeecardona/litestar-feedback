"""Request and response bodies of the support routes.

``msgspec`` structs, so Litestar validates them with no extra dependency. A body that fails
validation gets HTTP 400.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import msgspec

from litestar_feedback.support.models import IssueKind, IssueStatus

Body = Annotated[str, msgspec.Meta(min_length=1, max_length=10_000)]
Title = Annotated[str, msgspec.Meta(max_length=200)]
Source = Annotated[str, msgspec.Meta(min_length=1, max_length=16)]
DedupeKey = Annotated[str, msgspec.Meta(min_length=1, max_length=200)]
Reason = Annotated[str, msgspec.Meta(max_length=2_000)]


class IssueCreate(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /issues``.

    ``context`` holds page data from the widget: ``page_url``, ``viewport_w``, ``viewport_h``,
    ``user_agent``, ``replay_session_id``, ``replay_offset_ms``, ``version``.

    ``follows_id`` is a closed issue in the caller's scope that this issue continues. A comment
    on a closed issue answers 409 with ``code: "issue_closed"``; the client then opens a new
    issue with ``follows_id``.
    """

    body: Body
    kind: IssueKind | None = None
    title: Title | None = None
    source: Source = "widget"
    context: dict[str, Any] | None = None
    dedupe_key: DedupeKey | None = None
    follows_id: uuid.UUID | None = None
    attachment_ids: list[uuid.UUID] = msgspec.field(default_factory=list[uuid.UUID])


class CommentCreate(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /issues/{id}/comments``."""

    body: Body
    attachment_ids: list[uuid.UUID] = msgspec.field(default_factory=list[uuid.UUID])


class OperatorCommentCreate(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /admin/issues/{id}/comments``. ``internal=true`` writes a private note."""

    body: Body
    internal: bool = False
    attachment_ids: list[uuid.UUID] = msgspec.field(default_factory=list[uuid.UUID])


class ResolveRequest(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /issues/{id}/resolve``. The id of the newest event the customer saw."""

    last_seen_event_id: uuid.UUID


class StatusChange(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /admin/issues/{id}/status``.

    ``reason`` is stored as an internal note before the status change.
    """

    to: IssueStatus
    last_seen_event_id: uuid.UUID
    reason: Reason | None = None


class AttachmentOut(msgspec.Struct, frozen=True, kw_only=True):
    """An attachment. ``url`` is a short-lived download link, or None without a store."""

    id: uuid.UUID
    mime_type: str
    size_bytes: int
    url: str | None


class EventOut(msgspec.Struct, frozen=True, kw_only=True):
    """One event of a thread.

    ``notified_at`` and ``notify_skipped_reason`` are only filled for operators.
    """

    id: uuid.UUID
    issue_id: uuid.UUID
    seq: int
    author_side: str
    event_type: str
    actor: str
    body: str | None
    internal: bool
    created_at: datetime
    attachments: list[AttachmentOut]
    notified_at: datetime | None = None
    notify_skipped_reason: str | None = None


class IssueOut(msgspec.Struct, frozen=True, kw_only=True):
    """An issue. ``events`` is empty on list routes.

    For customers, ``context`` leaves out the replay keys and ``dedupe_key`` and
    ``register_ref`` are None.
    """

    id: uuid.UUID
    scope_id: str
    user_id: str | None
    kind: str | None
    title: str | None
    body: str
    source: str
    status: str
    context: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    has_unread_reply: bool
    events: list[EventOut]
    follows_id: uuid.UUID | None = None
    dedupe_key: str | None = None
    register_ref: str | None = None
