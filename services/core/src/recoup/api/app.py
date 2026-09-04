"""FastAPI application factory.

Thin by design (ARCHITECTURE section 5): this module wires routers together
and depends on the rest of the package. Nothing else may depend on it --
enforced by the nothing-imports-api import-linter contract.
"""

from fastapi import FastAPI

from recoup.api.routes import health, metrics, webhooks
from recoup.planning.playbooks.loader import load_playbooks
from recoup.platform.config import get_settings
from recoup.platform.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    # TR-16: an invalid playbook must prevent boot, not degrade the first
    # time a case reaches planning -- so this runs unguarded, here, rather
    # than lazily on first use.
    playbooks = load_playbooks()

    app = FastAPI(
        title="Recoup",
        description="Revenue recovery control plane -- API service",
        version="0.1.0",
    )
    app.state.playbooks = playbooks

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(webhooks.router)

    return app


app = create_app()
