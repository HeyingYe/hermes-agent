"""Unit tests for the P5.1 TokenBudget circuit breaker (agent/token_budget.py)."""

from agent.token_budget import TokenBudget


def test_no_limits_never_breaches():
    tb = TokenBudget(per_turn_limit=0, per_session_limit=0)
    tb.add(10_000_000)
    assert tb.breach() is None


def test_per_turn_breach_at_limit():
    tb = TokenBudget(per_turn_limit=3_000_000, per_session_limit=0)
    tb.add(2_999_999)
    assert tb.breach() is None
    tb.add(1)  # now == limit
    assert tb.breach() == "per_turn"


def test_per_session_breach():
    tb = TokenBudget(per_turn_limit=0, per_session_limit=8_000_000)
    for _ in range(8):
        tb.add(1_000_000)
    assert tb.breach() == "per_session"


def test_reset_turn_clears_turn_not_session():
    tb = TokenBudget(per_turn_limit=3_000_000, per_session_limit=8_000_000)
    tb.add(3_000_000)
    assert tb.breach() == "per_turn"
    tb.reset_turn()
    assert tb.turn_used == 0
    assert tb.session_used == 3_000_000
    assert tb.breach() is None  # turn cleared, session 3M < 8M


def test_session_breach_survives_turn_resets():
    """The whole point of the session cap: many small turns still add up."""
    tb = TokenBudget(per_turn_limit=3_000_000, per_session_limit=8_000_000)
    for _ in range(9):
        tb.add(1_000_000)
        tb.reset_turn()
    assert tb.session_used == 9_000_000
    assert tb.breach() == "per_session"


def test_reset_session_clears_both():
    tb = TokenBudget(per_turn_limit=3_000_000, per_session_limit=8_000_000)
    tb.add(5_000_000)
    tb.reset_session()
    assert tb.turn_used == 0
    assert tb.session_used == 0
    assert tb.breach() is None


def test_disabled_never_breaches():
    tb = TokenBudget(per_turn_limit=1, per_session_limit=1, enabled=False)
    tb.add(1_000_000)
    assert tb.breach() is None


def test_non_positive_add_ignored():
    tb = TokenBudget(per_turn_limit=100, per_session_limit=100)
    tb.add(-5)
    tb.add(0)
    assert tb.turn_used == 0
    assert tb.session_used == 0


# --- P5.4: in-turn expensive-model (Opus) tighter caps ---

def test_expensive_cap_is_tighter():
    tb = TokenBudget(
        per_turn_limit=3_000_000,
        per_session_limit=8_000_000,
        expensive_per_turn_limit=1_500_000,
        expensive_per_session_limit=4_000_000,
        expensive_models=["claude-opus-4-8"],
    )
    tb.add(1_600_000)  # under normal cap, over expensive cap
    assert tb.breach(expensive=False) is None
    assert tb.breach(expensive=True) == "per_turn"


def test_is_expensive_membership():
    tb = TokenBudget(expensive_models=["claude-opus-4-8", "claude-opus-4-6"])
    assert tb.is_expensive("claude-opus-4-8") is True
    assert tb.is_expensive("gpt-5.5") is False
    assert tb.is_expensive(None) is False
    assert tb.is_expensive("") is False


def test_expensive_cap_applies_when_normal_unlimited():
    tb = TokenBudget(
        per_turn_limit=0,  # unlimited normally
        expensive_per_turn_limit=1_500_000,
        expensive_models=["claude-opus-4-8"],
    )
    tb.add(1_500_000)
    assert tb.breach(expensive=False) is None
    assert tb.breach(expensive=True) == "per_turn"


def test_default_breach_is_non_expensive():
    """Backward-compat: breach() with no arg behaves as expensive=False."""
    tb = TokenBudget(
        per_turn_limit=3_000_000,
        expensive_per_turn_limit=1_000_000,
        expensive_models=["claude-opus-4-8"],
    )
    tb.add(2_000_000)
    assert tb.breach() is None  # under the 3M normal cap
