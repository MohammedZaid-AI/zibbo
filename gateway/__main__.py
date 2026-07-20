"""``python -m gateway`` — run the server with settings from the environment."""

from __future__ import annotations

import os

import uvicorn

from gateway.config import Environment, Settings, get_settings


def _reload_enabled(settings: Settings) -> bool:
    """Autoreload in development — but never when ``ZIBBO_RELOAD`` says otherwise.

    ``zibbo start`` spawns the gateway detached and sets ``ZIBBO_RELOAD=false``: uvicorn's
    reload supervisor spawns a worker subprocess that is orphaned and killed under
    ``DETACHED_PROCESS`` on Windows, so the server would die right after "startup complete"
    and ``start`` would report a bare timeout. A developer running ``python -m gateway`` by
    hand sets nothing and still gets reload in development.
    """
    override = os.environ.get("ZIBBO_RELOAD")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return settings.environment is Environment.DEVELOPMENT


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        reload=_reload_enabled(settings),
        log_config=None,  # logging is configured by gateway.logging
        access_log=False,  # RequestContextMiddleware emits the access log
    )


if __name__ == "__main__":
    main()
