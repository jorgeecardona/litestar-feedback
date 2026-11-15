"""Replay plugin: ingest, ownership, caps, consent, playback, heatmaps, prune, user deletion."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import msgspec
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar import Litestar
from litestar.testing import TestClient

from examples.replay_app import MemoryChunkStore, make_app
from litestar_feedback.caller import Caller
from litestar_feedback.replay import ReplayBase, ReplayConfig, ReplayPlugin
from litestar_feedback.replay import events as rr
from litestar_feedback.replay.migration import create_tables, drop_tables
from tests.conftest import CUSTOMER, OPERATOR, OTHER, FakeClock

Client = TestClient[Litestar]
T0 = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp() * 1000)
SYSTEM = Caller(scope_id="system", user_id=None, actor="system:prune", is_operator=True)


def meta(ts: int, href: str = "https://app.test/orders", width: int = 1200) -> dict[str, Any]:
    return {"type": 4, "timestamp": ts, "data": {"href": href, "width": width, "height": 800}}


def snapshot(ts: int) -> dict[str, Any]:
    return {"type": 2, "timestamp": ts, "data": {"node": {}, "initialOffset": {}}}


def click(ts: int, x: int, y: int) -> dict[str, Any]:
    return {"type": 3, "timestamp": ts, "data": {"source": 2, "type": 2, "id": 7, "x": x, "y": y}}


def move(ts: int) -> dict[str, Any]:
    return {"type": 3, "timestamp": ts, "data": {"source": 1, "positions": []}}


def resize(ts: int, width: int) -> dict[str, Any]:
    return {"type": 3, "timestamp": ts, "data": {"source": 4, "width": width, "height": 700}}


def _client(**kwargs: Any) -> Iterator[Client]:
    with TestClient(app=make_app(**kwargs)) as c:
        yield c


@pytest.fixture
def client() -> Iterator[Client]:
    yield from _client()


def ingest(
    client: Client,
    session_id: uuid.UUID,
    seq: int,
    events: list[dict[str, Any]],
    headers: dict[str, str] = CUSTOMER,
) -> Any:
    return client.post(
        "/replay/ingest",
        json={
            "session_id": str(session_id),
            "seq": seq,
            "events": events,
            "entry_url": "https://app.test/orders",
            "viewport_w": 1200,
            "viewport_h": 800,
        },
        headers={**headers, "User-Agent": "test-agent"},
    )


def play(client: Client, session_id: uuid.UUID, offset_ms: int | None = None) -> dict[str, Any]:
    query = "" if offset_ms is None else f"?offset_ms={offset_ms}"
    res = client.get(f"/admin/replay/sessions/{session_id}{query}", headers=OPERATOR)
    assert res.status_code == 200, res.text
    return res.json()


# -- ingest ------------------------------------------------------------------------------


def test_ingest_then_play(client: Client) -> None:
    sid = uuid.uuid4()
    first = [meta(T0), snapshot(T0 + 1), move(T0 + 2)]
    assert ingest(client, sid, 0, first).json() == {"action": "appended", "event_count": 3}
    assert ingest(client, sid, 1, [move(T0 + 3)]).json()["action"] == "appended"
    data = play(client, sid)
    assert [e["timestamp"] for e in data["events"]] == [T0, T0 + 1, T0 + 2, T0 + 3]
    assert data["seek_to_ms"] is None
    session = data["session"]
    assert session["chunk_count"] == 2
    assert session["user_agent"] == "test-agent"
    assert session["scope_id"] == "tenant-a"


def test_repeated_seq_is_ignored(client: Client) -> None:
    sid = uuid.uuid4()
    ingest(client, sid, 0, [meta(T0)])
    res = ingest(client, sid, 0, [meta(T0), move(T0 + 1)])
    assert res.json() == {"action": "exists", "event_count": 0}
    assert play(client, sid)["session"]["event_count"] == 1


def test_other_tenant_cannot_write_into_a_session(client: Client) -> None:
    sid = uuid.uuid4()
    ingest(client, sid, 0, [meta(T0)])
    res = ingest(client, sid, 1, [move(T0 + 1)], headers=OTHER)
    assert res.status_code == 409
    assert play(client, sid)["session"]["chunk_count"] == 1


@pytest.mark.parametrize(
    ("config", "events", "status"),
    [
        (ReplayConfig(max_chunk_bytes=100), [meta(T0)] * 3, 413),
        (ReplayConfig(max_chunk_events=2), [meta(T0)] * 3, 413),
        (ReplayConfig(), [], 422),
        (ReplayConfig(), [{"type": "x", "timestamp": T0}], 422),
    ],
)
def test_bad_chunks_are_refused(config: ReplayConfig, events: list[Any], status: int) -> None:
    for client in _client(config=config):
        assert ingest(client, uuid.uuid4(), 0, events).status_code == status


def test_session_cap_refuses_the_chunk_that_would_pass_it() -> None:
    size = len(rr_bytes([meta(T0)]))
    for client in _client(config=ReplayConfig(max_session_bytes=size * 2)):
        sid = uuid.uuid4()
        assert ingest(client, sid, 0, [meta(T0)]).status_code == 200
        assert ingest(client, sid, 1, [meta(T0)]).status_code == 200
        assert ingest(client, sid, 2, [meta(T0)]).status_code == 413
        assert play(client, sid)["session"]["byte_count"] == size * 2


def rr_bytes(events: list[dict[str, Any]]) -> bytes:
    return msgspec.json.encode(events)


def test_no_consent_stores_nothing(client: Client) -> None:
    sid = uuid.uuid4()
    res = client.post(
        "/replay/ingest",
        json={"session_id": str(sid), "seq": 0, "events": [meta(T0)]},
        headers={**CUSTOMER, "X-Replay-Consent": "no"},
    )
    assert res.status_code == 403
    assert client.get(f"/admin/replay/sessions/{sid}", headers=OPERATOR).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/admin/replay/sessions",
        f"/admin/replay/sessions/{uuid.uuid4()}",
        "/admin/replay/heatmaps",
        "/admin/replay/heatmap?path=/&viewport_bucket=0",
    ],
)
def test_customers_cannot_use_admin_routes(client: Client, path: str) -> None:
    assert client.get(path, headers=CUSTOMER).status_code == 403


def test_active_time_spans_chunks(client: Client) -> None:
    sid = uuid.uuid4()
    ingest(client, sid, 0, [meta(T0), move(T0 + 1_000), move(T0 + 3_000)])
    ingest(client, sid, 1, [move(T0 + 10_000), move(T0 + 200_000)])
    assert play(client, sid)["session"]["active_ms"] == 2_000 + 7_000


# -- playback from an offset -------------------------------------------------------------


def _two_snapshots(client: Client, sid: uuid.UUID) -> None:
    ingest(client, sid, 0, [meta(T0), snapshot(T0 + 1), move(T0 + 10_000)])
    ingest(client, sid, 1, [move(T0 + 20_000)])
    ingest(client, sid, 2, [meta(T0 + 30_000), snapshot(T0 + 30_001), move(T0 + 50_000)])


def test_offset_starts_at_the_last_snapshot_before_it(client: Client) -> None:
    sid = uuid.uuid4()
    _two_snapshots(client, sid)
    data = play(client, sid, offset_ms=40_000)
    assert [e["timestamp"] for e in data["events"]] == [T0 + 30_000, T0 + 30_001, T0 + 50_000]
    assert data["seek_to_ms"] == T0 + 40_000


def test_offset_before_second_snapshot_loads_from_the_first(client: Client) -> None:
    sid = uuid.uuid4()
    _two_snapshots(client, sid)
    data = play(client, sid, offset_ms=15_000)
    assert data["events"][0]["timestamp"] == T0
    assert len(data["events"]) == 7


def test_blob_store_holds_chunk_bodies() -> None:
    store = MemoryChunkStore()
    for client in _client(chunk_store=store):
        sid = uuid.uuid4()
        _two_snapshots(client, sid)
        assert len(store.blobs) == 3
        assert len(play(client, sid, offset_ms=40_000)["events"]) == 3
        assert client.delete(f"/admin/replay/sessions/{sid}", headers=OPERATOR).status_code == 204
        assert store.blobs == {}
        assert client.delete(f"/admin/replay/sessions/{sid}", headers=OPERATOR).status_code == 404


# -- heatmaps ----------------------------------------------------------------------------


def test_heatmap_groups_clicks_by_path_and_bucket(client: Client) -> None:
    sid = uuid.uuid4()
    ingest(client, sid, 0, [meta(T0, width=1200), click(T0 + 1, 0, 10), click(T0 + 2, 1199, 60)])
    # No meta in this chunk: the path and width carry over from the last one.
    ingest(client, sid, 1, [click(T0 + 3, 600, 10)])
    ingest(client, sid, 2, [resize(T0 + 4, 500), click(T0 + 5, 250, 10)])
    pages = client.get("/admin/replay/heatmaps", headers=OPERATOR).json()
    assert {(p["path"], p["viewport_bucket"], p["clicks"]) for p in pages} >= {
        ("/orders", 1024, 3),
        ("/orders", 0, 1),
    }
    res = client.get(
        "/admin/replay/heatmap",
        params={"path": "/orders", "viewport_bucket": 1024, "scope_id": "tenant-a"},
        headers=OPERATOR,
    )
    grid = res.json()
    assert grid["total_clicks"] == 3
    assert {(c["column"], c["row"], c["clicks"]) for c in grid["cells"]} == {
        (0, 0, 1),
        (10, 0, 1),
        (19, 1, 1),
    }


def test_clicks_before_the_first_meta_are_dropped() -> None:
    facts = rr.facts([click(T0, 5, 5), meta(T0 + 1), click(T0 + 2, 5, 5)], rr.PageState(None, None))
    assert len(facts.clicks) == 1


# -- prune and user deletion -------------------------------------------------------------


def _replay(client: Client) -> ReplayPlugin:
    app = cast(Litestar, client.app)  # pyright: ignore[reportUnknownMemberType]
    return app.plugins.get(ReplayPlugin)


def test_prune_keeps_sessions_live_issues_point_to(clock: FakeClock) -> None:
    clock.now = datetime(2026, 2, 15, tzinfo=UTC)
    for client in _client(config=ReplayConfig(clock=clock), with_support=True):
        old, kept, fresh = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        ingest(client, old, 0, [meta(T0)])
        ingest(client, kept, 0, [meta(T0)])
        ingest(client, fresh, 0, [meta(int(clock.now.timestamp() * 1000))])
        res = client.post(
            "/issues",
            json={"body": "it broke", "context": {"replay_session_id": str(kept)}},
            headers=CUSTOMER,
        )
        assert res.status_code == 201, res.text
        result = client.blocking_portal.call(_replay(client).prune, SYSTEM)
        assert (result.candidates, result.deleted, result.protected) == (2, 1, 1)
        assert client.get(f"/admin/replay/sessions/{old}", headers=OPERATOR).status_code == 404
        play(client, kept)
        play(client, fresh)


def test_prune_with_zero_retention_keeps_everything(clock: FakeClock) -> None:
    clock.now += timedelta(days=3650)
    for client in _client(config=ReplayConfig(clock=clock), retention_days=0):
        ingest(client, uuid.uuid4(), 0, [meta(T0)])
        assert client.blocking_portal.call(_replay(client).prune, SYSTEM).deleted == 0


def test_delete_user_keeps_protected_sessions_unlinked() -> None:
    for client in _client(with_support=True):
        gone, kept, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        ingest(client, gone, 0, [meta(T0)])
        ingest(client, kept, 0, [meta(T0)])
        ingest(client, other, 0, [meta(T0)], headers=OTHER)
        client.post(
            "/issues",
            json={"body": "x", "context": {"replay_session_id": str(kept)}},
            headers=CUSTOMER,
        )
        deleted = client.blocking_portal.call(_replay(client).delete_user, SYSTEM, "user-a")
        assert deleted == 1
        assert client.get(f"/admin/replay/sessions/{gone}", headers=OPERATOR).status_code == 404
        assert play(client, kept)["session"]["user_id"] is None
        assert play(client, other)["session"]["user_id"] == "user-b"


def test_list_sessions_filters_by_scope(client: Client) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    ingest(client, a, 0, [meta(T0)])
    ingest(client, b, 0, [meta(T0)], headers=OTHER)
    rows = client.get("/admin/replay/sessions?scope_id=tenant-b", headers=OPERATOR).json()
    assert [r["id"] for r in rows] == [str(b)]


# -- migration ---------------------------------------------------------------------------

TABLES = ("replay_sessions", "replay_chunks", "replay_clicks")


def _shape(conn: sa.Connection) -> dict[str, Any]:
    inspector = sa.inspect(conn)
    return {
        table: (
            {(c["name"], bool(c["nullable"])) for c in inspector.get_columns(table)},
            {i["name"] for i in inspector.get_indexes(table)},
        )
        for table in TABLES
    }


def test_migration_matches_models() -> None:
    migrated = sa.create_engine("sqlite://")
    with migrated.begin() as conn:
        create_tables(Operations(MigrationContext.configure(conn)))
        from_migration = _shape(conn)
    modelled = sa.create_engine("sqlite://")
    with modelled.begin() as conn:
        ReplayBase.metadata.create_all(conn)
        assert from_migration == _shape(conn)
    with migrated.begin() as conn:
        drop_tables(Operations(MigrationContext.configure(conn)))
        assert sa.inspect(conn).get_table_names() == []
