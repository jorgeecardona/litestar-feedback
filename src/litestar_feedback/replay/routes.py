"""The replay route handlers. ``ReplayPlugin`` mounts them under ``ReplayConfig.path``."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import msgspec
from litestar import Request, Response, delete, get, post
from litestar.di import NamedDependency
from litestar.exceptions import NotFoundException, PermissionDeniedException
from litestar.params import FromPath, FromQuery, JSONBody, QueryParameter
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_204_NO_CONTENT,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_ENTITY,
)
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller
from litestar_feedback.replay.adapter import ReplayAdapter, ReplayConfig
from litestar_feedback.replay.models import ReplaySession
from litestar_feedback.replay.service import (
    BadChunk,
    Chunk,
    Heatmap,
    HeatmapPage,
    IngestResult,
    ReplayError,
    ReplayService,
    SessionNotFound,
    SessionOwnedElsewhere,
    TooLarge,
)

audit = logging.getLogger("litestar_feedback.replay.audit")

DEFAULT_PAGE = 50

AnyRequest = Request[Any, Any, Any]
SessionId = FromPath[uuid.UUID]


class IngestRequest(msgspec.Struct, frozen=True, kw_only=True):
    """Body of ``POST /replay/ingest``: one chunk of rrweb events.

    ``seq`` numbers the chunks of a session from 0 or 1; a repeated ``seq`` is ignored.
    ``entry_url`` and the viewport are read from the first chunk only.
    """

    session_id: uuid.UUID
    seq: Annotated[int, msgspec.Meta(ge=0)]
    events: list[dict[str, Any]]
    entry_url: Annotated[str, msgspec.Meta(max_length=2000)] | None = None
    viewport_w: Annotated[int, msgspec.Meta(ge=0)] | None = None
    viewport_h: Annotated[int, msgspec.Meta(ge=0)] | None = None


class SessionOut(msgspec.Struct, frozen=True, kw_only=True):
    """A recording, without its events."""

    id: uuid.UUID
    scope_id: str
    user_id: str | None
    started_at: datetime
    last_event_at: datetime
    entry_url: str | None
    user_agent: str | None
    viewport_w: int | None
    viewport_h: int | None
    event_count: int
    chunk_count: int
    byte_count: int
    active_ms: int


class PlaybackOut(msgspec.Struct, frozen=True, kw_only=True):
    """Events for an rrweb player. Seek to ``seek_to_ms`` (an rrweb timestamp) when set."""

    session: SessionOut
    events: list[dict[str, Any]]
    seek_to_ms: int | None


def _session_out(row: ReplaySession) -> SessionOut:
    return SessionOut(
        id=row.id,
        scope_id=row.scope_id,
        user_id=row.user_id,
        started_at=row.started_at,
        last_event_at=row.last_event_at,
        entry_url=row.entry_url,
        user_agent=row.user_agent,
        viewport_w=row.viewport_w,
        viewport_h=row.viewport_h,
        event_count=row.event_count,
        chunk_count=row.chunk_count,
        byte_count=row.byte_count,
        active_ms=row.active_ms,
    )


@dataclass(frozen=True, slots=True)
class ReplayRuntime:
    """The adapter and config, handed to every route as the ``feedback_replay`` dependency."""

    adapter: ReplayAdapter
    config: ReplayConfig

    def service(self, session: AsyncSession) -> ReplayService:
        """Return a ``ReplayService`` bound to ``session``."""
        return ReplayService(
            session=session, config=self.config, chunk_store=self.adapter.chunk_store
        )

    async def operator(self, request: AnyRequest) -> Caller:
        """Return the caller, or raise 403 when they are not an operator."""
        caller = await self.adapter.caller(request)
        if not caller.is_operator:
            raise PermissionDeniedException("operator access required")
        return caller


Runtime = NamedDependency[ReplayRuntime]


@post("/replay/ingest", status_code=HTTP_200_OK)
async def ingest(
    request: AnyRequest, data: JSONBody[IngestRequest], feedback_replay: Runtime
) -> IngestResult:
    """Store one chunk of the caller's recording. 403 without consent, 413 over a cap."""
    rt = feedback_replay
    caller = await rt.adapter.caller(request)
    if rt.adapter.consent is not None and not await rt.adapter.consent(caller, request):
        raise PermissionDeniedException("recording consent not given")
    async with rt.adapter.session(caller) as session:
        return await rt.service(session).ingest(
            caller,
            Chunk(
                session_id=data.session_id,
                seq=data.seq,
                events=data.events,
                entry_url=data.entry_url,
                viewport_w=data.viewport_w,
                viewport_h=data.viewport_h,
                user_agent=request.headers.get("user-agent"),
            ),
        )


