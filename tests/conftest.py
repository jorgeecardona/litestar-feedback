"""Fixtures: the five the support contract needs, plus helpers for the rule tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from litestar.testing import TestClient

from examples.support_app import MemoryStore, RecordingMailer, make_app
from litestar_feedback.support import SupportConfig

CUSTOMER = {"X-User": "customer@a.test"}
OPERATOR = {"X-User": "operator@a.test"}
OTHER = {"X-User": "customer@b.test"}


@dataclass
class FakeClock:
    """A clock the test moves by hand."""

    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient[Any]]:
    """The example app with a store and a mailer, shared by the contract tests."""
    app = make_app(
        config=SupportConfig(rate_limit_count=1000),
        storage=MemoryStore(),
        mailer=RecordingMailer(),
    )
    with TestClient(app=app) as c:
        yield c


@pytest.fixture
def customer_headers() -> dict[str, str]:
    return dict(CUSTOMER)


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return dict(OPERATOR)


@pytest.fixture
def other_tenant_headers() -> dict[str, str]:
    return dict(OTHER)


@pytest.fixture
def seeded_issue(client: TestClient[Any], customer_headers: dict[str, str]) -> dict[str, Any]:
    res = client.post("/issues", json={"body": "seeded issue"}, headers=customer_headers)
    assert res.status_code == 201, res.text
    return res.json()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
