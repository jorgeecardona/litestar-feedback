"""Alembic helpers that create and drop the replay tables.

The table definitions are written out here, not read from the models, so a migration you
wrote today creates the same tables after a package upgrade.

```python
from alembic import op
from litestar_feedback.replay.migration import create_tables, drop_tables


def upgrade() -> None:
    create_tables(op)


def downgrade() -> None:
    drop_tables(op)
```
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from litestar_feedback.replay.models import KEY_LENGTH


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _session_fk() -> sa.Column[Any]:
    return sa.Column(
        "session_id",
        sa.Uuid(),
        sa.ForeignKey("replay_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )


def create_tables(op: Any) -> None:
    """Create ``replay_sessions``, ``replay_chunks`` and ``replay_clicks`` with their indexes.

    Args:
        op: Alembic's ``op`` (``alembic.operations.Operations``).
    """
    key = sa.String(KEY_LENGTH)
    op.create_table(
        "replay_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_id", key, nullable=False),
        sa.Column("user_id", key, nullable=True),
        _ts("started_at"),
        _ts("last_event_at"),
        sa.Column("entry_url", sa.String(500), nullable=True),
        sa.Column("user_agent", sa.String(500), nullable=True),
        sa.Column("current_path", sa.String(500), nullable=True),
        sa.Column("viewport_w", sa.Integer(), nullable=True),
        sa.Column("viewport_h", sa.Integer(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("active_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_input_ms", sa.BigInteger(), nullable=True),
        _ts("created_at"),
    )
    op.create_index(
        "ix_replay_sessions_scope_started", "replay_sessions", ["scope_id", "started_at"]
    )
    op.create_index("ix_replay_sessions_last_event_at", "replay_sessions", ["last_event_at"])
    op.create_index("ix_replay_sessions_user_id", "replay_sessions", ["user_id"])

    op.create_table(
        "replay_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _session_fk(),
        sa.Column("scope_id", key, nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("first_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_ms", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_ms", sa.BigInteger(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("events", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=True),
        sa.Column("blob_key", sa.String(512), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("session_id", "seq", name="uq_replay_chunks_session_seq"),
    )
    op.create_index("ix_replay_chunks_session_id", "replay_chunks", ["session_id"])
    op.create_index("ix_replay_chunks_scope_id", "replay_chunks", ["scope_id"])

    op.create_table(
        "replay_clicks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _session_fk(),
        sa.Column("scope_id", key, nullable=False),
        sa.Column("path", sa.String(500), nullable=False),
        sa.Column("viewport_bucket", sa.Integer(), nullable=False),
        sa.Column("x_ratio", sa.Float(), nullable=False),
        sa.Column("y_px", sa.Integer(), nullable=False),
        _ts("at"),
    )
    op.create_index("ix_replay_clicks_session_id", "replay_clicks", ["session_id"])
    op.create_index("ix_replay_clicks_scope_id", "replay_clicks", ["scope_id"])
    op.create_index("ix_replay_clicks_path_bucket", "replay_clicks", ["path", "viewport_bucket"])


def drop_tables(op: Any) -> None:
    """Drop the three replay tables, children first."""
    op.drop_table("replay_clicks")
    op.drop_table("replay_chunks")
    op.drop_table("replay_sessions")
