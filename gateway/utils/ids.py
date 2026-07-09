"""Identifier generation."""

from __future__ import annotations

import uuid

REQUEST_ID_PREFIX = "req_"


def new_request_id() -> str:
    """Return a unique, log-greppable request identifier (``req_<32 hex>``)."""
    return f"{REQUEST_ID_PREFIX}{uuid.uuid4().hex}"


def is_request_id(value: str) -> bool:
    """Loose validation for client-supplied ``X-Request-ID`` headers."""
    return bool(value) and len(value) <= 128 and value.isprintable()
