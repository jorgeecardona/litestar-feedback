"""Support rules the black-box contract cannot reach: timers, rate limit, mail, storage, cleanup."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from litestar import Litestar
from litestar.testing import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from examples.support_app import MemoryStore, RecordingMailer, caller_from_header, make_app
from litestar_feedback.caller import Caller
from litestar_feedback.support import (
    IssueAttachment,
    IssueEvent,
    IssueStatus,
    ReplyNotice,
    Side,
    SupportAdapter,
    SupportBase,
    SupportConfig,
    SupportPlugin,
    SupportService,
    referenced_replay_sessions,
)
from litestar_feedback.support.migration import create_tables, drop_tables
from litestar_feedback.support.service import NewIssue
from tests.conftest import CUSTOMER, OPERATOR, FakeClock

pytest_plugins = ["pytester"]

Client = TestClient[Any]


def _client(**kwargs: Any) -> Iterator[Client]:
    with TestClient(app=make_app(**kwargs)) as c:
        yield c


def _open(client: Client, body: str = "broken") -> dict[str, Any]:
    res = client.post("/issues", json={"body": body}, headers=CUSTOMER)
    assert res.status_code == 201, res.text
    return res.json()


def _reply(client: Client, issue_id: str, *, internal: bool = False) -> dict[str, Any]:
    res = client.post(
        f"/admin/issues/{issue_id}/comments",
        json={"body": "on it", "internal": internal},
        headers=OPERATOR,
    )
    assert res.status_code == 201, res.text
    return res.json()


def _admin_event(client: Client, issue_id: str, event_id: str) -> dict[str, Any]:
    events = client.get(f"/admin/issues/{issue_id}", headers=OPERATOR).json()["events"]
    return next(e for e in events if e["id"] == event_id)


# -- timers ------------------------------------------------------------------------------


@pytest.fixture
def timed(clock: FakeClock) -> Iterator[Client]:
    yield from _client(config=SupportConfig(clock=clock))


def test_waiting_customer_resolves_after_14_days(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    _reply(timed, issue["id"])
    clock.advance(timedelta(days=14) - timedelta(seconds=1))
    assert timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()["status"] == (
        "waiting_customer"
    )
    clock.advance(timedelta(seconds=1))
    data = timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()
    assert data["status"] == "resolved"
    assert data["events"][-1]["actor"] == "system:timer"


def test_resolved_closes_after_another_14_days(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    _reply(timed, issue["id"])
    clock.advance(timedelta(days=14))
    assert timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()["status"] == "resolved"
    clock.advance(timedelta(days=14))
    assert timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()["status"] == "closed"


def test_list_applies_timers(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    _reply(timed, issue["id"])
    clock.advance(timedelta(days=15))
    rows = timed.get("/issues", headers=CUSTOMER).json()
    assert next(r for r in rows if r["id"] == issue["id"])["status"] == "resolved"


def test_open_issue_has_no_timer(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    clock.advance(timedelta(days=400))
    assert timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()["status"] == "open"


def test_reply_on_issue_closed_by_timer_opens_follow_up(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    _reply(timed, issue["id"])
    clock.advance(timedelta(days=14))
    timed.get("/issues", headers=CUSTOMER)  # waiting_customer -> resolved
    clock.advance(timedelta(days=14))
    res = timed.post(f"/issues/{issue['id']}/comments", json={"body": "again"}, headers=CUSTOMER)
    assert res.status_code == 201, res.text
    follow_up = timed.get(f"/issues/{res.json()['issue_id']}", headers=CUSTOMER).json()
    assert follow_up["follows_id"] == issue["id"]


# -- follow-ups --------------------------------------------------------------------------


def _close(client: Client, issue_id: str) -> None:
    last = client.get(f"/admin/issues/{issue_id}", headers=OPERATOR).json()["events"][-1]["id"]
    res = client.post(
        f"/admin/issues/{issue_id}/status",
        json={"to": "closed", "last_seen_event_id": last},
        headers=OPERATOR,
    )
    assert res.status_code == 200, res.text


def test_follow_up_keeps_scope_reporter_and_kind() -> None:
    for client in _client():
        closed = _open(client)
        client.post(f"/admin/issues/{closed['id']}/kind", json={"kind": "bug"}, headers=OPERATOR)
        _close(client, closed["id"])
        res = client.post(f"/issues/{closed['id']}/comments", json={"body": "x"}, headers=CUSTOMER)
        new = client.get(f"/admin/issues/{res.json()['issue_id']}", headers=OPERATOR).json()
        assert (new["scope_id"], new["user_id"], new["kind"]) == ("tenant-a", "user-a", "bug")
        assert new["title"] == "x"


def test_follow_up_is_rate_limited(clock: FakeClock) -> None:
    config = SupportConfig(clock=clock, rate_limit_count=1)
    for client in _client(config=config):
        closed = _open(client)
        _close(client, closed["id"])
        res = client.post(f"/issues/{closed['id']}/comments", json={"body": "x"}, headers=CUSTOMER)
        assert res.status_code == 429


# -- rate limit --------------------------------------------------------------------------


def test_rate_limit_returns_429_with_retry_after(clock: FakeClock) -> None:
    config = SupportConfig(clock=clock, rate_limit_count=2, rate_limit_window=timedelta(hours=1))
    for client in _client(config=config):
        issue = _open(client)
        clock.advance(timedelta(minutes=10))
        res = client.post(f"/issues/{issue['id']}/comments", json={"body": "1"}, headers=CUSTOMER)
        assert res.status_code == 201
        res = client.post("/issues", json={"body": "third"}, headers=CUSTOMER)
        assert res.status_code == 429
        assert res.headers["retry-after"] == str(50 * 60)
        _reply(client, issue["id"])  # operators are not limited
        clock.advance(timedelta(minutes=50))
        assert client.post("/issues", json={"body": "later"}, headers=CUSTOMER).status_code == 201


def test_unknown_user_gets_401() -> None:
    for client in _client():
        assert client.get("/issues").status_code == 401


def test_routes_mount_under_path() -> None:
    for client in _client(config=SupportConfig(path="/v1")):
        assert client.get("/v1/issues", headers=CUSTOMER).status_code == 200
        assert client.get("/issues", headers=CUSTOMER).status_code == 404


# -- mail --------------------------------------------------------------------------------


def test_public_reply_is_mailed_and_recorded() -> None:
    mailer = RecordingMailer()
    for client in _client(mailer=mailer):
        issue = _open(client, "Export fails")
        event = _reply(client, issue["id"])
        assert [n.event_id for n in mailer.sent] == [uuid.UUID(event["id"])]
        assert mailer.sent[0].issue_title == "Export fails"
        stored = _admin_event(client, issue["id"], event["id"])
        assert stored["notified_at"] is not None
        assert stored["notify_skipped_reason"] is None


def test_internal_note_is_not_mailed() -> None:
    mailer = RecordingMailer()
    for client in _client(mailer=mailer):
        issue = _open(client)
        event = _reply(client, issue["id"], internal=True)
        assert mailer.sent == []
        assert _admin_event(client, issue["id"], event["id"])["notify_skipped_reason"] == "internal"


def test_no_mailer_is_recorded() -> None:
    for client in _client():
        issue = _open(client)
        event = _reply(client, issue["id"])
        assert (
            _admin_event(client, issue["id"], event["id"])["notify_skipped_reason"] == "no_mailer"
        )


def test_failed_commit_sends_no_mail() -> None:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    fail = {"commit": False}

    @asynccontextmanager
    async def open_session(caller: Caller) -> AsyncGenerator[AsyncSession]:
        async with sessions.begin() as session:
            yield session
            if fail["commit"]:
                raise RuntimeError("commit failed")

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(SupportBase.metadata.create_all)

    mailer = RecordingMailer()
    adapter = SupportAdapter(caller=caller_from_header, session=open_session, mailer=mailer)
    app = Litestar(
        plugins=[SupportPlugin(adapter=adapter, config=SupportConfig())], on_startup=[setup]
    )
    with TestClient(app=app) as client:
        issue = _open(client)
        fail["commit"] = True
        res = client.post(
            f"/admin/issues/{issue['id']}/comments", json={"body": "on it"}, headers=OPERATOR
        )
        assert res.status_code == 500
        assert mailer.sent == []
        fail["commit"] = False
        events = client.get(f"/admin/issues/{issue['id']}", headers=OPERATOR).json()["events"]
        assert [e["event_type"] for e in events] == ["opened"]


def test_mail_is_sent_after_commit() -> None:
    committed: list[bool] = []

    class _Checking:
        async def send_reply(self, notice: ReplyNotice) -> bool:
            committed.append(seen_commit["done"])
            return True

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    seen_commit = {"done": False}

    @asynccontextmanager
    async def open_session(caller: Caller) -> AsyncGenerator[AsyncSession]:
        seen_commit["done"] = False
        async with sessions.begin() as session:
            yield session
        seen_commit["done"] = True

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(SupportBase.metadata.create_all)

    adapter = SupportAdapter(caller=caller_from_header, session=open_session, mailer=_Checking())
    app = Litestar(
        plugins=[SupportPlugin(adapter=adapter, config=SupportConfig())], on_startup=[setup]
    )
    with TestClient(app=app) as client:
        issue = _open(client)
        event = _reply(client, issue["id"])
        assert committed == [True]
        assert _admin_event(client, issue["id"], event["id"])["notified_at"] is not None


class _NoAddress:
    async def send_reply(self, notice: ReplyNotice) -> bool:
        return False


class _Broken:
    async def send_reply(self, notice: ReplyNotice) -> bool:
        raise RuntimeError("smtp down")


@pytest.mark.parametrize(
    ("mailer", "reason"), [(_NoAddress(), "no_email"), (_Broken(), "send_failed")]
)
def test_mailer_outcomes_are_recorded(mailer: Any, reason: str) -> None:
    for client in _client(mailer=mailer):
        issue = _open(client)
        event = _reply(client, issue["id"])
        assert _admin_event(client, issue["id"], event["id"])["notify_skipped_reason"] == reason


# -- uploads -----------------------------------------------------------------------------


def test_upload_without_store_is_503() -> None:
    for client in _client():
        res = client.post(
            "/issue-attachments", files={"file": ("a.png", b"png", "image/png")}, headers=CUSTOMER
        )
        assert res.status_code == 503


@pytest.mark.parametrize("data", [b"", b"x" * 11])
def test_upload_size_limits(data: bytes) -> None:
    for client in _client(storage=MemoryStore(), config=SupportConfig(max_attachment_bytes=10)):
        res = client.post(
            "/issue-attachments", files={"file": ("a.png", data, "image/png")}, headers=CUSTOMER
        )
        assert res.status_code == 422


def test_attachment_url_comes_from_store() -> None:
    for client in _client(storage=MemoryStore()):
        att = client.post(
            "/issue-attachments", files={"file": ("a.png", b"png", "image/png")}, headers=CUSTOMER
        ).json()
        assert att["url"].startswith("memory://issue-attachments/tenant-a/")


# -- service functions -------------------------------------------------------------------

CUSTOMER_CALLER = Caller(scope_id="tenant-a", user_id="user-a", actor="customer@a.test")


def _with_session(
    work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]],
) -> None:
    async def main() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(SupportBase.metadata.create_all)
        try:
            await work(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    asyncio.run(main())


def test_import_issue_is_seeded_and_not_rate_limited(clock: FakeClock) -> None:
    config = SupportConfig(clock=clock, rate_limit_count=1)

    async def work(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions.begin() as session:
            svc = SupportService(session=session, config=config)
            old = await svc.import_issue(
                scope_id="tenant-a",
                user_id="user-a",
                actor="customer@a.test",
                body="from the old feedback table",
                created_at=clock.now,
            )
            events = (await session.scalars(sa.select(IssueEvent))).all()
            assert [e.notify_skipped_reason for e in events] == ["seeded"]
            assert old.created_at == clock.now
            issue, created = await svc.open_issue(CUSTOMER_CALLER, NewIssue(body="new one"))
            assert created
            assert issue.status == IssueStatus.OPEN

    _with_session(work)


def test_forget_user_deletes_screenshots_and_unlinks_issues(clock: FakeClock) -> None:
    store = MemoryStore()
    config = SupportConfig(clock=clock)

    async def work(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions.begin() as session:
            svc = SupportService(session=session, config=config, storage=store)
            att = await svc.upload(CUSTOMER_CALLER, data=b"png", mime_type="image/png")
            issue, _ = await svc.open_issue(
                CUSTOMER_CALLER, NewIssue(body="x", attachment_ids=(att.id,))
            )
            assert await svc.forget_user("user-a") == 1
            assert store.objects == {}
            await session.refresh(issue)
            assert issue.user_id is None

    _with_session(work)


def test_reap_staged_keeps_bound_uploads(clock: FakeClock) -> None:
    store = MemoryStore()
    config = SupportConfig(clock=clock)

    async def work(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions.begin() as session:
            svc = SupportService(session=session, config=config, storage=store)
            stale = await svc.upload(CUSTOMER_CALLER, data=b"1", mime_type="image/png")
            bound = await svc.upload(CUSTOMER_CALLER, data=b"2", mime_type="image/png")
            await svc.open_issue(CUSTOMER_CALLER, NewIssue(body="x", attachment_ids=(bound.id,)))
            clock.advance(timedelta(hours=25))
            assert await svc.reap_staged() == 1
            left = (await session.scalars(sa.select(IssueAttachment.id))).all()
            assert left == [bound.id]
            assert stale.object_key not in store.objects

    _with_session(work)


def test_referenced_replay_sessions(clock: FakeClock) -> None:
    config = SupportConfig(clock=clock)
    live, closed = uuid.uuid4(), uuid.uuid4()

    async def work(sessions: async_sessionmaker[AsyncSession]) -> None:
        async with sessions.begin() as session:
            svc = SupportService(session=session, config=config)
            await svc.open_issue(
                CUSTOMER_CALLER, NewIssue(body="a", context={"replay_session_id": str(live)})
            )
            gone, _ = await svc.open_issue(
                CUSTOMER_CALLER, NewIssue(body="b", context={"replay_session_id": str(closed)})
            )
            await svc.set_status(gone, IssueStatus.CLOSED, actor="op", side=Side.OPERATOR)
            await svc.open_issue(
                CUSTOMER_CALLER, NewIssue(body="c", context={"replay_session_id": "not-a-uuid"})
            )
            assert await referenced_replay_sessions(session) == {live}

    _with_session(work)


# -- migration ---------------------------------------------------------------------------


def _shape(conn: sa.Connection) -> dict[str, set[tuple[str, bool]]]:
    inspector = sa.inspect(conn)
    return {
        table: {(c["name"], bool(c["nullable"])) for c in inspector.get_columns(table)}
        for table in ("issues", "issue_events", "issue_attachments")
    }


def test_migration_matches_models() -> None:
    migrated = sa.create_engine("sqlite://")
    with migrated.begin() as conn:
        create_tables(Operations(MigrationContext.configure(conn)))
        from_migration = _shape(conn)
        indexes = {i["name"] for i in sa.inspect(conn).get_indexes("issues")}
    modelled = sa.create_engine("sqlite://")
    with modelled.begin() as conn:
        SupportBase.metadata.create_all(conn)
        from_models = _shape(conn)
        model_indexes = {i["name"] for i in sa.inspect(conn).get_indexes("issues")}
    assert from_migration == from_models
    assert indexes == model_indexes
    with migrated.begin() as conn:
        drop_tables(Operations(MigrationContext.configure(conn)))
        assert sa.inspect(conn).get_table_names() == []


def test_dedupe_index_allows_reuse_after_close() -> None:
    engine = sa.create_engine("sqlite://")
    SupportBase.metadata.create_all(engine)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    row: dict[str, Any] = {
        "scope_id": "s",
        "body": "b",
        "source": "monitor",
        "dedupe_key": "k",
        "created_at": now,
        "updated_at": now,
    }
    issues = SupportBase.metadata.tables["issues"]
    with engine.begin() as conn:
        conn.execute(issues.insert(), [{**row, "id": uuid.uuid4(), "status": "closed"}])
        conn.execute(issues.insert(), [{**row, "id": uuid.uuid4(), "status": "open"}])
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(issues.insert(), [{**row, "id": uuid.uuid4(), "status": "open"}])


# -- pytest plugin -----------------------------------------------------------------------


def test_contract_without_fixtures_says_what_to_define(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        from litestar_feedback.support.contract import SupportContract

        class TestContract(SupportContract):
            pass
        """
    )
    result = pytester.runpytest_inprocess("-k", "test_customer_opens_issue")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*define the 'client' fixture in your conftest.py*"])


# -- triage ------------------------------------------------------------------------------


def test_kind_change_keeps_timer(timed: Client, clock: FakeClock) -> None:
    issue = _open(timed)
    _reply(timed, issue["id"])
    clock.advance(timedelta(days=10))
    res = timed.post(f"/admin/issues/{issue['id']}/kind", json={"kind": "bug"}, headers=OPERATOR)
    assert res.status_code == 200, res.text
    clock.advance(timedelta(days=4))
    assert timed.get(f"/issues/{issue['id']}", headers=CUSTOMER).json()["status"] == "resolved"
