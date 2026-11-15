"""What the host product provides to ``ReplayPlugin``, and the limits it can tune."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller, CallerResolver, SessionOpener


class ChunkStore(Protocol):
    """Async blob store for chunk bodies. Without one, chunks are stored in the database."""

    async def put(self, *, key: str, data: bytes) -> None:
        """Store ``data`` under ``key``."""
        ...

    async def get(self, *, key: str) -> bytes:
        """Return the bytes stored under ``key``."""
        ...

    async def delete(self, *, key: str) -> None:
        """Remove ``key``. A missing key is not an error."""
        ...


ConsentCheck = Callable[[Caller, Request[Any, Any, Any]], Awaitable[bool]]
"""Return True when this caller agreed to be recorded."""

ProtectedSessions = Callable[[AsyncSession], Awaitable[set[uuid.UUID]]]
"""Return replay session ids that prune and user deletion must keep.

Pass ``litestar_feedback.support.referenced_replay_sessions`` to keep sessions that live
support issues point to.
"""


@dataclass(frozen=True, slots=True)
class ReplayAdapter:
    """The product-specific parts of session replay.

    Attributes:
        caller: Maps a request to a ``Caller``. Operators can play back and see heatmaps.
        session: Opens a transaction for that caller. Set row-level security here.
        chunk_store: Blob store for chunk bodies. None stores them in ``replay_chunks.events``.
        retention_days: ``prune`` deletes sessions whose last event is older. 0 keeps all.
        consent: Called before every ingest. False answers 403 and stores nothing. None
            means the host checks consent before it starts the recorder.
        protected_sessions: Session ids that prune and user deletion keep.
    """

    caller: CallerResolver
    session: SessionOpener
    chunk_store: ChunkStore | None = None
    retention_days: int = 30
    consent: ConsentCheck | None = None
    protected_sessions: ProtectedSessions | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Ingest limits and heatmap buckets.

    Attributes:
        path: Prefix for every route, for example ``"/v1"``.
        max_chunk_bytes: Largest accepted chunk, as JSON bytes. Bigger is 413.
        max_session_bytes: Total JSON bytes one session may store. A chunk that would pass it
            is 413. 0 turns the cap off.
        max_chunk_events: Most events in one chunk. More is 413.
        viewport_buckets: Lower bounds of the viewport width buckets for heatmaps. A click at
            width 900 with buckets ``(0, 640, 1024)`` falls in bucket 640.
        heatmap_columns: Columns of the heatmap grid across the viewport width.
        heatmap_row_px: Height of one heatmap row in pixels.
        list_limit: Default and maximum page size of the session list.
        clock: Returns the current time. Tests replace it.
    """

    path: str = "/"
    max_chunk_bytes: int = 1024 * 1024
    max_session_bytes: int = 20 * 1024 * 1024
    max_chunk_events: int = 5_000
    viewport_buckets: tuple[int, ...] = (0, 640, 1024, 1440)
    heatmap_columns: int = 20
    heatmap_row_px: int = 50
    list_limit: int = 200
    clock: Callable[[], datetime] = field(default=_utcnow)

    def bucket(self, width: int) -> int:
        """Return the bucket lower bound for a viewport width."""
        fitting = [b for b in self.viewport_buckets if b <= width]
        return max(fitting) if fitting else min(self.viewport_buckets)
