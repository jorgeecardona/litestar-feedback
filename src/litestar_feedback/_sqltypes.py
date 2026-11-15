"""Column types both plugins share."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Dialect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """A timezone-aware timestamp that always loads as UTC.

    SQLite drops the offset on the way back. This type puts UTC back, so comparisons with
    ``datetime.now(UTC)`` work on every backend. Naive values are refused on write.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Refuse naive datetimes and convert aware ones to UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: pass a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """Attach UTC to values the driver returns without an offset."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


JSONColumn: Any = JSON().with_variant(JSONB(), "postgresql")
"""JSON on SQLite, JSONB on Postgres."""
