"""Mutable runtime controls, separate from the frozen ``Settings``.

``Settings`` is immutable by design — it is the configuration the process booted with.
But a few things must change *while* the process runs, without a restart: the plugin's
``/zibbo enable`` and ``/zibbo disable`` flip optimization on and off through
``POST /internal/enable`` and ``/internal/disable``.

This holds exactly those live switches. It is the single source of truth for whether
optimization is active: seeded from ``settings.optimization_enabled`` at startup, then
owned here. The policy engine reads it live, so a flip takes effect on the very next
request. Thread-safe, because it is read from worker threads and written from the event
loop.
"""

from __future__ import annotations

import threading


class RuntimeControl:
    """Process-wide switches that outlive a single request but not the process."""

    __slots__ = ("_lock", "_optimization_enabled", "_prompt_optimization_enabled")

    def __init__(
        self, *, optimization_enabled: bool, prompt_optimization_enabled: bool = False
    ) -> None:
        self._lock = threading.Lock()
        self._optimization_enabled = optimization_enabled
        self._prompt_optimization_enabled = prompt_optimization_enabled

    @property
    def optimization_enabled(self) -> bool:
        with self._lock:
            return self._optimization_enabled

    def set_optimization_enabled(self, enabled: bool) -> bool:
        """Set the flag; return the value it now holds."""
        with self._lock:
            self._optimization_enabled = enabled
            return self._optimization_enabled

    @property
    def prompt_optimization_enabled(self) -> bool:
        """Whether deterministic prompt de-duplication is active. Seeded from settings,
        then owned here so ``zibbo enable/disable prompt`` takes effect without a
        restart. The registry and detector are updated in lockstep with this flag."""
        with self._lock:
            return self._prompt_optimization_enabled

    def set_prompt_optimization_enabled(self, enabled: bool) -> bool:
        with self._lock:
            self._prompt_optimization_enabled = enabled
            return self._prompt_optimization_enabled
