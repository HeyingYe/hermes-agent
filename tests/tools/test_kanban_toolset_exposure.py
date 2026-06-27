"""Kanban worker toolset exposure behavior tests."""
from __future__ import annotations

import model_tools


def _tool_names(tool_defs):
    return {td.get("function", {}).get("name") for td in tool_defs}


def test_dispatcher_worker_receives_kanban_lifecycle_tools(monkeypatch):
    """HERMES_KANBAN_TASK should append the explicit kanban toolset.

    This keeps dispatcher-spawned workers functional even when product-layer
    Kanban tools are removed from the generic Hermes core platform toolsets.
    """
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_testworker")
    model_tools._tool_defs_cache.clear()

    names = _tool_names(
        model_tools.get_tool_definitions(
            enabled_toolsets=["terminal"],
            quiet_mode=True,
        )
    )

    assert "terminal" in names
    assert "kanban_complete" in names
    assert "kanban_block" in names
    assert "kanban_heartbeat" in names
    assert "kanban_comment" in names
    assert "kanban_create" in names
    assert "kanban_show" in names
    assert "kanban_link" in names
    assert "kanban_list" not in names
    assert "kanban_unblock" not in names


def test_normal_session_does_not_receive_kanban_tools_from_core(monkeypatch):
    """A normal session with generic core toolsets should not see Kanban tools."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    model_tools._tool_defs_cache.clear()

    names = _tool_names(
        model_tools.get_tool_definitions(
            enabled_toolsets=["hermes-cli"],
            quiet_mode=True,
        )
    )

    assert "terminal" in names
    assert not {name for name in names if name.startswith("kanban_")}
