#!/usr/bin/env python3
"""Report model-facing tool schema size by tool and toolset.

This script is intentionally a terminal/CI helper instead of a model-facing
Hermes tool. It helps maintainers decide what belongs in core, opt-in toolsets,
plugins, MCP, or tool_search without increasing the default tool schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


CORE_TOOLSETS = {
    "web",
    "search",
    "terminal",
    "file",
    "skills",
    "browser",
    "cronjob",
    "todo",
    "memory",
    "session_search",
    "clarify",
    "code_execution",
    "delegation",
    "vision",
    "image_gen",
    "tts",
    "computer_use",
    "homeassistant",
}


def schema_json(tool_def: Mapping[str, Any]) -> str:
    """Return canonical compact JSON for one OpenAI-format tool definition."""
    return json.dumps(tool_def, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_size_chars(tool_def: Mapping[str, Any]) -> int:
    return len(schema_json(tool_def))


def estimate_tokens(chars: int) -> int:
    """Cheap token estimate for schema budgeting.

    Tool schemas are mostly ASCII JSON; 4 chars/token is a conservative enough
    planning heuristic for ranking and trend tracking. Use provider tokenizer
    only for final cost accounting.
    """
    return (chars + 3) // 4


def _tool_name(tool_def: Mapping[str, Any]) -> str:
    fn = tool_def.get("function") if isinstance(tool_def, Mapping) else None
    if isinstance(fn, Mapping):
        return str(fn.get("name") or "<unknown>")
    return "<unknown>"


def _recommendations(by_toolset: Mapping[str, Mapping[str, int]], top_tools: Sequence[Mapping[str, Any]]) -> list[str]:
    recommendations: list[str] = []
    non_core_heavy = [
        (name, stats)
        for name, stats in by_toolset.items()
        if name not in CORE_TOOLSETS and stats.get("schema_chars", 0) >= 1000
    ]
    if non_core_heavy:
        names = ", ".join(name for name, _stats in sorted(non_core_heavy, key=lambda item: item[1]["schema_chars"], reverse=True))
        recommendations.append(
            f"Consider deferring large non-core toolsets behind tool_search, plugin opt-in, or MCP catalog entry: {names}."
        )
    large_tools = [tool for tool in top_tools if int(tool.get("schema_chars", 0)) >= 2000]
    if large_tools:
        names = ", ".join(str(tool["name"]) for tool in large_tools[:5])
        recommendations.append(
            f"Review very large individual schemas for shorter descriptions or progressive disclosure: {names}."
        )
    if not recommendations:
        recommendations.append(
            "No immediate schema-cost outliers detected at the configured thresholds; keep measuring before moving tools."
        )
    return recommendations


def summarize_tool_schemas(
    tool_defs: Sequence[Mapping[str, Any]],
    *,
    toolset_map: Mapping[str, str] | None = None,
    enabled_toolsets: Sequence[str] | None = None,
    top_n: int = 15,
) -> dict:
    """Summarize schema cost for already-resolved tool definitions."""
    toolset_map = toolset_map or {}
    per_tool: list[dict] = []
    by_toolset: dict[str, dict[str, int]] = defaultdict(lambda: {"tool_count": 0, "schema_chars": 0, "estimated_tokens": 0})
    total_chars = 0
    for tool_def in tool_defs:
        name = _tool_name(tool_def)
        chars = schema_size_chars(tool_def)
        tokens = estimate_tokens(chars)
        toolset = toolset_map.get(name, "<unknown>")
        total_chars += chars
        by_toolset[toolset]["tool_count"] += 1
        by_toolset[toolset]["schema_chars"] += chars
        by_toolset[toolset]["estimated_tokens"] += tokens
        per_tool.append({
            "name": name,
            "toolset": toolset,
            "schema_chars": chars,
            "estimated_tokens": tokens,
        })
    per_tool.sort(key=lambda item: (-item["schema_chars"], item["name"]))
    by_toolset_sorted = {
        name: stats
        for name, stats in sorted(
            by_toolset.items(),
            key=lambda item: (-item[1]["schema_chars"], item[0]),
        )
    }
    top_tools = per_tool[: max(0, top_n)]
    summary = {
        "toolsets": list(enabled_toolsets or []),
        "total_tools": len(tool_defs),
        "total_schema_chars": total_chars,
        "estimated_tokens": estimate_tokens(total_chars),
        "by_toolset": by_toolset_sorted,
        "top_tools": top_tools,
    }
    summary["recommendations"] = _recommendations(by_toolset_sorted, top_tools)
    return summary


def collect_tool_schema_report(enabled_toolsets: Sequence[str] | None = None, *, top_n: int = 15) -> dict:
    """Resolve live Hermes tool schemas and summarize their size.

    Uses ``skip_tool_search_assembly=True`` so the report measures the raw tool
    surface before progressive-disclosure deferral.
    """
    from model_tools import TOOL_TO_TOOLSET_MAP, get_tool_definitions

    tool_defs = get_tool_definitions(
        enabled_toolsets=list(enabled_toolsets) if enabled_toolsets else None,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    return summarize_tool_schemas(
        tool_defs,
        toolset_map=TOOL_TO_TOOLSET_MAP,
        enabled_toolsets=list(enabled_toolsets or ["<all>"]),
        top_n=top_n,
    )


def format_json(summary: Mapping[str, Any]) -> str:
    return json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True)


def format_markdown(summary: Mapping[str, Any]) -> str:
    lines = ["# Tool Schema Cost Report", ""]
    toolsets = summary.get("toolsets") or []
    lines.append(f"Toolsets: `{', '.join(toolsets) if toolsets else '<unspecified>'}`")
    lines.append(f"Total tools: {summary.get('total_tools', 0)}")
    lines.append(f"Total schema chars: {summary.get('total_schema_chars', 0)}")
    lines.append(f"Estimated tokens: {summary.get('estimated_tokens', 0)}")
    lines.append("")
    lines.append("## By toolset")
    for name, stats in (summary.get("by_toolset") or {}).items():
        lines.append(
            f"- `{name}`: {stats.get('tool_count', 0)} tools, "
            f"{stats.get('schema_chars', 0)} chars, ~{stats.get('estimated_tokens', 0)} tokens"
        )
    lines.append("")
    lines.append("## Largest tools")
    for tool in summary.get("top_tools") or []:
        lines.append(
            f"- `{tool.get('name')}` (`{tool.get('toolset')}`): "
            f"{tool.get('schema_chars')} chars, ~{tool.get('estimated_tokens')} tokens"
        )
    lines.append("")
    lines.append("## Recommendations")
    for rec in summary.get("recommendations") or []:
        lines.append(f"- {rec}")
    return "\n".join(lines)


def _parse_toolsets(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--toolsets",
        default="hermes-cli",
        help="Comma-separated toolsets to measure. Use 'all' for every enabled toolset path (default: hermes-cli).",
    )
    parser.add_argument("--top", type=int, default=15, help="Number of largest tools to show")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, default=None, help="Optional output file path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    enabled = None if args.toolsets.strip().lower() == "all" else _parse_toolsets(args.toolsets)
    summary = collect_tool_schema_report(enabled, top_n=args.top)
    text = format_json(summary) if args.format == "json" else format_markdown(summary)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
