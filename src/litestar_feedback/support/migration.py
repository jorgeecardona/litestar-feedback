"""Alembic helpers that create and drop the support tables.

Call them from your own migration. The table definitions are written out here, not read from
the models, so a migration you wrote today creates the same tables after a package upgrade.

```python
from alembic import op
from litestar_feedback.support.migration import create_tables, drop_tables


def upgrade() -> None:
    create_tables(op, scope_fk="tenants.id")


def downgrade() -> None:
    drop_tables(op)
```
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from litestar_feedback.support.models import KEY_LENGTH


def _ts(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def create_tables(op: Any, *, scope_fk: str | None = None, user_fk: str | None = None) -> None:
    """Create ``issues``, ``issue_events`` and ``issue_attachments`` with their indexes.

    Args:
        op: Alembic's ``op`` (``alembic.operations.Operations``).
        scope_fk: ``"table.column"`` that ``scope_id`` references, ON DELETE CASCADE. The
            referenced column must be text-compatible with ``scope_id`` (a ``varchar``); leave
            it None when the key is a UUID or integer column.
        user_fk: Same for ``user_id``, ON DELETE SET NULL.
    """
    scope_ref = [sa.ForeignKey(scope_fk, ondelete="CASCADE")] if scope_fk else []
    user_ref = [sa.ForeignKey(user_fk, ondelete="SET NULL")] if user_fk else []
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    live = sa.text("status <> 'closed' AND dedupe_key IS NOT NULL")

    op.create_table(
        "issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_id", sa.String(KEY_LENGTH), *scope_ref, nullable=False),
        sa.Column("user_id", sa.String(KEY_LENGTH), *user_ref, nullable=True),
        sa.Column("kind", sa.String(24), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("context", json_type, nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("register_ref", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_issues_scope_id", "issues", ["scope_id"])
    op.create_index("ix_issues_user_id", "issues", ["user_id"])
    op.create_index("ix_issues_status_updated_at", "issues", ["status", "updated_at"])
    op.create_index(
        "uq_issues_scope_dedupe_live",
        "issues",
        ["scope_id", "dedupe_key"],
        unique=True,
        postgresql_where=live,
        sqlite_where=live,
    )

    op.create_table(
        "issue_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "issue_id", sa.Uuid(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("author_side", sa.String(10), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("internal", sa.Boolean(), nullable=False),
        _ts("notified_at", nullable=True),
        sa.Column("notify_skipped_reason", sa.Text(), nullable=True),
        _ts("created_at"),
        sa.UniqueConstraint("issue_id", "seq", name="uq_issue_events_issue_seq"),
    )
    op.create_index("ix_issue_events_issue_id", "issue_events", ["issue_id"])

    op.create_table(
        "issue_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scope_id", sa.String(KEY_LENGTH), *scope_ref, nullable=False),
        sa.Column("user_id", sa.String(KEY_LENGTH), *user_ref, nullable=True),
        sa.Column(
            "issue_id", sa.Uuid(), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "event_id",
            sa.Uuid(),
            sa.ForeignKey("issue_events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(60), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_issue_attachments_scope_id", "issue_attachments", ["scope_id"])
    op.create_index("ix_issue_attachments_user_id", "issue_attachments", ["user_id"])
    op.create_index("ix_issue_attachments_issue_id", "issue_attachments", ["issue_id"])
    op.create_index("ix_issue_attachments_event_id", "issue_attachments", ["event_id"])


def drop_tables(op: Any) -> None:
    """Drop the three support tables, children first."""
    op.drop_table("issue_attachments")
    op.drop_table("issue_events")
    op.drop_table("issues")
