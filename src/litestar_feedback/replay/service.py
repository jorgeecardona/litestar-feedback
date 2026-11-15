"""Replay rules: ingest with caps, playback from an offset, heatmaps, prune, user deletion.

``ReplayService`` works on one ``AsyncSession`` and never commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import msgspec
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller
from litestar_feedback.replay import events as rr
from litestar_feedback.replay.adapter import ChunkStore, ProtectedSessions, ReplayConfig
from litestar_feedback.replay.models import ReplayChunk, ReplayClick, ReplaySession


class ReplayError(Exception):
    """Base class of the errors the routes turn into HTTP responses."""


class SessionNotFound(ReplayError):
    """No session with that id for this caller (HTTP 404)."""


class SessionOwnedElsewhere(ReplayError):
    """The session id belongs to another scope or user (HTTP 409)."""


class TooLarge(ReplayError):
    """A chunk or session is over its cap (HTTP 413)."""


class BadChunk(ReplayError):
    """The events are not valid rrweb events (HTTP 422)."""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One ingest request."""

    session_id: uuid.UUID
    seq: int
    events: Sequence[dict[str, Any]]
    entry_url: str | None = None
    viewport_w: int | None = None
    viewport_h: int | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """``appended`` stored the chunk. ``exists`` means this ``seq`` was already stored."""

    action: Literal["appended", "exists"]
    event_count: int


@dataclass(frozen=True, slots=True)
class Playback:
    """Events to load into an rrweb player, and where to seek.

    ``seek_to_ms`` is an rrweb timestamp, or None to play from the start.
    """

    session: ReplaySession
    events: list[dict[str, Any]]
    seek_to_ms: int | None


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    """Clicks in one grid cell. ``column`` counts across the width, ``row`` down in pixels."""

    column: int
    row: int
    clicks: int


@dataclass(frozen=True, slots=True)
class Heatmap:
    """Clicks on one page path and viewport bucket, on a grid normalised to the width.

    A cell spans ``1 / columns`` of the viewport width and ``row_px`` pixels of height.
    """

    path: str
    viewport_bucket: int
    columns: int
    row_px: int
    total_clicks: int
    cells: list[HeatmapCell]


@dataclass(frozen=True, slots=True)
class HeatmapPage:
    """A page path and bucket that has clicks."""

    path: str
    viewport_bucket: int
    clicks: int


@dataclass(frozen=True, slots=True)
class PruneResult:
    """What ``prune`` found and did."""

    candidates: int
    deleted: int
    protected: int


_DELETE_BATCH = 500


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


