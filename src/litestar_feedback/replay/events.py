"""Reading rrweb events: what one chunk says about time, activity, snapshots and clicks.

Pure functions. The numbers are rrweb's ``EventType`` and ``IncrementalSource`` enums.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

FULL_SNAPSHOT = 2
INCREMENTAL_SNAPSHOT = 3
META = 4

SOURCE_MOUSE_INTERACTION = 2
SOURCE_VIEWPORT_RESIZE = 4
MOUSE_CLICK = 2

USER_INPUT_SOURCES = frozenset({1, 2, 3, 5, 6})
"""Mouse move, mouse interaction, scroll, input, touch move: things a person causes."""

ACTIVE_GAP_MS = 60_000
"""Gaps between inputs up to this long count as active time (same rule as crm)."""

Event = Mapping[str, Any]


class InvalidEvent(ValueError):
    """An event without an integer ``type`` and ``timestamp``."""


@dataclass(frozen=True, slots=True)
class Click:
    """One click, with ``x`` as a fraction of the viewport width and ``y`` in viewport pixels."""

    path: str
    viewport_w: int
    x_ratio: float
    y_px: int
    at_ms: int


@dataclass(frozen=True, slots=True)
class PageState:
    """The page and viewport width in effect at a point of the recording."""

    path: str | None
    viewport_w: int | None


@dataclass(frozen=True, slots=True)
class ChunkFacts:
    """What the plugin stores about a chunk besides its events."""

    first_ms: int
    last_ms: int
    snapshot_ms: int | None
    input_ms: tuple[int, ...]
    clicks: tuple[Click, ...]
    end_state: PageState


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def validate(events: Sequence[Any]) -> None:
    """Raise ``InvalidEvent`` unless every event is an object with integer type and timestamp."""
    for i, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise InvalidEvent(f"event {i} is not an object")
        typed: Mapping[str, Any] = event  # pyright: ignore[reportUnknownVariableType]
        if _int(typed.get("type")) is None or _int(typed.get("timestamp")) is None:
            raise InvalidEvent(f"event {i} needs integer 'type' and 'timestamp'")


def path_of(href: str) -> str:
    """Return the path of a URL, ``/`` when empty."""
    return urlsplit(href).path or "/"


def facts(events: Sequence[Event], start: PageState) -> ChunkFacts:
    """Read one validated, non-empty chunk.

    ``start`` is the page state after the previous chunk. Meta events change the path and
    width, viewport resize events change the width. Clicks before the first known page and
    width are dropped.
    """
    path, width = start.path, start.viewport_w
    stamps: list[int] = []
    inputs: list[int] = []
    clicks: list[Click] = []
    snapshot: int | None = None
    for event in events:
        ts = int(event["timestamp"])
        kind = int(event["type"])
        stamps.append(ts)
        data: Mapping[str, Any] = event.get("data") or {}
        if kind == META:
            href = data.get("href")
            if isinstance(href, str):
                path = path_of(href)
            width = _int(data.get("width")) or width
        elif kind == FULL_SNAPSHOT:
            snapshot = ts if snapshot is None else max(snapshot, ts)
        elif kind == INCREMENTAL_SNAPSHOT:
            source = data.get("source")
            if source in USER_INPUT_SOURCES:
                inputs.append(ts)
            if source == SOURCE_VIEWPORT_RESIZE:
                width = _int(data.get("width")) or width
            elif source == SOURCE_MOUSE_INTERACTION and data.get("type") == MOUSE_CLICK:
                x, y = _int(data.get("x")), _int(data.get("y"))
                if path is not None and width and x is not None and y is not None:
                    ratio = min(1.0, max(0.0, x / width))
                    clicks.append(Click(path, width, ratio, max(0, y), ts))
    return ChunkFacts(
        first_ms=min(stamps),
        last_ms=max(stamps),
        snapshot_ms=snapshot,
        input_ms=tuple(sorted(inputs)),
        clicks=tuple(clicks),
        end_state=PageState(path=path, viewport_w=width),
    )


def active_ms(inputs: Sequence[int], *, previous_input_ms: int | None) -> int:
    """Return active time in ``inputs``, plus the gap from ``previous_input_ms`` when short."""
    total = sum(b - a for a, b in pairwise(inputs) if b - a <= ACTIVE_GAP_MS)
    if inputs and previous_input_ms is not None:
        gap = inputs[0] - previous_input_ms
        if 0 <= gap <= ACTIVE_GAP_MS:
            total += gap
    return total


def play_from(events: Sequence[Event], target_ms: int) -> list[Event]:
    """Return the events a player needs to start at ``target_ms``.

    rrweb rebuilds the page from a full snapshot and the meta event before it. Keep events from
    the last meta event before the last full snapshot at or before ``target_ms``. With no such
    snapshot, keep everything.
    """
    start = 0
    for i, event in enumerate(events):
        if int(event["timestamp"]) > target_ms:
            break
        if int(event["type"]) == FULL_SNAPSHOT:
            start = i
            for j in range(i - 1, -1, -1):
                if int(events[j]["type"]) == META:
                    start = j
                    break
    return list(events[start:])
