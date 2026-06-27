"""Behavior tests for scripts/jarvis_architecture_guard.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "jarvis_architecture_guard.py"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("jarvis_architecture_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_detects_kanban_tools_in_shared_core_toolset(tmp_path):
    guard = _load_guard_module()
    (tmp_path / "toolsets.py").write_text(
        "_HERMES_CORE_TOOLS = [\n"
        "    'terminal',\n"
        "    'kanban_complete',\n"
        "]\n",
        encoding="utf-8",
    )

    issues = guard.scan_repository(tmp_path)

    assert any(
        issue.severity == "error"
        and issue.code == "core-tool-product-tool"
        and "kanban_complete" in issue.message
        for issue in issues
    )


def test_detects_default_on_kanban_side_effects(tmp_path):
    guard = _load_guard_module()
    config = tmp_path / "hermes_cli" / "config.py"
    config.parent.mkdir(parents=True)
    config.write_text(
        "DEFAULT_CONFIG = {\n"
        "    'kanban': {\n"
        "        'dispatch_in_gateway': True,\n"
        "        'track_background_processes': True,\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )

    issues = guard.scan_repository(tmp_path)
    codes = {issue.code for issue in issues if issue.severity == "error"}

    assert "default-on-kanban-side-effect" in codes


def test_current_repo_has_no_architecture_guard_errors():
    guard = _load_guard_module()

    issues = guard.scan_repository(REPO_ROOT)
    errors = [issue for issue in issues if issue.severity == "error"]

    assert errors == []


def test_text_formatter_summarizes_error_and_warning_counts(tmp_path):
    guard = _load_guard_module()
    issue = guard.ArchitectureIssue(
        severity="warning",
        code="internal-env-compat-shim",
        path=Path("tools/process_registry.py"),
        line=127,
        message="Compatibility env shim should remain internal.",
        match="HERMES_KANBAN_TRACK_BACKGROUND",
    )

    output = guard.format_text([issue])

    assert "errors=0" in output
    assert "warnings=1" in output
    assert "internal-env-compat-shim" in output
