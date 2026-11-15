"""A small app wired with ``SupportPlugin``. The package's own tests run the contract against it.

Sign-in is a header, ``X-User: <email>``, looked up in ``USERS``. A real product resolves the
caller from its session cookie or token instead.

Run it:

```bash
uv run litestar --app examples.support_app:app run
```
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from litestar import Litestar, Request
from litestar.exceptions import NotAuthorizedException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from litestar_feedback.caller import Caller
from litestar_feedback.support import (
    ReplyNotice,
    SupportAdapter,
    SupportBase,
    SupportConfig,
    SupportPlugin,
)

USERS: dict[str, Caller] = {
    "customer@a.test": Caller(scope_id="tenant-a", user_id="user-a", actor="customer@a.test"),
    "operator@a.test": Caller(
        scope_id="tenant-a", user_id="user-op", actor="operator@a.test", is_operator=True
    ),
    "customer@b.test": Caller(scope_id="tenant-b", user_id="user-b", actor="customer@b.test"),
}


@dataclass
class MemoryStore:
    """An ``AttachmentStore`` that keeps objects in a dict."""

    objects: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        """Keep ``data`` under ``key``."""
        self.objects[key] = data

    async def delete(self, *, key: str) -> None:
        """Forget ``key``."""
        self.objects.pop(key, None)

    async def presign_get(self, *, key: str) -> str:
        """Return a fake download URL."""
        return f"memory://{key}"


@dataclass
class RecordingMailer:
    """A ``Mailer`` that records notices instead of sending them."""

    sent: list[ReplyNotice] = field(default_factory=list[ReplyNotice])

    async def send_reply(self, notice: ReplyNotice) -> bool:
        """Record the notice. Users without an id have no address."""
        if notice.user_id is None:
            return False
        self.sent.append(notice)
        return True


async def caller_from_header(request: Request[Any, Any, Any]) -> Caller:
    """Return the ``Caller`` named by the ``X-User`` header."""
    caller = USERS.get(request.headers.get("x-user", ""))
    if caller is None:
        raise NotAuthorizedException("sign in first")
    return caller


def make_app(
    *,
    config: SupportConfig | None = None,
    storage: MemoryStore | None = None,
    mailer: RecordingMailer | None = None,
) -> Litestar:
    """Build the app on an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_session(caller: Caller) -> AsyncGenerator[AsyncSession]:
        # A Postgres product would SET LOCAL its tenant here, from caller.scope_id.
        async with sessions.begin() as session:
            yield session

    async def create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(SupportBase.metadata.create_all)

    adapter = SupportAdapter(
        caller=caller_from_header, session=open_session, mailer=mailer, storage=storage
    )
    return Litestar(
        plugins=[SupportPlugin(adapter=adapter, config=config or SupportConfig())],
        on_startup=[create_tables],
    )


app = make_app(storage=MemoryStore(), mailer=RecordingMailer())
