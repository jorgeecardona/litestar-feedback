"""Who is calling, and how to open a database session for them.

Both plugins use these types. Neither plugin imports the other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from litestar import Request
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated user of one request, as the plugins see it.

    ``scope_id`` is the product's tenancy key: crm ``tenant_id``, accounting ``company_id``,
    tyko ``reporter_user_id``. Rows the plugins write carry it, and customer routes only
    return rows with the caller's ``scope_id``.

    Both ids are strings so that UUID keys (crm, tyko) and integer keys (accounting) fit the
    same columns. Pass ``str(user.tenant_id)``. Row-level security policies then compare text.

    ``actor`` is the label written on events, usually the user's email.

    ``is_operator`` opens the ``/admin`` routes, which read across all scopes.
    """

    scope_id: str
    user_id: str | None
    actor: str
    is_operator: bool = False


CallerResolver = Callable[[Request[Any, Any, Any]], Awaitable[Caller]]
"""Return the ``Caller`` for a request. Raise ``NotAuthorizedException`` when nobody is signed in."""

SessionOpener = Callable[[Caller], AbstractAsyncContextManager[AsyncSession]]
"""Open a session for one request, inside a transaction that commits on exit.

The plugin passes the ``Caller`` so the host can prepare the session for it, for example
``SET LOCAL app.tenant_id`` for row-level security, or a session that bypasses row-level
security when ``caller.is_operator`` is true. An exception inside the block must roll back.
"""
