"""Per-turn / per-session token budget — circuit breaker for runaway loops.

Companion to :class:`agent.iteration_budget.IterationBudget`.  Where
``IterationBudget`` caps the *number* of tool-calling iterations, ``TokenBudget``
caps the *total context tokens sent* within a single turn and across a whole
session.

This catches the failure mode the iteration cap misses: a loop that re-sends a
very large context (e.g. ~245K tokens) on every iteration, so the real cost is
``iterations x context_size`` rather than iteration count alone.  Such loops are
dominated by cached input, so the budget is measured on ``prompt_tokens``
(input + cached input), *not* uncached input (which stays tiny and would never
trip the breaker).

A scope whose limit is ``<= 0`` is treated as unlimited.  ``enabled=False``
disables the breaker entirely, so the default behavior is fully backward
compatible when limits are unset.
"""

from __future__ import annotations

import threading
from typing import Optional


class TokenBudget:
    """Thread-safe cumulative token counter with per-turn and per-session caps.

    Feed it the ``prompt_tokens`` of each API response via :meth:`add`.  Call
    :meth:`reset_turn` at the start of every turn and :meth:`reset_session` when
    a fresh session begins.  Check :meth:`breach` at the top of the loop to
    decide whether to stop.
    """

    def __init__(
        self,
        per_turn_limit: int = 0,
        per_session_limit: int = 0,
        enabled: bool = True,
    ):
        # limit <= 0 => unlimited for that scope
        self.per_turn_limit = max(0, int(per_turn_limit or 0))
        self.per_session_limit = max(0, int(per_session_limit or 0))
        self.enabled = bool(enabled)
        self._turn_used = 0
        self._session_used = 0
        self._lock = threading.Lock()

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

    def breach(self) -> Optional[str]:
        """Return ``"per_turn"`` / ``"per_session"`` if a cap is hit, else None."""
        if not self.enabled:
            return None
        with self._lock:
            if self.per_turn_limit and self._turn_used >= self.per_turn_limit:
                return "per_turn"
            if self.per_session_limit and self._session_used >= self.per_session_limit:
                return "per_session"
        return None


__all__ = ["TokenBudget"]
