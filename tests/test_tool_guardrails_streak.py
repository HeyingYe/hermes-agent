"""Unit tests for the P5.2 read-only no-progress streak detector.

The pre-existing idempotent_no_progress detector only fires when the SAME tool
is called with the SAME args repeatedly. These tests cover the new cross-signature
streak that catches "many DIFFERENT read_file calls with no writes" loops.
"""

from agent.tool_guardrails import ToolCallGuardrailConfig, ToolCallGuardrailController


def _ctrl(hard_stop=True, warn_after=3, block_after=5):
    cfg = ToolCallGuardrailConfig(
        hard_stop_enabled=hard_stop,
        readonly_streak_warn_after=warn_after,
        readonly_streak_block_after=block_after,
    )
    return ToolCallGuardrailController(cfg)


def test_distinct_file_reads_accumulate_and_halt():
    """The whole point: distinct args (different files) still trip the breaker."""
    c = _ctrl(block_after=5)
    halted_at = None
    for i in range(10):
        d = c.after_call("read_file", {"path": f"f{i}.py"}, f"contents {i}", failed=False)
        if d.should_halt:
            halted_at = i + 1
            assert d.code == "readonly_no_progress_halt"
            break
    assert halted_at == 5


def test_mutating_tool_resets_streak():
    c = _ctrl(block_after=5)
    for i in range(4):
        c.after_call("read_file", {"path": f"f{i}.py"}, "x", failed=False)
    # a write = progress → streak resets
    c.after_call("write_file", {"path": "out.py"}, "ok", failed=False)
    halted = False
    for i in range(4):  # 4 more reads, below block_after again
        d = c.after_call("read_file", {"path": f"g{i}.py"}, "y", failed=False)
        if d.should_halt:
            halted = True
    assert not halted


def test_warn_fires_before_block():
    c = _ctrl(warn_after=3, block_after=5)
    warned_at = None
    for i in range(4):
        d = c.after_call("read_file", {"path": f"f{i}.py"}, "x", failed=False)
        if d.action == "warn" and d.code == "readonly_no_progress_warning":
            warned_at = i + 1
    assert warned_at == 3


def test_no_halt_when_hard_stop_disabled():
    c = _ctrl(hard_stop=False, block_after=5)
    halted = False
    for i in range(12):
        d = c.after_call("read_file", {"path": f"f{i}.py"}, "x", failed=False)
        if d.should_halt:
            halted = True
    assert not halted


def test_reset_for_turn_clears_streak():
    c = _ctrl(block_after=5)
    for i in range(4):
        c.after_call("read_file", {"path": f"f{i}.py"}, "x", failed=False)
    c.reset_for_turn()
    halted = False
    for i in range(4):
        d = c.after_call("read_file", {"path": f"h{i}.py"}, "x", failed=False)
        if d.should_halt:
            halted = True
    assert not halted


def test_config_from_mapping_reads_streak_keys():
    cfg = ToolCallGuardrailConfig.from_mapping(
        {
            "hard_stop_enabled": True,
            "warn_after": {"readonly_streak": 7},
            "hard_stop_after": {"readonly_streak": 11},
        }
    )
    assert cfg.readonly_streak_warn_after == 7
    assert cfg.readonly_streak_block_after == 11
