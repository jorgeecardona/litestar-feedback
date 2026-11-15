"""In-app customer support inbox for Litestar: issues, a reply thread, an operator inbox.

Follows ISSUES-STANDARD.md: four statuses, 14-day timers, internal notes, staged image uploads.
"""

from litestar_feedback.caller import Caller, CallerResolver, SessionOpener
from litestar_feedback.support.adapter import (
    AttachmentStore,
    Mailer,
    ReplyNotice,
    SupportAdapter,
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
    SupportBase,
)
from litestar_feedback.support.plugin import SupportPlugin
from litestar_feedback.support.service import (
    ALLOWED_TRANSITIONS,
    SupportService,
    referenced_replay_sessions,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "AttachmentStore",
    "Caller",
    "CallerResolver",
    "EventType",
    "Issue",
    "IssueAttachment",
    "IssueEvent",
    "IssueKind",
    "IssueStatus",
    "Mailer",
    "ReplyNotice",
    "SessionOpener",
    "Side",
    "SupportAdapter",
    "SupportBase",
    "SupportConfig",
    "SupportPlugin",
    "SupportService",
    "referenced_replay_sessions",
]