@get("/admin/replay/sessions")
async def list_sessions(
    request: AnyRequest,
    feedback_replay: Runtime,
    scope_id: FromQuery[str | None] = None,
    user_id: FromQuery[str | None] = None,
    limit: Annotated[int | None, QueryParameter(ge=1)] = None,
    offset: Annotated[int, QueryParameter(ge=0)] = 0,
) -> list[SessionOut]:
    """List recordings, newest first."""
    rt = feedback_replay
    caller = await rt.operator(request)
    audit.info("%s listed replay sessions", caller.actor)
    page = max(1, min(limit or DEFAULT_PAGE, rt.config.list_limit))
    async with rt.adapter.session(caller) as session:
        rows = await rt.service(session).list_sessions(
            scope_id=scope_id, user_id=user_id, limit=page, offset=offset
        )
        return [_session_out(r) for r in rows]


@get("/admin/replay/sessions/{session_id:uuid}")
async def play(
    request: AnyRequest,
    session_id: SessionId,
    feedback_replay: Runtime,
    offset_ms: Annotated[int | None, QueryParameter(ge=0)] = None,
) -> PlaybackOut:
    """Return a recording's events. ``offset_ms`` (from an issue's ``replay_offset_ms``) seeks."""
    rt = feedback_replay
    caller = await rt.operator(request)
    audit.info("%s viewed replay session %s", caller.actor, session_id)
    async with rt.adapter.session(caller) as session:
        result = await rt.service(session).playback(session_id, offset_ms=offset_ms)
        return PlaybackOut(
            session=_session_out(result.session),
            events=result.events,
            seek_to_ms=result.seek_to_ms,
        )


@delete("/admin/replay/sessions/{session_id:uuid}", status_code=HTTP_204_NO_CONTENT)
async def delete_session(
    request: AnyRequest, session_id: SessionId, feedback_replay: Runtime
) -> None:
    """Delete one recording with its chunks and clicks."""
    rt = feedback_replay
    caller = await rt.operator(request)
    audit.info("%s deleted replay session %s", caller.actor, session_id)
    async with rt.adapter.session(caller) as session:
        if not await rt.service(session).delete_session(session_id):
            raise NotFoundException(f"replay session {session_id} not found")


@get("/admin/replay/heatmaps")
async def heatmap_pages(
    request: AnyRequest, feedback_replay: Runtime, scope_id: FromQuery[str | None] = None
) -> list[HeatmapPage]:
    """List page paths and viewport buckets that have clicks."""
    rt = feedback_replay
    caller = await rt.operator(request)
    async with rt.adapter.session(caller) as session:
        return await rt.service(session).heatmap_pages(scope_id=scope_id)


@get("/admin/replay/heatmap")
async def heatmap(
    request: AnyRequest,
    feedback_replay: Runtime,
    path: FromQuery[str],
    viewport_bucket: FromQuery[int],
    scope_id: FromQuery[str | None] = None,
) -> Heatmap:
    """Return the click grid of one page path and viewport bucket."""
    rt = feedback_replay
    caller = await rt.operator(request)
    async with rt.adapter.session(caller) as session:
        return await rt.service(session).heatmap(
            path=path, viewport_bucket=viewport_bucket, scope_id=scope_id
        )


_STATUS_FOR: dict[type[ReplayError], int] = {
    SessionNotFound: HTTP_404_NOT_FOUND,
    SessionOwnedElsewhere: HTTP_409_CONFLICT,
    TooLarge: HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    BadChunk: HTTP_422_UNPROCESSABLE_ENTITY,
}


def replay_error_handler(_: AnyRequest, exc: Exception) -> Response[dict[str, Any]]:
    """Turn a ``ReplayError`` into Litestar's usual ``{status_code, detail}`` error body."""
    code = next(
        (status for kind, status in _STATUS_FOR.items() if isinstance(exc, kind)),
        HTTP_409_CONFLICT,
    )
    return Response({"status_code": code, "detail": str(exc)}, status_code=code)


ROUTE_HANDLERS = [ingest, list_sessions, play, delete_session, heatmap_pages, heatmap]
"""Every replay route."""