@dataclass(frozen=True, slots=True)
class ReplayService:
    """Reads and writes the replay tables inside one session."""

    session: AsyncSession
    config: ReplayConfig
    chunk_store: ChunkStore | None = None

    async def ingest(self, caller: Caller, chunk: Chunk) -> IngestResult:
        """Store one chunk.

        The first chunk of a session id creates the session for the caller. A later chunk
        from another scope or user is refused, so nobody can write into someone else's
        recording. A repeated ``seq`` stores nothing and returns ``exists``.
        """
        if not chunk.events:
            raise BadChunk("events is empty")
        if len(chunk.events) > self.config.max_chunk_events:
            raise TooLarge(f"a chunk may hold at most {self.config.max_chunk_events} events")
        try:
            rr.validate(chunk.events)
        except rr.InvalidEvent as exc:
            raise BadChunk(str(exc)) from exc
        body = msgspec.json.encode(chunk.events)
        if len(body) > self.config.max_chunk_bytes:
            raise TooLarge(f"a chunk may be at most {self.config.max_chunk_bytes} bytes")

        row = await self._session_for(caller, chunk)
        exists = await self.session.scalar(
            select(ReplayChunk.id).where(
                ReplayChunk.session_id == row.id, ReplayChunk.seq == chunk.seq
            )
        )
        if exists is not None:
            return IngestResult(action="exists", event_count=0)
        cap = self.config.max_session_bytes
        if cap > 0 and row.byte_count + len(body) > cap:
            raise TooLarge("this recording reached its size cap")

        facts = rr.facts(chunk.events, rr.PageState(row.current_path, row.viewport_w))
        blob_key: str | None = None
        if self.chunk_store is not None:
            blob_key = f"replay/{row.scope_id}/{row.id}/{chunk.seq}.json"
            await self.chunk_store.put(key=blob_key, data=body)
        now = self.config.clock()
        self.session.add(
            ReplayChunk(
                session_id=row.id,
                scope_id=row.scope_id,
                seq=chunk.seq,
                first_ms=facts.first_ms,
                last_ms=facts.last_ms,
                snapshot_ms=facts.snapshot_ms,
                event_count=len(chunk.events),
                byte_size=len(body),
                events=None if blob_key else list(chunk.events),
                blob_key=blob_key,
                created_at=now,
            )
        )
        for click in facts.clicks:
            self.session.add(
                ReplayClick(
                    session_id=row.id,
                    scope_id=row.scope_id,
                    path=click.path[:500],
                    viewport_bucket=self.config.bucket(click.viewport_w),
                    x_ratio=click.x_ratio,
                    y_px=click.y_px,
                    at=_ms_to_dt(click.at_ms),
                )
            )
        row.active_ms += rr.active_ms(facts.input_ms, previous_input_ms=row.last_input_ms)
        if facts.input_ms:
            row.last_input_ms = max(row.last_input_ms or 0, facts.input_ms[-1])
        row.event_count += len(chunk.events)
        row.chunk_count += 1
        row.byte_count += len(body)
        row.last_event_at = max(row.last_event_at, _ms_to_dt(facts.last_ms))
        row.started_at = min(row.started_at, _ms_to_dt(facts.first_ms))
        row.current_path = facts.end_state.path[:500] if facts.end_state.path else None
        row.viewport_w = facts.end_state.viewport_w
        await self.session.flush()
        return IngestResult(action="appended", event_count=len(chunk.events))

    async def _session_for(self, caller: Caller, chunk: Chunk) -> ReplaySession:
        row = await self.session.get(ReplaySession, chunk.session_id, with_for_update=True)
        if row is not None:
            if row.scope_id != caller.scope_id or row.user_id != caller.user_id:
                raise SessionOwnedElsewhere("this session id belongs to another user")
            return row
        now = self.config.clock()
        first = min(int(e["timestamp"]) for e in chunk.events)
        row = ReplaySession(
            id=chunk.session_id,
            scope_id=caller.scope_id,
            user_id=caller.user_id,
            started_at=_ms_to_dt(first),
            last_event_at=_ms_to_dt(first),
            entry_url=chunk.entry_url[:500] if chunk.entry_url else None,
            user_agent=chunk.user_agent[:500] if chunk.user_agent else None,
            current_path=rr.path_of(chunk.entry_url)[:500] if chunk.entry_url else None,
            viewport_w=chunk.viewport_w,
            viewport_h=chunk.viewport_h,
            event_count=0,
            chunk_count=0,
            byte_count=0,
            active_ms=0,
            last_input_ms=None,
            created_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    # -- reads ---------------------------------------------------------------------------

    async def list_sessions(
        self,
        *,
        scope_id: str | None = None,
        user_id: str | None = None,
        limit: int,
        offset: int = 0,
    ) -> list[ReplaySession]:
        """List sessions, newest first, optionally for one scope or user."""
        query = select(ReplaySession)
        if scope_id is not None:
            query = query.where(ReplaySession.scope_id == scope_id)
        if user_id is not None:
            query = query.where(ReplaySession.user_id == user_id)
        query = query.order_by(ReplaySession.started_at.desc()).limit(limit).offset(offset)
        return list((await self.session.scalars(query)).all())

    async def playback(self, session_id: uuid.UUID, *, offset_ms: int | None = None) -> Playback:
        """Return the events to play, starting near ``offset_ms`` after the session start.

        With an offset, loading starts at the last chunk with a full snapshot at or before
        the target, so the player rebuilds the page and seeks to the target.
        """
        row = await self.session.get(ReplaySession, session_id)
        if row is None:
            raise SessionNotFound(f"replay session {session_id} not found")
        query = select(ReplayChunk).where(ReplayChunk.session_id == row.id)
        target: int | None = None
        if offset_ms is not None:
            first = await self.session.scalar(
                select(func.min(ReplayChunk.first_ms)).where(ReplayChunk.session_id == row.id)
            )
            if first is not None:
                target = first + max(0, offset_ms)
                start_seq = await self.session.scalar(
                    select(func.max(ReplayChunk.seq)).where(
                        ReplayChunk.session_id == row.id,
                        ReplayChunk.snapshot_ms.is_not(None),
                        ReplayChunk.snapshot_ms <= target,
                    )
                )
                if start_seq is not None:
                    query = query.where(ReplayChunk.seq >= start_seq)
        chunks = (await self.session.scalars(query.order_by(ReplayChunk.seq))).all()
        events: list[dict[str, Any]] = []
        for chunk in chunks:
            events.extend(await self._events_of(chunk))
        if target is not None:
            events = [dict(e) for e in rr.play_from(events, target)]
        return Playback(session=row, events=events, seek_to_ms=target)

    async def _events_of(self, chunk: ReplayChunk) -> list[dict[str, Any]]:
        if chunk.events is not None:
            return chunk.events
        if chunk.blob_key is None or self.chunk_store is None:
            return []
        raw = await self.chunk_store.get(key=chunk.blob_key)
        return msgspec.json.decode(raw, type=list[dict[str, Any]])

    async def heatmap_pages(self, *, scope_id: str | None = None) -> list[HeatmapPage]:
        """List page paths and buckets that have clicks, most clicked first."""
        count = func.count(ReplayClick.id)
        query = select(ReplayClick.path, ReplayClick.viewport_bucket, count).group_by(
            ReplayClick.path, ReplayClick.viewport_bucket
        )
        if scope_id is not None:
            query = query.where(ReplayClick.scope_id == scope_id)
        rows = await self.session.execute(query.order_by(count.desc()).limit(500))
        return [HeatmapPage(path=p, viewport_bucket=b, clicks=n) for p, b, n in rows.all()]

    async def heatmap(
        self, *, path: str, viewport_bucket: int, scope_id: str | None = None
    ) -> Heatmap:
        """Aggregate clicks on one path and bucket into the grid."""
        columns, row_px = self.config.heatmap_columns, self.config.heatmap_row_px
        query = select(ReplayClick.x_ratio, ReplayClick.y_px).where(
            ReplayClick.path == path, ReplayClick.viewport_bucket == viewport_bucket
        )
        if scope_id is not None:
            query = query.where(ReplayClick.scope_id == scope_id)
        counts: dict[tuple[int, int], int] = {}
        total = 0
        for x_ratio, y_px in (await self.session.execute(query)).all():
            cell = (min(columns - 1, int(x_ratio * columns)), y_px // row_px)
            counts[cell] = counts.get(cell, 0) + 1
            total += 1
        cells = [HeatmapCell(column=c, row=r, clicks=n) for (c, r), n in sorted(counts.items())]
        return Heatmap(
            path=path,
            viewport_bucket=viewport_bucket,
            columns=columns,
            row_px=row_px,
            total_clicks=total,
            cells=cells,
        )

    # -- deletion ------------------------------------------------------------------------

    async def delete_session(self, session_id: uuid.UUID) -> bool:
        """Delete one session with its chunks, blobs and clicks. Return False when missing."""
        found = await self.session.scalar(
            select(ReplaySession.id).where(ReplaySession.id == session_id)
        )
        if found is None:
            return False
        await self._delete([session_id])
        return True

    async def prune(
        self, *, retention_days: int, protected: ProtectedSessions | None = None
    ) -> PruneResult:
        """Delete sessions whose last event is older than ``retention_days``.

        Sessions in ``protected`` stay. ``retention_days <= 0`` deletes nothing.
        """
        if retention_days <= 0:
            return PruneResult(candidates=0, deleted=0, protected=0)
        cutoff = self.config.clock() - timedelta(days=retention_days)
        old = set(
            (
                await self.session.scalars(
                    select(ReplaySession.id).where(ReplaySession.last_event_at < cutoff)
                )
            ).all()
        )
        keep = (await protected(self.session)) & old if protected and old else set[uuid.UUID]()
        doomed = sorted(old - keep)
        await self._delete(doomed)
        return PruneResult(candidates=len(old), deleted=len(doomed), protected=len(keep))

    async def delete_user(self, user_id: str, *, protected: ProtectedSessions | None = None) -> int:
        """Delete a user's sessions. Protected ones stay with ``user_id`` set to NULL.

        Return how many were deleted. Call it when the product deletes the user.
        """
        ids = set(
            (
                await self.session.scalars(
                    select(ReplaySession.id).where(ReplaySession.user_id == user_id)
                )
            ).all()
        )
        keep = (await protected(self.session)) & ids if protected and ids else set[uuid.UUID]()
        doomed = sorted(ids - keep)
        await self._delete(doomed)
        if keep:
            await self.session.execute(
                update(ReplaySession).where(ReplaySession.id.in_(keep)).values(user_id=None)
            )
        return len(doomed)

    async def _delete(self, session_ids: Sequence[uuid.UUID]) -> None:
        for start in range(0, len(session_ids), _DELETE_BATCH):
            await self._delete_batch(session_ids[start : start + _DELETE_BATCH])

    async def _delete_batch(self, session_ids: Sequence[uuid.UUID]) -> None:
        if self.chunk_store is not None:
            keys = await self.session.scalars(
                select(ReplayChunk.blob_key).where(
                    ReplayChunk.session_id.in_(session_ids), ReplayChunk.blob_key.is_not(None)
                )
            )
            for key in keys:
                if key is not None:
                    await self.chunk_store.delete(key=key)
        # Explicit deletes: SQLite ignores ON DELETE CASCADE unless foreign keys are turned on.
        for model in (ReplayClick, ReplayChunk):
            await self.session.execute(delete(model).where(model.session_id.in_(session_ids)))
        await self.session.execute(delete(ReplaySession).where(ReplaySession.id.in_(session_ids)))
