"""The support route handlers. ``SupportPlugin`` mounts them under ``SupportConfig.path``."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from litestar import Request, Response, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency
from litestar.exceptions import PermissionDeniedException
from litestar.params import FromPath, FromQuery, JSONBody, MultipartBody, QueryParameter
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
)
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller
from litestar_feedback.support.adapter import SupportAdapter, SupportConfig
from litestar_feedback.support.models import IssueAttachment, IssueEvent, IssueStatus, Side
from litestar_feedback.support.schemas import (
    AttachmentOut,
    CommentCreate,
    EventOut,
    IssueCreate,
    IssueOut,
    KindChange,
    OperatorCommentCreate,
    ResolveRequest,
    StatusChange,
)
from litestar_feedback.support.service import (
    REPLAY_CONTEXT_KEYS,
    AttachmentRejected,
    AttachmentsUnavailable,
    IssueNotFound,
    IssueSummary,
    NewIssue,
    RateLimited,
    StaleThread,
    SupportError,
    SupportService,
    Thread,
    TransitionError,
)

audit = logging.getLogger("litestar_feedback.support.audit")

DEFAULT_PAGE = 50

AnyRequest = Request[Any, Any, Any]
IssueId = FromPath[uuid.UUID]


@dataclass(frozen=True, slots=True)
class SupportRuntime:
    """The adapter and config, handed to every route as the ``feedback_support`` dependency."""

    adapter: SupportAdapter
    config: SupportConfig

    def service(self, session: AsyncSession) -> SupportService:
        """Return a ``SupportService`` bound to ``session``."""
        return SupportService(
            session=session,
            config=self.config,
            storage=self.adapter.storage,
            mailer=self.adapter.mailer,
        )

    async def operator(self, request: AnyRequest) -> Caller:
        """Return the caller, or raise 403 when they are not an operator."""
        caller = await self.adapter.caller(request)
        if not caller.is_operator:
            raise PermissionDeniedException("operator access required")
        return caller

    def page(self, limit: int | None) -> int:
        """Clamp a requested page size to ``1..config.list_limit``."""
        return max(1, min(limit or DEFAULT_PAGE, self.config.list_limit))


Runtime = NamedDependency[SupportRuntime]


# -- rendering ---------------------------------------------------------------------------


async def _attachment_out(svc: SupportService, row: IssueAttachment) -> AttachmentOut:
    return AttachmentOut(
        id=row.id, mime_type=row.mime_type, size_bytes=row.size_bytes, url=await svc.view_url(row)
    )


async def _event_out(
    svc: SupportService,
    event: IssueEvent,
    attachments: list[IssueAttachment] | tuple[IssueAttachment, ...],
    *,
    operator: bool,
) -> EventOut:
    return EventOut(
        id=event.id,
        issue_id=event.issue_id,
        seq=event.seq,
        author_side=event.author_side,
        event_type=event.event_type,
        actor=event.actor,
        body=event.body,
        internal=event.internal,
        created_at=event.created_at,
        attachments=[await _attachment_out(svc, a) for a in attachments],
        notified_at=event.notified_at if operator else None,
        notify_skipped_reason=event.notify_skipped_reason if operator else None,
    )


def _issue_out(summary: IssueSummary, events: list[EventOut], *, operator: bool) -> IssueOut:
    issue = summary.issue
    context = issue.context
    if context is not None and not operator:
        context = {k: v for k, v in context.items() if k not in REPLAY_CONTEXT_KEYS}
    return IssueOut(
        id=issue.id,
        scope_id=issue.scope_id,
        user_id=issue.user_id,
        kind=issue.kind,
        title=issue.title,
        body=issue.body,
        source=issue.source,
        status=issue.status,
        context=context,
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        has_unread_reply=summary.has_unread_reply,
        events=events,
        follows_id=issue.follows_id,
        dedupe_key=issue.dedupe_key if operator else None,
        register_ref=issue.register_ref if operator else None,
    )


async def _thread_out(svc: SupportService, thread: Thread, *, operator: bool) -> IssueOut:
    events = [
        await _event_out(svc, e, list(thread.attachments.get(e.id, ())), operator=operator)
        for e in thread.events
    ]
    summary = IssueSummary(issue=thread.issue, has_unread_reply=thread.has_unread_reply)
    return _issue_out(summary, events, operator=operator)


# -- customer routes ---------------------------------------------------------------------


@post("/issues", status_code=HTTP_201_CREATED)
async def create_issue(
    request: AnyRequest, data: JSONBody[IssueCreate], feedback_support: Runtime
) -> Response[IssueOut]:
    """Open an issue. A ``dedupe_key`` that matches a live issue returns it with 200."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue, created = await svc.open_issue(
            caller,
            NewIssue(
                body=data.body,
                kind=data.kind.value if data.kind is not None else None,
                title=data.title,
                source=data.source,
                context=data.context,
                dedupe_key=data.dedupe_key,
                follows_id=data.follows_id,
                attachment_ids=tuple(data.attachment_ids),
            ),
        )
        out = await _thread_out(
            svc, await svc.thread(issue, include_internal=False), operator=False
        )
    return Response(out, status_code=HTTP_201_CREATED if created else HTTP_200_OK)


