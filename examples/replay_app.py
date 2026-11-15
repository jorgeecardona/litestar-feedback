"""A small app wired with ``ReplayPlugin``, and optionally ``SupportPlugin`` next to it.

With both, the replay prune keeps every recording a live support issue points to, through
``referenced_replay_sessions``. Sign-in is the ``X-User`` header from ``examples.support_app``.

```bash
uv run litestar --app examples.replay_app:app run
```
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from litestar import Litestar, Request
from litestar.plugins import PluginProtocol
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from examples.support_app import caller_from_header
from litestar_feedback.caller import Caller
from litestar_feedback.replay import ReplayAdapter, ReplayBase, ReplayConfig, ReplayPlugin
from litestar_feedback.support import (
    SupportAdapter,
    SupportBase,
    SupportConfig,
    SupportPlugin,
    referenced_replay_sessions,
)


@dataclass
class MemoryChunkStore:
    """A ``ChunkStore`` that keeps chunk bodies in a dict."""

    blobs: dict[str, bytes] = field(default_factory=dict[str, bytes])

    async def put(self, *, key: str, data: bytes) -> None:
        """Keep ``data`` under ``key``."""
        self.blobs[key] = data

    async def get(self, *, key: str) -> bytes:
        """Return the bytes under ``key``."""
        return self.blobs[key]

    async def delete(self, *, key: str) -> None:
        """Forget ``key``."""
        self.blobs.pop(key, None)


async def consent_from_header(caller: Caller, request: Request[Any, Any, Any]) -> bool:
    """Treat ``X-Replay-Consent: no`` as a refusal. A product reads its consent record."""
    return request.headers.get("x-replay-consent", "yes") != "no"


def make_app(
    *,
    config: ReplayConfig | None = None,
    chunk_store: MemoryChunkStore | None = None,
    retention_days: int = 30,
    with_support: bool = False,
) -> Litestar:
    """Build the app on an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def open_session(caller: Caller) -> AsyncGenerator[AsyncSession]:
        async with sessions.begin() as session:
            yield session

    async def create_tables() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(ReplayBase.metadata.create_all)
            if with_support:
                await conn.run_sync(SupportBase.metadata.create_all)

    replay = ReplayPlugin(
        adapter=ReplayAdapter(
            caller=caller_from_header,
            session=open_session,
            chunk_store=chunk_store,
            retention_days=retention_days,
            consent=consent_from_header,
            protected_sessions=referenced_replay_sessions if with_support else None,
        ),
        config=config or ReplayConfig(),
    )
    plugins: list[PluginProtocol] = [replay]
    if with_support:
        support = SupportAdapter(caller=caller_from_header, session=open_session)
        plugins.append(SupportPlugin(adapter=support, config=SupportConfig(rate_limit_count=0)))
    return Litestar(plugins=plugins, on_startup=[create_tables])


app = make_app(with_support=True)
