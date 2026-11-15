"""Support inbox rules: the state machine, timers, rate limit, attachments.

``SupportService`` works on one ``AsyncSession`` and never commits. The caller's transaction
(``SupportAdapter.session``) commits or rolls back.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller
from litestar_feedback.support.adapter import (
    AttachmentStore,
    Mailer,
    ReplyNotice,
    SupportConfig,
)
from litestar_feedback.support.models import (
    EventType,
    Issue,
    IssueAttachment,
    IssueEvent,
    IssueKind,
    IssueStatus,
    Side,
)

logger = logging.getLogger(__name__)

ALLOWED_TRANSITIONS: Mapping[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.OPEN: frozenset(
        {IssueStatus.WAITING_CUSTOMER, IssueStatus.RESOLVED, IssueStatus.CLOSED}
    ),
    IssueStatus.WAITING_CUSTOMER: frozenset(
        {IssueStatus.OPEN, IssueStatus.RESOLVED, IssueStatus.CLOSED}
    ),
    IssueStatus.RESOLVED: frozenset({IssueStatus.OPEN, IssueStatus.CLOSED}),
    IssueStatus.CLOSED: frozenset(),
}
"""Every allowed status move. Anything else is a ``TransitionError`` (HTTP 409)."""

REPLAY_CONTEXT_KEYS: frozenset[str] = frozenset({"replay_session_id", "replay_offset_ms"})
"""``context`` keys that point into a session replay. Customer responses leave them out."""


class SupportError(Exception):
    """Base class of the errors the routes turn into HTTP responses.

    ``code`` is a stable string in the error body, so a client can tell two errors with the
    same HTTP status apart.
    """

    code = "support_error"


class IssueNotFound(SupportError):
    """No issue with that id in the caller's scope (HTTP 404)."""

    code = "issue_not_found"


class TransitionError(SupportError):
    """The status move is not in ``ALLOWED_TRANSITIONS`` (HTTP 409)."""

    code = "transition_not_allowed"


class IssueClosed(TransitionError):
    """An operator comment on a closed issue (HTTP 409). A customer comment opens a follow-up."""

    code = "issue_closed"


class StaleThread(SupportError):
    """``last_seen_event_id`` is not the newest event: the thread moved (HTTP 409)."""

    code = "stale_thread"


class RateLimited(SupportError):
    """The actor posted ``rate_limit_count`` messages inside the window (HTTP 429)."""

    code = "rate_limited"

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__("too many messages, try again later")
        self.retry_after = retry_after


class AttachmentRejected(SupportError):
    """The upload or the attachment ids are not acceptable (HTTP 422)."""

    code = "attachment_rejected"


class AttachmentsUnavailable(SupportError):
    """No ``AttachmentStore`` is configured (HTTP 503)."""

    code = "attachments_unavailable"


@dataclass(frozen=True, slots=True)
class NewIssue:
    """What a customer sends to open an issue."""

    body: str
    kind: str | None = None
    title: str | None = None
    source: str = "widget"
    context: Mapping[str, Any] | None = None
    dedupe_key: str | None = None
    attachment_ids: Sequence[uuid.UUID] = ()


@dataclass(frozen=True, slots=True)
class IssueSummary:
    """An issue plus whether the newest public event came from the operator."""

    issue: Issue
    has_unread_reply: bool


@dataclass(frozen=True, slots=True)
class Thread:
    """An issue with its events and their attachments, in ``seq`` order."""

    issue: Issue
    events: Sequence[IssueEvent]
    attachments: Mapping[uuid.UUID, Sequence[IssueAttachment]]

    @property
    def has_unread_reply(self) -> bool:
        """True when the newest public event was written by the operator."""
        public = [e for e in self.events if not e.internal]
        return bool(public) and public[-1].author_side == Side.OPERATOR


