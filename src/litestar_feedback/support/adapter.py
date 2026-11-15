"""What the host product provides to ``SupportPlugin``, and the limits it can tune."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from litestar_feedback.caller import CallerResolver, SessionOpener


class AttachmentStore(Protocol):
    """Async object store for screenshots. Wrap S3, R2, GCS or a local folder in this shape."""

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        """Store ``data`` under ``key``, replacing anything there."""
        ...

    async def delete(self, *, key: str) -> None:
        """Remove ``key``. A missing key is not an error."""
        ...

    async def presign_get(self, *, key: str) -> str:
        """Return a short-lived URL that downloads ``key``."""
        ...


@dataclass(frozen=True, slots=True)
class ReplyNotice:
    """An operator posted a public reply. The mailer tells the customer."""

    issue_id: uuid.UUID
    event_id: uuid.UUID
    scope_id: str
    user_id: str | None
    issue_title: str | None
    reply_body: str
    operator: str


class Mailer(Protocol):
    """Sends the customer a notice about an operator reply (email or in-app)."""

    async def send_reply(self, notice: ReplyNotice) -> bool:
        """Send the notice. Return False when the customer has no address (``no_email``).

        An exception is logged and recorded as ``send_failed``. It never fails the request.
        """
        ...


@dataclass(frozen=True, slots=True)
class SupportAdapter:
    """The product-specific parts of the support inbox.

    Attributes:
        caller: Maps a request to a ``Caller``: scope, user, actor label, operator flag.
        session: Opens a transaction for that caller. Set row-level security here.
        mailer: Notifies customers about operator replies. None records ``no_mailer`` on the
            event and sends nothing.
        storage: Object store for screenshots. None makes ``POST /issue-attachments`` return 503.
    """

    caller: CallerResolver
    session: SessionOpener
    mailer: Mailer | None = None
    storage: AttachmentStore | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SupportConfig:
    """Limits and timers. The defaults follow ISSUES-STANDARD.md.

    Attributes:
        path: Prefix for every route, for example ``"/v1"`` or ``"/api"``.
        waiting_to_resolved: A ``waiting_customer`` issue with no activity for this long
            becomes ``resolved``.
        resolved_to_closed: A ``resolved`` issue with no activity for this long becomes
            ``closed``.
        rate_limit_count: Customer messages (new issues plus comments) one actor may post per
            ``rate_limit_window``. Operators are not limited. 0 turns the limit off.
        rate_limit_window: The window ``rate_limit_count`` counts over.
        staged_ttl: Age after which an unclaimed upload is reaped.
        max_attachment_bytes: Largest accepted upload.
        allowed_mime_types: Accepted upload types.
        system_actor: ``actor`` written on events that timers produce.
        list_limit: Default and maximum page size of the list routes.
        clock: Returns the current time. Tests replace it to move time forward.
    """

    path: str = "/"
    waiting_to_resolved: timedelta = timedelta(days=14)
    resolved_to_closed: timedelta = timedelta(days=14)
    rate_limit_count: int = 20
    rate_limit_window: timedelta = timedelta(hours=1)
    staged_ttl: timedelta = timedelta(hours=24)
    max_attachment_bytes: int = 4 * 1024 * 1024
    allowed_mime_types: frozenset[str] = frozenset(
        {"image/png", "image/jpeg", "image/gif", "image/webp"}
    )
    system_actor: str = "system:timer"
    list_limit: int = 200
    clock: Callable[[], datetime] = field(default=_utcnow)
