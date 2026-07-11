"""Persistence: Postgres repositories for token/cost analytics (next phase).

The transformation cache is *not* here — it lives in ``gateway/cache/``, because it
stores deterministic transformation outputs, not application data. This package is
reserved for durable analytics persistence.
"""
