"""pytest plugin: stubs for the five fixtures the support contract test needs.

pytest loads it through the ``pytest11`` entry point whenever this package is installed. Each
stub fails with a clear message until your ``conftest.py`` defines the real fixture.

Run the contract in your repo:

1. Define ``client``, ``customer_headers``, ``operator_headers``, ``other_tenant_headers`` and
   ``seeded_issue`` in ``tests/conftest.py``.
2. Add a test file:

```python
from litestar_feedback.support.contract import SupportContract


class TestSupportContract(SupportContract):
    pass
```
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest


def _missing(name: str) -> NoReturn:
    pytest.fail(
        f"define the '{name}' fixture in your conftest.py to run the support contract "
        "(see litestar_feedback.support.pytest_plugin)",
        pytrace=False,
    )


@pytest.fixture
def client() -> Any:
    """A ``litestar.testing.TestClient`` for the app under test."""
    _missing("client")


@pytest.fixture
def customer_headers() -> dict[str, str]:
    """Headers that sign in a customer of scope A."""
    _missing("customer_headers")


@pytest.fixture
def operator_headers() -> dict[str, str]:
    """Headers that sign in an operator."""
    _missing("operator_headers")


@pytest.fixture
def other_tenant_headers() -> dict[str, str]:
    """Headers that sign in a customer of scope B."""
    _missing("other_tenant_headers")


@pytest.fixture
def seeded_issue() -> dict[str, Any]:
    """An issue in scope A, as returned by ``POST /issues``."""
    _missing("seeded_issue")