@get("/issues")
async def list_issues(
    request: AnyRequest,
    feedback_support: Runtime,
    status: FromQuery[IssueStatus | None] = None,
    limit: Annotated[int | None, QueryParameter(ge=1)] = None,
    offset: Annotated[int, QueryParameter(ge=0)] = 0,
) -> list[IssueOut]:
    """List the caller's scope's issues, newest first."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    async with rt.adapter.session(caller) as session:
        rows = await rt.service(session).list_issues(
            scope_id=caller.scope_id, status=status, limit=rt.page(limit), offset=offset
        )
        return [_issue_out(r, [], operator=False) for r in rows]


@get("/issues/{issue_id:uuid}")
async def get_issue(request: AnyRequest, issue_id: IssueId, feedback_support: Runtime) -> IssueOut:
    """Return one issue with its public events."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=caller.scope_id)
        return await _thread_out(
            svc, await svc.thread(issue, include_internal=False), operator=False
        )


@post("/issues/{issue_id:uuid}/comments", status_code=HTTP_201_CREATED)
async def comment_as_customer(
    request: AnyRequest,
    issue_id: IssueId,
    data: JSONBody[CommentCreate],
    feedback_support: Runtime,
) -> EventOut:
    """Add a customer comment. It reopens a ``resolved`` or ``waiting_customer`` issue."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=caller.scope_id)
        event = await svc.comment(
            issue,
            caller,
            side=Side.CUSTOMER,
            body=data.body,
            attachment_ids=tuple(data.attachment_ids),
        )
        thread = await svc.thread(issue, include_internal=False)
        return await _event_out(
            svc, event, list(thread.attachments.get(event.id, ())), operator=False
        )


@post("/issues/{issue_id:uuid}/resolve", status_code=HTTP_200_OK)
async def resolve_issue(
    request: AnyRequest,
    issue_id: IssueId,
    data: JSONBody[ResolveRequest],
    feedback_support: Runtime,
) -> IssueOut:
    """Customer marks the issue resolved. 409 when the thread moved since ``last_seen_event_id``."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=caller.scope_id)
        await svc.resolve_as_customer(issue, caller, last_seen_event_id=data.last_seen_event_id)
        return await _thread_out(
            svc, await svc.thread(issue, include_internal=False), operator=False
        )


@post("/issue-attachments", status_code=HTTP_201_CREATED)
async def upload_attachment(
    request: AnyRequest,
    data: MultipartBody[UploadFile],
    feedback_support: Runtime,
) -> AttachmentOut:
    """Stage an image. Send its id in ``attachment_ids`` of the next issue or comment."""
    rt = feedback_support
    caller = await rt.adapter.caller(request)
    raw = await data.read()
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        row = await svc.upload(caller, data=raw, mime_type=data.content_type)
        return await _attachment_out(svc, row)


# -- operator routes ---------------------------------------------------------------------


@get("/admin/issues")
async def admin_list_issues(
    request: AnyRequest,
    feedback_support: Runtime,
    status: FromQuery[IssueStatus | None] = None,
    limit: Annotated[int | None, QueryParameter(ge=1)] = None,
    offset: Annotated[int, QueryParameter(ge=0)] = 0,
) -> list[IssueOut]:
    """List issues across every scope, newest first."""
    rt = feedback_support
    caller = await rt.operator(request)
    audit.info("%s listed issues", caller.actor)
    async with rt.adapter.session(caller) as session:
        rows = await rt.service(session).list_issues(
            scope_id=None, status=status, limit=rt.page(limit), offset=offset
        )
        return [_issue_out(r, [], operator=True) for r in rows]


