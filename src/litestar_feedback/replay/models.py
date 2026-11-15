"""The replay tables: ``replay_sessions``, ``replay_chunks``, ``replay_clicks``.

They hang off their own ``ReplayBase`` metadata. Create them with
``litestar_feedback.replay.migration.create_tables``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from litestar_feedback._sqltypes import JSONColumn, UTCDateTime

KEY_LENGTH = 64
"""Width of the ``scope_id`` and ``user_id`` columns. Fits a UUID or a big integer as text."""


class ReplayBase(DeclarativeBase):
    """Declarative base that owns the replay tables' metadata."""


class ReplaySession(ReplayBase):
    """One recording: one page load of the app, with an id the browser picks.

    ``byte_count`` is the JSON size of every stored chunk, so the session cap needs no sum.
    ``current_path`` and ``viewport_w`` carry the page state from one chunk to the next, for
    click extraction. ``active_ms`` adds up gaps of 60 s or less between user inputs.
    """

    __tablename__ = "replay_sessions"
    __table_args__ = (
        Index("ix_replay_sessions_scope_started", "scope_id", "started_at"),
        Index("ix_replay_sessions_last_event_at", "last_event_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(KEY_LENGTH))
    user_id: Mapped[str | None] = mapped_column(String(KEY_LENGTH), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_event_at: Mapped[datetime] = mapped_column(UTCDateTime)
    entry_url: Mapped[str | None] = mapped_column(String(500))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    current_path: Mapped[str | None] = mapped_column(String(500))
    viewport_w: Mapped[int | None] = mapped_column(Integer)
    viewport_h: Mapped[int | None] = mapped_column(Integer)
    event_count: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer)
    byte_count: Mapped[int] = mapped_column(BigInteger)
    active_ms: Mapped[int] = mapped_column(BigInteger)
    last_input_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ReplayChunk(ReplayBase):
    """One batch of rrweb events, numbered by the browser with ``seq``.

    Exactly one of ``events`` (database storage) and ``blob_key`` (``ChunkStore``) is set.
    ``first_ms``, ``last_ms`` and ``snapshot_ms`` are rrweb timestamps. Playback from an offset
    starts at the last chunk whose ``snapshot_ms`` is at or before the target.
    """

    __tablename__ = "replay_chunks"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_replay_chunks_session_seq"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("replay_sessions.id", ondelete="CASCADE"), index=True
    )
    scope_id: Mapped[str] = mapped_column(String(KEY_LENGTH), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    first_ms: Mapped[int] = mapped_column(BigInteger)
    last_ms: Mapped[int] = mapped_column(BigInteger)
    snapshot_ms: Mapped[int | None] = mapped_column(BigInteger)
    event_count: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    events: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONColumn)
    blob_key: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ReplayClick(ReplayBase):
    """One click, kept for heatmaps. Deleted with its session."""

    __tablename__ = "replay_clicks"
    __table_args__ = (Index("ix_replay_clicks_path_bucket", "path", "viewport_bucket"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("replay_sessions.id", ondelete="CASCADE"), index=True
    )
    scope_id: Mapped[str] = mapped_column(String(KEY_LENGTH), index=True)
    path: Mapped[str] = mapped_column(String(500))
    viewport_bucket: Mapped[int] = mapped_column(Integer)
    x_ratio: Mapped[float] = mapped_column(Float)
    y_px: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(UTCDateTime)
