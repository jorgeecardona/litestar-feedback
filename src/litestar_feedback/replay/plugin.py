"""``ReplayPlugin``: mounts the session replay routes on a Litestar app."""

from __future__ import annotations

from dataclasses import dataclass, field

from litestar import Router
from litestar.config.app import AppConfig
from litestar.di import Provide
from litestar.plugins import InitPlugin
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_feedback.caller import Caller
from litestar_feedback.replay.adapter import ReplayAdapter, ReplayConfig
from litestar_feedback.replay.routes import ROUTE_HANDLERS, ReplayRuntime, replay_error_handler
from litestar_feedback.replay.service import PruneResult, ReplayError, ReplayService


@dataclass(eq=False)
class ReplayPlugin(InitPlugin):
    """Adds the session replay routes under ``config.path``.

    The routes register one dependency, ``feedback_replay``, on their own router only.
    """

    adapter: ReplayAdapter
    config: ReplayConfig = field(default_factory=ReplayConfig)

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        """Append the replay router to the app's route handlers."""
        runtime = ReplayRuntime(adapter=self.adapter, config=self.config)

        def provide_runtime() -> ReplayRuntime:
            return runtime

        router = Router(
            path=self.config.path,
            route_handlers=ROUTE_HANDLERS,
            dependencies={"feedback_replay": Provide(provide_runtime, sync_to_thread=False)},
            exception_handlers={ReplayError: replay_error_handler},
            tags=["replay"],
        )
        app_config.route_handlers = [*app_config.route_handlers, router]
        return app_config

    async def prune(self, caller: Caller) -> PruneResult:
        """Delete sessions older than ``adapter.retention_days``, keeping protected ones.

        Run it from a scheduler. ``caller`` is the system identity the session opener needs,
        usually an operator ``Caller`` so row-level security does not hide other scopes.
        """
        async with self.adapter.session(caller) as session:
            return await self._service(session).prune(
                retention_days=self.adapter.retention_days,
                protected=self.adapter.protected_sessions,
            )

    async def delete_user(self, caller: Caller, user_id: str) -> int:
        """Delete a user's sessions, keeping protected ones. Return how many were deleted."""
        async with self.adapter.session(caller) as session:
            return await self._service(session).delete_user(
                user_id, protected=self.adapter.protected_sessions
            )

    def _service(self, session: AsyncSession) -> ReplayService:
        return ReplayRuntime(adapter=self.adapter, config=self.config).service(session)
