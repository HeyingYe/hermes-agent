"""Per-turn token budget — circuit breaker for runaway loops.

Companion to :class:`agent.iteration_budget.IterationBudget`.  Where
``IterationBudget`` caps the *number* of tool-calling iterations, ``TokenBudget``
caps the *total context tokens sent* within a single turn.

This catches the failure mode the iteration cap misses: a loop that re-sends a
very large context (e.g. ~245K tokens) on every iteration, so the real cost is
``iterations x context_size`` rather than iteration count alone.  Such loops are
dominated by cached input, so the per-turn budget is measured on
``prompt_tokens`` (input + cached input), *not* uncached input (which stays tiny
and would never trip the breaker).

``session_used`` is intentionally tracked for observability only. Cumulative
session prompt tokens are cost/latency telemetry, not a context-health signal:
they grow with successful useful work and must not freeze an otherwise valid
conversation.

A scope whose limit is ``<= 0`` is treated as unlimited.  ``enabled=False``
disables the breaker entirely, so the default behavior is fully backward
compatible when limits are unset.
"""

from __future__ import annotations

import threading
from typing import Optional


class TokenBudget:
    """Thread-safe token counter with a per-turn circuit breaker.

    Feed it the ``prompt_tokens`` of each API response via :meth:`add`.  Call
    :meth:`reset_turn` at the start of every turn and :meth:`reset_session` when
    a fresh session begins.  Check :meth:`breach` at the top of the loop to decide
    whether to stop. ``per_session_limit`` is retained for config compatibility
    and observability, but does not trigger ``breach()``.
    """

    def __init__(
        self,
        per_turn_limit: int = 0,
        per_session_limit: int = 0,
        enabled: bool = True,
        expensive_per_turn_limit: int = 0,
        expensive_per_session_limit: int = 0,
        expensive_models=None,
    ):
        # limit <= 0 => unlimited for that scope
        self.per_turn_limit = max(0, int(per_turn_limit or 0))
        self.per_session_limit = max(0, int(per_session_limit or 0))
        # P5.4: tighter caps applied while the active model is "expensive"
        # (e.g. Opus). 0 = no separate expensive cap for that scope.
        self.expensive_per_turn_limit = max(0, int(expensive_per_turn_limit or 0))
        self.expensive_per_session_limit = max(0, int(expensive_per_session_limit or 0))
        self.expensive_models = frozenset(expensive_models or ())
        self.enabled = bool(enabled)
        self._turn_used = 0
        self._session_used = 0
        self._lock = threading.Lock()

    def is_expensive(self, model) -> bool:
        """True if ``model`` is in the configured expensive set (P5.4)."""
        return bool(model) and model in self.expensive_models

    def add(self, prompt_tokens: int) -> None:
        """Record the context tokens sent on one API call (prompt_tokens)."""
        if not prompt_tokens or prompt_tokens <= 0:
            return
        with self._lock:
            self._turn_used += int(prompt_tokens)
            self._session_used += int(prompt_tokens)

    def reset_turn(self) -> None:
        """Clear the per-turn counter (call at the start of each turn)."""
        with self._lock:
            self._turn_used = 0

    def reset_session(self) -> None:
        """Clear both counters (call when a fresh session starts)."""
        with self._lock:
            self._turn_used = 0
            self._session_used = 0

    @property
    def turn_used(self) -> int:
        with self._lock:
            return self._turn_used

    @property
    def session_used(self) -> int:
        with self._lock:
            return self._session_used

    def breach(self, expensive: bool = False) -> Optional[str]:
        """Return ``"per_turn"`` if the per-turn cap is hit, else None.

        When ``expensive`` is True (the active model is in ``expensive_models``,
        P5.4), the tighter expensive per-turn cap applies. Session cumulative
        tokens remain telemetry only and never block a turn.
        """
        if not self.enabled:
            return None
        with self._lock:
            per_turn = self.per_turn_limit
            if expensive:
                if self.expensive_per_turn_limit:
                    per_turn = (
                        self.expensive_per_turn_limit
                        if per_turn == 0
                        else min(per_turn, self.expensive_per_turn_limit)
                    )
            if per_turn and self._turn_used >= per_turn:
                return "per_turn"
        return None


__all__ = ["TokenBudget"]