def _status(issue: Issue) -> IssueStatus:
    return IssueStatus(issue.status)


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened when one reply notice went to the mailer, after the commit."""

    event_id: uuid.UUID
    notified_at: datetime | None
    skipped: str | None


@dataclass(frozen=True, slots=True)
class SupportService:
    """Reads and writes the support tables inside one session.

    Reply mail is never sent from here. ``outbox`` collects the notices, and the caller sends
    them with ``deliver`` after the transaction commits, so a rolled back reply sends no mail.
    """

    session: AsyncSession
    config: SupportConfig
    storage: AttachmentStore | None = None
    mailer: Mailer | None = None
    outbox: list[ReplyNotice] = field(default_factory=list[ReplyNotice])

    def _now(self) -> datetime:
        return self.config.clock()

    # -- reads ---------------------------------------------------------------------------

    async def get(self, issue_id: uuid.UUID, *, scope_id: str | None) -> Issue:
        """Return the issue, applying due timers first.

        ``scope_id=None`` means any scope (operators). Another scope's issue raises
        ``IssueNotFound``, the same as a missing one, so ids do not leak across scopes.
        """
        issue = await self.session.get(Issue, issue_id, with_for_update=False)
        if issue is None or (scope_id is not None and issue.scope_id != scope_id):
            raise IssueNotFound(f"issue {issue_id} not found")
        await self.apply_timer(issue)
        return issue

    async def thread(self, issue: Issue, *, include_internal: bool) -> Thread:
        """Return the issue's events (internal ones only when asked) and their attachments."""
        query = select(IssueEvent).where(IssueEvent.issue_id == issue.id)
        if not include_internal:
            query = query.where(IssueEvent.internal.is_(False))
        events = list((await self.session.scalars(query.order_by(IssueEvent.seq))).all())
        attachments: dict[uuid.UUID, list[IssueAttachment]] = {}
        if events:
            rows = await self.session.scalars(
                select(IssueAttachment)
                .where(IssueAttachment.event_id.in_([e.id for e in events]))
                .order_by(IssueAttachment.created_at)
            )
            for row in rows:
                assert row.event_id is not None
                attachments.setdefault(row.event_id, []).append(row)
        return Thread(issue=issue, events=events, attachments=attachments)

    async def list_issues(
        self,
        *,
        scope_id: str | None,
        status: IssueStatus | None = None,
        limit: int,
        offset: int = 0,
    ) -> list[IssueSummary]:
        """List issues newest first. ``scope_id=None`` lists every scope (operators)."""
        await self.sweep_timers(scope_id=scope_id)
        query = select(Issue)
        if scope_id is not None:
            query = query.where(Issue.scope_id == scope_id)
        if status is not None:
            query = query.where(Issue.status == status.value)
        query = query.order_by(Issue.updated_at.desc(), Issue.id).limit(limit).offset(offset)
        issues = list((await self.session.scalars(query)).all())
        unread = await self._unread([i.id for i in issues])
        return [IssueSummary(issue=i, has_unread_reply=i.id in unread) for i in issues]

    async def _unread(self, issue_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        """Return the ids whose newest public event is the operator's."""
        if not issue_ids:
            return set()
        newest = (
            select(IssueEvent.issue_id, func.max(IssueEvent.seq).label("seq"))
            .where(IssueEvent.issue_id.in_(issue_ids), IssueEvent.internal.is_(False))
            .group_by(IssueEvent.issue_id)
            .subquery()
        )
        rows = await self.session.execute(
            select(IssueEvent.issue_id)
            .join(
                newest,
                and_(IssueEvent.issue_id == newest.c.issue_id, IssueEvent.seq == newest.c.seq),
            )
            .where(IssueEvent.author_side == Side.OPERATOR.value)
        )
        return set(rows.scalars().all())

    # -- writes --------------------------------------------------------------------------

    async def open_issue(self, caller: Caller, new: NewIssue) -> tuple[Issue, bool]:
        """Open an issue. Return ``(issue, created)``.

        A ``dedupe_key`` that matches a live (not closed) issue in the same scope returns that
        issue with ``created=False`` and writes nothing.

        """
        if new.dedupe_key is not None:
            existing = await self.session.scalar(
                select(Issue).where(
                    Issue.scope_id == caller.scope_id,
                    Issue.dedupe_key == new.dedupe_key,
                    Issue.status != IssueStatus.CLOSED.value,
                )
            )
            if existing is not None:
                return existing, False
        await self._check_rate_limit(caller)
        now = self._now()
        issue = Issue(
            id=uuid.uuid4(),
            scope_id=caller.scope_id,
            user_id=caller.user_id,
            kind=new.kind,
            title=new.title if new.title is not None else _title_from(new.body),
            body=new.body,
            source=new.source,
            status=IssueStatus.OPEN.value,
            context=dict(new.context) if new.context is not None else None,
            dedupe_key=new.dedupe_key,
            created_at=now,
            updated_at=now,
        )
        self.session.add(issue)
        event = await self._append(
            issue,
            side=Side.CUSTOMER,
            event_type=EventType.OPENED,
            actor=caller.actor,
            body=new.body,
        )
        await self._bind(caller, issue, event, new.attachment_ids)
        return issue, True

    async def import_issue(
        self,
        *,
        scope_id: str,
        user_id: str | None,
        actor: str,
        body: str,
        created_at: datetime,
        status: IssueStatus = IssueStatus.OPEN,
        kind: str | None = None,
        title: str | None = None,
        source: str = "widget",
        context: Mapping[str, Any] | None = None,
    ) -> Issue:
        """Insert a historical issue, for example when migrating an old feedback table.

        The ``opened`` event gets ``notify_skipped_reason='seeded'``. No mail is sent and no
        rate limit applies.
        """
        issue = Issue(
            id=uuid.uuid4(),
            scope_id=scope_id,
            user_id=user_id,
            kind=kind,
            title=title if title is not None else _title_from(body),
            body=body,
            source=source,
            status=status.value,
            context=dict(context) if context is not None else None,
            created_at=created_at,
            updated_at=created_at,
        )
        self.session.add(issue)
        await self._append(
            issue,
            side=Side.CUSTOMER,
            event_type=EventType.OPENED,
            actor=actor,
            body=body,
            at=created_at,
            skipped="seeded",
        )
        return issue

    async def comment(
        self,
        issue: Issue,
        caller: Caller,
        *,
        side: Side,
        body: str,
        internal: bool = False,
        attachment_ids: Sequence[uuid.UUID] = (),
    ) -> IssueEvent:
        """Add a comment and move the status to whoever must act next. Return the new event.

        A customer comment moves ``waiting_customer`` or ``resolved`` to ``open``. A public
        operator comment moves ``open`` to ``waiting_customer`` and queues a mail to the
        customer. An internal note changes no status.

        On a closed issue, a customer comment opens a new issue that follows it, and the
        returned event is that issue's ``opened`` event (its ``issue_id`` is the new issue).
        An operator comment on a closed issue raises ``IssueClosed``. (ISSUES-STANDARD)
        """
        if _status(issue) is IssueStatus.CLOSED:
            if side is Side.OPERATOR:
                raise IssueClosed("this issue is closed")
            return await self._follow_up(issue, caller, body, attachment_ids)
        if side is Side.CUSTOMER:
            await self._check_rate_limit(caller)
        event = await self._append(
            issue,
            side=side,
            event_type=EventType.COMMENT,
            actor=caller.actor,
            body=body,
            internal=internal,
            skipped="internal" if internal else None,
        )
        await self._bind(caller, issue, event, attachment_ids)
        if not internal:
            target = IssueStatus.OPEN if side is Side.CUSTOMER else IssueStatus.WAITING_CUSTOMER
            if target is not _status(issue) and target in ALLOWED_TRANSITIONS[_status(issue)]:
                await self._move(issue, target, actor=caller.actor, side=side)
        if side is Side.OPERATOR and not internal:
            await self._notify(issue, event)
        return event

    async def _follow_up(
        self, closed: Issue, caller: Caller, body: str, attachment_ids: Sequence[uuid.UUID]
    ) -> IssueEvent:
        """Open a new issue with ``follows_id`` set to ``closed``, starting with ``body``.

        Scope, reporter, kind and source come from the closed issue. A closed issue whose
        reporter was deleted gets the caller as reporter.
        """
        await self._check_rate_limit(caller)
        now = self._now()
        issue = Issue(
            id=uuid.uuid4(),
            scope_id=closed.scope_id,
            user_id=closed.user_id if closed.user_id is not None else caller.user_id,
            kind=closed.kind,
            title=_title_from(body),
            body=body,
            source=closed.source,
            status=IssueStatus.OPEN.value,
            follows_id=closed.id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(issue)
        event = await self._append(
            issue, side=Side.CUSTOMER, event_type=EventType.OPENED, actor=caller.actor, body=body
        )
        await self._bind(caller, issue, event, attachment_ids)
        return event

    async def record(self, deliveries: Iterable[Delivery]) -> None:
        """Write the mailer outcomes of ``deliver`` onto their events."""
        for d in deliveries:
            await self.session.execute(
                update(IssueEvent)
                .where(IssueEvent.id == d.event_id)
                .values(notified_at=d.notified_at, notify_skipped_reason=d.skipped)
            )

    async def resolve_as_customer(
        self, issue: Issue, caller: Caller, *, last_seen_event_id: uuid.UUID
    ) -> None:
        """Customer marks the issue resolved. Refuses when the thread moved since their read."""
        await self.set_status(
            issue,
            IssueStatus.RESOLVED,
            actor=caller.actor,
            side=Side.CUSTOMER,
            last_seen_event_id=last_seen_event_id,
        )

    async def set_status(
        self,
        issue: Issue,
        to: IssueStatus,
        *,
        actor: str,
        side: Side,
        last_seen_event_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> None:
        """Move the issue to ``to`` and write a ``status_change`` event.

        ``reason`` is written first as an internal note, so the operator sees why an issue was
        closed as spam or duplicate.

        ``last_seen_event_id`` is compared with the newest event that ``side`` can see: a
        customer never sees internal notes, so they do not make a customer's view stale.
        """
        if last_seen_event_id is not None:
            newest_query = select(IssueEvent.id).where(IssueEvent.issue_id == issue.id)
            if side is Side.CUSTOMER:
                newest_query = newest_query.where(IssueEvent.internal.is_(False))
            newest = await self.session.scalar(
                newest_query.order_by(IssueEvent.seq.desc()).limit(1)
            )
            if newest != last_seen_event_id:
                raise StaleThread("the thread moved since you last read it")
        if to not in ALLOWED_TRANSITIONS[_status(issue)]:
            raise TransitionError(f"cannot move an issue from {issue.status} to {to.value}")
        if reason:
            await self._append(
                issue,
                side=side,
                event_type=EventType.COMMENT,
                actor=actor,
                body=reason,
                internal=True,
                skipped="internal",
            )
        await self._move(issue, to, actor=actor, side=side)

    async def set_kind(self, issue: Issue, kind: IssueKind | None, *, actor: str) -> None:
        """Set the issue's ``kind`` after triage, and record the change as an internal note.

        Allowed in every status, closed included. The note does not change ``updated_at``, so
        triage never restarts a 14-day timer. Setting the same kind again writes nothing.
        """
        new = kind.value if kind is not None else None
        if new == issue.kind:
            return
        await self._append(
            issue,
            side=Side.OPERATOR,
            event_type=EventType.COMMENT,
            actor=actor,
            body=f"kind: {issue.kind or 'none'} -> {new or 'none'}",
            internal=True,
            skipped="internal",
            touch=False,
        )
        issue.kind = new
        await self.session.flush()

    async def set_register_ref(self, issue: Issue, register_ref: str | None) -> None:
        """Record the register entry the bridge filed for this issue."""
        issue.register_ref = register_ref
        await self.session.flush()

    # -- timers --------------------------------------------------------------------------

    def _due(self) -> ColumnElement[bool]:
        now = self._now()
        return or_(
            and_(
                Issue.status == IssueStatus.WAITING_CUSTOMER.value,
                Issue.updated_at <= now - self.config.waiting_to_resolved,
            ),
            and_(
                Issue.status == IssueStatus.RESOLVED.value,
                Issue.updated_at <= now - self.config.resolved_to_closed,
            ),
        )

    async def apply_timer(self, issue: Issue) -> bool:
        """Apply the 14-day timers to one issue. Return True when it moved.

        It moves at most one step. The step sets ``updated_at`` to now, so a ``resolved``
        issue made by the timer waits its own full period before it closes.
        """
        now = self._now()
        status = _status(issue)
        if (
            status is IssueStatus.WAITING_CUSTOMER
            and now - issue.updated_at >= self.config.waiting_to_resolved
        ):
            target = IssueStatus.RESOLVED
        elif (
            status is IssueStatus.RESOLVED
            and now - issue.updated_at >= self.config.resolved_to_closed
        ):
            target = IssueStatus.CLOSED
        else:
            return False
        await self._move(issue, target, actor=self.config.system_actor, side=Side.OPERATOR)
        return True

    async def sweep_timers(self, *, scope_id: str | None = None) -> int:
        """Apply due timers to every issue (or one scope). Return how many moved.

        The list routes call this. A host with a scheduler can also call it on a schedule.
        """
        query = select(Issue).where(self._due())
        if scope_id is not None:
            query = query.where(Issue.scope_id == scope_id)
        moved = 0
        for issue in (await self.session.scalars(query)).all():
            moved += int(await self.apply_timer(issue))
        return moved

    # -- attachments ---------------------------------------------------------------------

    async def upload(self, caller: Caller, *, data: bytes, mime_type: str) -> IssueAttachment:
        """Store an image and record it as staged. It is attached by a later issue or comment."""
        if self.storage is None:
            raise AttachmentsUnavailable("attachment storage is not configured")
        if mime_type not in self.config.allowed_mime_types:
            raise AttachmentRejected(f"{mime_type} is not an accepted image type")
        if not data:
            raise AttachmentRejected("the file is empty")
        if len(data) > self.config.max_attachment_bytes:
            raise AttachmentRejected(
                f"images must be at most {self.config.max_attachment_bytes} bytes"
            )
        attachment_id = uuid.uuid4()
        key = f"issue-attachments/{caller.scope_id}/{attachment_id}"
        await self.storage.put(key=key, data=data, content_type=mime_type)
        row = IssueAttachment(
            id=attachment_id,
            scope_id=caller.scope_id,
            user_id=caller.user_id,
            object_key=key,
            mime_type=mime_type,
            size_bytes=len(data),
            created_at=self._now(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def view_url(self, attachment: IssueAttachment) -> str | None:
        """Return a download URL, or None when no store is configured."""
        if self.storage is None:
            return None
        return await self.storage.presign_get(key=attachment.object_key)

    async def reap_staged(self) -> int:
        """Delete uploads that no event claimed within ``staged_ttl``. Return the count."""
        cutoff = self._now() - self.config.staged_ttl
        rows = (
            await self.session.scalars(
                select(IssueAttachment).where(
                    IssueAttachment.event_id.is_(None), IssueAttachment.created_at < cutoff
                )
            )
        ).all()
        await self._delete_attachments(rows)
        return len(rows)

    async def forget_user(self, user_id: str) -> int:
        """Delete a user's screenshots and unlink their issues. Return the attachments deleted.

        Issues and events stay for the operator, with ``user_id`` set to NULL. Call it when the
        product deletes the user. (ISSUES-STANDARD: screenshots follow account deletion.)
        """
        rows = (
            await self.session.scalars(
                select(IssueAttachment).where(IssueAttachment.user_id == user_id)
            )
        ).all()
        await self._delete_attachments(rows)
        await self.session.execute(
            update(Issue).where(Issue.user_id == user_id).values(user_id=None)
        )
        return len(rows)

    async def _delete_attachments(self, rows: Sequence[IssueAttachment]) -> None:
        if not rows:
            return
        if self.storage is not None:
            for row in rows:
                await self.storage.delete(key=row.object_key)
        await self.session.execute(
            delete(IssueAttachment).where(IssueAttachment.id.in_([r.id for r in rows]))
        )

    # -- internals -----------------------------------------------------------------------

    async def _append(
        self,
        issue: Issue,
        *,
        side: Side,
        event_type: EventType,
        actor: str,
        body: str | None,
        internal: bool = False,
        skipped: str | None = None,
        at: datetime | None = None,
        touch: bool = True,
    ) -> IssueEvent:
        """Write the next event of the issue. ``touch=False`` leaves ``updated_at`` alone."""
        await self.session.flush()
        last = await self.session.scalar(
            select(func.max(IssueEvent.seq)).where(IssueEvent.issue_id == issue.id)
        )
        now = at if at is not None else self._now()
        event = IssueEvent(
            id=uuid.uuid4(),
            issue_id=issue.id,
            seq=(last or 0) + 1,
            author_side=side.value,
            event_type=event_type.value,
            actor=actor,
            body=body,
            internal=internal,
            notify_skipped_reason=skipped,
            created_at=now,
        )
        self.session.add(event)
        if touch:
            issue.updated_at = now
        await self.session.flush()
        return event

    async def _move(self, issue: Issue, to: IssueStatus, *, actor: str, side: Side) -> None:
        await self._append(
            issue,
            side=side,
            event_type=EventType.STATUS_CHANGE,
            actor=actor,
            body=to.value,
            skipped="status_change",
        )
        issue.status = to.value
        await self.session.flush()

    async def _bind(
        self, caller: Caller, issue: Issue, event: IssueEvent, ids: Iterable[uuid.UUID]
    ) -> None:
        wanted = set(ids)
        if not wanted:
            return
        rows = (
            await self.session.scalars(
                select(IssueAttachment).where(
                    IssueAttachment.id.in_(wanted),
                    IssueAttachment.scope_id == caller.scope_id,
                    IssueAttachment.user_id == caller.user_id
                    if caller.user_id is not None
                    else IssueAttachment.user_id.is_(None),
                    IssueAttachment.event_id.is_(None),
                )
            )
        ).all()
        if {r.id for r in rows} != wanted:
            raise AttachmentRejected("unknown or already used attachment id")
        for row in rows:
            row.event_id = event.id
            row.issue_id = issue.id
        await self.session.flush()

    async def _check_rate_limit(self, caller: Caller) -> None:
        if caller.is_operator or self.config.rate_limit_count <= 0:
            return
        since = self._now() - self.config.rate_limit_window
        recent = await self.session.scalars(
            select(IssueEvent.created_at)
            .where(
                IssueEvent.actor == caller.actor,
                IssueEvent.author_side == Side.CUSTOMER.value,
                IssueEvent.event_type.in_([EventType.OPENED.value, EventType.COMMENT.value]),
                IssueEvent.notify_skipped_reason.is_distinct_from("seeded"),
                IssueEvent.created_at > since,
            )
            .order_by(IssueEvent.created_at)
        )
        times = list(recent.all())
        if len(times) >= self.config.rate_limit_count:
            oldest = times[len(times) - self.config.rate_limit_count]
            raise RateLimited(retry_after=oldest + self.config.rate_limit_window - self._now())

    async def _notify(self, issue: Issue, event: IssueEvent) -> None:
        if self.mailer is None:
            event.notify_skipped_reason = "no_mailer"
            return
        event.notify_skipped_reason = "pending"
        notice = ReplyNotice(
            issue_id=issue.id,
            event_id=event.id,
            scope_id=issue.scope_id,
            user_id=issue.user_id,
            issue_title=issue.title,
            reply_body=event.body or "",
            operator=event.actor,
        )
        self.outbox.append(notice)


async def deliver(
    mailer: Mailer | None, notices: Iterable[ReplyNotice], config: SupportConfig
) -> list[Delivery]:
    """Send each notice and return what happened. Call it only after the commit.

    A mailer exception is logged and recorded as ``send_failed``; it never raises.
    """
    if mailer is None:
        return []
    out: list[Delivery] = []
    for notice in notices:
        try:
            sent = await mailer.send_reply(notice)
        except Exception:
            logger.exception(
                "mailer failed for issue %s event %s", notice.issue_id, notice.event_id
            )
            out.append(Delivery(notice.event_id, None, "send_failed"))
            continue
        if sent:
            out.append(Delivery(notice.event_id, config.clock(), None))
        else:
            out.append(Delivery(notice.event_id, None, "no_email"))
    return out


def _title_from(body: str) -> str:
    """Return the first line of ``body``, cut to 200 characters (the widget sends no title)."""
    first = body.strip().splitlines()[0] if body.strip() else ""
    return first[:200]


async def referenced_replay_sessions(session: AsyncSession) -> set[uuid.UUID]:
    """Return the replay session ids that live issues point to.

    Pass this to ``ReplayAdapter.protected_sessions`` so the replay prune keeps them. Closed
    issues do not protect their replay. Values that are not UUIDs are skipped, not fatal.
    """
    rows = await session.scalars(
        select(Issue.context).where(
            Issue.status != IssueStatus.CLOSED.value, Issue.context.is_not(None)
        )
    )
    found: set[uuid.UUID] = set()
    for context in rows:
        raw = context.get("replay_session_id") if context else None
        if isinstance(raw, str):
            try:
                found.add(uuid.UUID(raw))
            except ValueError:
                continue
    return found
