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


def test_detects_product_terms_in_engine_core_files(tmp_path):
    guard = _load_guard_module()
    (tmp_path / "run_agent.py").write_text(
        'def build_prompt():\n    return "Open the Jarvis Dashboard"\n',
        encoding="utf-8",
    )

    issues = guard.scan_repository(tmp_path)

    assert any(
        issue.severity == "warning"
        and issue.code == "product-term-in-engine-file"
        and issue.match == "Jarvis"
        for issue in issues
    )
    assert any(issue.code == "product-term-in-engine-file" and issue.match == "Dashboard" for issue in issues)


def test_detects_user_facing_non_secret_hermes_behavior_env_vars(tmp_path):
    guard = _load_guard_module()
    docs = tmp_path / "docs" / "feature.md"
    docs.parent.mkdir(parents=True)
    docs.write_text(
        "Set HERMES_ENABLE_WIDGETS=1 in your shell to turn on widgets.\n"
        "Set HERMES_API_KEY for the credential.\n",
        encoding="utf-8",
    )

    issues = guard.scan_repository(tmp_path)

    assert any(
        issue.severity == "warning"
        and issue.code == "non-secret-hermes-env-behavior-config"
        and issue.match == "HERMES_ENABLE_WIDGETS"
        for issue in issues
    )
    assert not any(issue.match == "HERMES_API_KEY" for issue in issues)


def test_detects_tool_schema_budget_regression(tmp_path):
    guard = _load_guard_module()
    report = tmp_path / "docs" / "tool-schema-cost-report-20260627.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "# Tool Schema Cost Report\n\n"
        "Total tools: 45\n"
        "Total schema chars: 90000\n"
        "Estimated tokens: 22500\n",
        encoding="utf-8",
    )

    issues = guard.scan_repository(tmp_path)

    assert any(
        issue.severity == "warning"
        and issue.code == "tool-schema-budget-regression"
        and "22500" in issue.message
        for issue in issues
    )
