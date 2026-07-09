"""Upstream provider clients.

Phase 2 adds the OpenAI client; Phase 6 adds Anthropic. Both will implement a
common ``Provider`` protocol so the gateway routes by base path, not by ``if``.
"""
