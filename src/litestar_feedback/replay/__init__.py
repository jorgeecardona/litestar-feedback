"""Session replay for Litestar: chunked rrweb ingest, playback from an offset, click heatmaps.

The browser recorder is not part of this package. See the README for the ingest API.
"""

from litestar_feedback.caller import Caller, CallerResolver, SessionOpener
from litestar_feedback.replay.adapter import (
    ChunkStore,
    ConsentCheck,
    ProtectedSessions,
    ReplayAdapter,
    ReplayConfig,
)
from litestar_feedback.replay.models import ReplayBase, ReplayChunk, ReplayClick, ReplaySession
from litestar_feedback.replay.plugin import ReplayPlugin
from litestar_feedback.replay.service import (
    Heatmap,
    HeatmapCell,
    HeatmapPage,
    Playback,
    PruneResult,
    ReplayService,
)

__all__ = [
    "Caller",
    "CallerResolver",
    "ChunkStore",
    "ConsentCheck",
    "Heatmap",
    "HeatmapCell",
    "HeatmapPage",
    "Playback",
    "ProtectedSessions",
    "PruneResult",
    "ReplayAdapter",
    "ReplayBase",
    "ReplayChunk",
    "ReplayClick",
    "ReplayConfig",
    "ReplayPlugin",
    "ReplayService",
    "ReplaySession",
    "SessionOpener",
]
