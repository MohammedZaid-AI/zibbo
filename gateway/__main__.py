"""``python -m gateway`` — run the server with settings from the environment."""

from __future__ import annotations

import uvicorn

from gateway.config import Environment, get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "gateway.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment is Environment.DEVELOPMENT,
        log_config=None,  # logging is configured by gateway.logging
        access_log=False,  # RequestContextMiddleware emits the access log
    )


if __name__ == "__main__":
    main()
