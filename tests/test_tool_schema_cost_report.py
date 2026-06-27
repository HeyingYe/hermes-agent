"""Behavior tests for scripts/tool_schema_cost_report.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "tool_schema_cost_report.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("tool_schema_cost_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(name: str, description: str, properties: dict):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties},
        },
    }


def test_summarize_tool_schemas_counts_and_ranks_tools_by_json_size():
    report = _load_report_module()
    tools = [
        _tool("small", "s", {}),
        _tool("large", "long description" * 20, {"value": {"type": "string"}}),
    ]
    toolset_map = {"small": "core", "large": "plugin-big"}

    summary = report.summarize_tool_schemas(tools, toolset_map=toolset_map, top_n=2)

    assert summary["total_tools"] == 2
    assert summary["total_schema_chars"] > 0
    assert summary["top_tools"][0]["name"] == "large"
    assert summary["top_tools"][0]["toolset"] == "plugin-big"
    assert summary["by_toolset"]["core"]["tool_count"] == 1
    assert summary["by_toolset"]["plugin-big"]["tool_count"] == 1


def test_markdown_formatter_includes_recommendations_for_large_non_core_tools():
    report = _load_report_module()
    summary = {
        "toolsets": ["hermes-cli"],
        "total_tools": 2,
        "total_schema_chars": 1200,
        "estimated_tokens": 300,
        "by_toolset": {
            "core": {"tool_count": 1, "schema_chars": 100, "estimated_tokens": 25},
            "plugin-big": {"tool_count": 1, "schema_chars": 1100, "estimated_tokens": 275},
        },
        "top_tools": [
            {"name": "large", "toolset": "plugin-big", "schema_chars": 1100, "estimated_tokens": 275},
        ],
        "recommendations": ["Consider deferring plugin-big behind tool_search/plugin opt-in."],
    }

    markdown = report.format_markdown(summary)

    assert "# Tool Schema Cost Report" in markdown
    assert "plugin-big" in markdown
    assert "Consider deferring" in markdown


def test_json_formatter_round_trips_summary():
    report = _load_report_module()
    summary = {
        "toolsets": ["coding"],
        "total_tools": 1,
        "total_schema_chars": 4,
        "estimated_tokens": 1,
        "by_toolset": {},
        "top_tools": [],
        "recommendations": [],
    }

    encoded = report.format_json(summary)

    assert json.loads(encoded) == summary


def test_script_entrypoint_imports_repo_modules_from_scripts_directory():
    run = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--toolsets",
            "coding",
            "--format",
            "json",
            "--top",
            "1",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["toolsets"] == ["coding"]
    assert payload["total_tools"] > 0


def test_script_entrypoint_ignores_worker_kanban_env_by_default():
    env = os.environ.copy()
    env["HERMES_KANBAN_TASK"] = "t_testworker"
    env["HERMES_KANBAN_RUN_ID"] = "123"
    run = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--toolsets",
            "hermes-cli",
            "--format",
            "json",
            "--top",
            "50",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert "kanban" not in payload["by_toolset"]


def test_script_entrypoint_can_include_worker_kanban_env_when_requested():
    env = os.environ.copy()
    env["HERMES_KANBAN_TASK"] = "t_testworker"
    env["HERMES_KANBAN_RUN_ID"] = "123"
    run = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--toolsets",
            "hermes-cli",
            "--include-worker-kanban",
            "--format",
            "json",
            "--top",
            "50",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(run.stdout)
    assert payload["by_toolset"]["kanban"]["tool_count"] > 0