@get("/admin/issues/{issue_id:uuid}")
async def admin_get_issue(
    request: AnyRequest, issue_id: IssueId, feedback_support: Runtime
) -> IssueOut:
    """Return one issue with every event, internal notes included."""
    rt = feedback_support
    caller = await rt.operator(request)
    audit.info("%s viewed issue %s", caller.actor, issue_id)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=None)
        return await _thread_out(svc, await svc.thread(issue, include_internal=True), operator=True)


@post("/admin/issues/{issue_id:uuid}/comments", status_code=HTTP_201_CREATED)
async def comment_as_operator(
    request: AnyRequest,
    issue_id: IssueId,
    data: JSONBody[OperatorCommentCreate],
    feedback_support: Runtime,
) -> EventOut:
    """Reply to the customer, or write a private note with ``internal=true``."""
    rt = feedback_support
    caller = await rt.operator(request)
    audit.info("%s commented on issue %s internal=%s", caller.actor, issue_id, data.internal)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=None)
        event = await svc.comment(
            issue,
            caller,
            side=Side.OPERATOR,
            body=data.body,
            internal=data.internal,
            attachment_ids=tuple(data.attachment_ids),
        )
        thread = await svc.thread(issue, include_internal=True)
        return await _event_out(
            svc, event, list(thread.attachments.get(event.id, ())), operator=True
        )


@post("/admin/issues/{issue_id:uuid}/status", status_code=HTTP_200_OK)
async def set_status(
    request: AnyRequest,
    issue_id: IssueId,
    data: JSONBody[StatusChange],
    feedback_support: Runtime,
) -> IssueOut:
    """Move the issue to another status. 409 for a move not allowed or a stale view."""
    rt = feedback_support
    caller = await rt.operator(request)
    audit.info("%s set issue %s to %s", caller.actor, issue_id, data.to.value)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=None)
        await svc.set_status(
            issue,
            data.to,
            actor=caller.actor,
            side=Side.OPERATOR,
            last_seen_event_id=data.last_seen_event_id,
            reason=data.reason,
        )
        return await _thread_out(svc, await svc.thread(issue, include_internal=True), operator=True)


@post("/admin/issues/{issue_id:uuid}/kind", status_code=HTTP_200_OK)
async def set_kind(
    request: AnyRequest,
    issue_id: IssueId,
    data: JSONBody[KindChange],
    feedback_support: Runtime,
) -> IssueOut:
    """Set the issue's kind after triage. The change is written as an internal note."""
    rt = feedback_support
    caller = await rt.operator(request)
    kind = data.kind.value if data.kind is not None else None
    audit.info("%s set issue %s kind to %s", caller.actor, issue_id, kind)
    async with rt.adapter.session(caller) as session:
        svc = rt.service(session)
        issue = await svc.get(issue_id, scope_id=None)
        await svc.set_kind(issue, data.kind, actor=caller.actor)
        return await _thread_out(svc, await svc.thread(issue, include_internal=True), operator=True)


# -- errors ------------------------------------------------------------------------------

_STATUS_FOR: dict[type[SupportError], int] = {
    IssueNotFound: HTTP_404_NOT_FOUND,
    TransitionError: HTTP_409_CONFLICT,
    StaleThread: HTTP_409_CONFLICT,
    RateLimited: HTTP_429_TOO_MANY_REQUESTS,
    AttachmentRejected: HTTP_422_UNPROCESSABLE_ENTITY,
    AttachmentsUnavailable: HTTP_503_SERVICE_UNAVAILABLE,
}


def support_error_handler(_: AnyRequest, exc: Exception) -> Response[dict[str, Any]]:
    """Turn a ``SupportError`` into Litestar's ``{status_code, detail}`` body plus ``code``."""
    status = next(
        (status for kind, status in _STATUS_FOR.items() if isinstance(exc, kind)),
        HTTP_409_CONFLICT,
    )
    code = exc.code if isinstance(exc, SupportError) else SupportError.code
    headers: dict[str, str] = {}
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(max(1, math.ceil(exc.retry_after.total_seconds())))
    body = {"status_code": status, "detail": str(exc), "code": code}
    return Response(body, status_code=status, headers=headers)


ROUTE_HANDLERS = [
    create_issue,
    list_issues,
    get_issue,
    comment_as_customer,
    resolve_issue,
    upload_attachment,
    admin_list_issues,
    admin_get_issue,
    comment_as_operator,
    set_status,
    set_kind,
]
"""Every support route, in the order of ISSUES-STANDARD.md."""
