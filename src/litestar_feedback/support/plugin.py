"""``SupportPlugin``: mounts the support routes on a Litestar app."""

from __future__ import annotations

from dataclasses import dataclass, field

from litestar import Router
from litestar.config.app import AppConfig
from litestar.di import Provide
from litestar.plugins import InitPlugin

from litestar_feedback.support.adapter import SupportAdapter, SupportConfig
from litestar_feedback.support.routes import ROUTE_HANDLERS, SupportRuntime, support_error_handler
from litestar_feedback.support.service import SupportError


@dataclass(eq=False)
class SupportPlugin(InitPlugin):
    """Adds the customer support inbox routes under ``config.path``.

    The routes get their ``Caller`` and database session from ``adapter``. They register one
    dependency, ``feedback_support``, on their own router only.
    """

    adapter: SupportAdapter
    config: SupportConfig = field(default_factory=SupportConfig)

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        """Append the support router to the app's route handlers."""
        runtime = SupportRuntime(adapter=self.adapter, config=self.config)

        def provide_runtime() -> SupportRuntime:
            return runtime

        router = Router(
            path=self.config.path,
            route_handlers=ROUTE_HANDLERS,
            dependencies={"feedback_support": Provide(provide_runtime, sync_to_thread=False)},
            exception_handlers={SupportError: support_error_handler},
            tags=["support"],
        )
        app_config.route_handlers = [*app_config.route_handlers, router]
        return app_config
