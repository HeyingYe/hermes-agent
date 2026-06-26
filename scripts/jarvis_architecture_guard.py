#!/usr/bin/env python3
"""Jarvis architecture guard for Hermes fork changes.

This is a lightweight pre-handoff/pre-commit check. It does not decide whether a
change is correct; it highlights changes that require an explicit architecture
justification under the Jarvis/Hermes upstream-sync guard.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

CORE_RISK_PATHS = {
    "agent/conversation_loop.py",
    "run_agent.py",
    "agent/system_prompt.py",
    "agent/turn_finalizer.py",
    "model_tools.py",
    "toolsets.py",
    "gateway/run.py",
    "hermes_cli/config.py",
    "cron/scheduler.py",
    "hermes_state.py",
}

EDGE_PREFIXES = (
    "plugins/",
    "skills/",
    "optional-skills/",
    "scripts/",
    "docs/",
    "tests/",
    "gateway/platforms/",
)

ALLOWED_INTERNAL_ENV = {
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_BOARD",
}

OLD_HOME_PATTERNS = (
    r"/Users/heyingye/\.hermes",
    r"~/.hermes",
    r"Path\.home\(\)\s*/\s*[\"']\.hermes[\"']",
    r"expanduser\([\"']~/.hermes",
)


def run(cmd: list[str], cwd: Path) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed {cmd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def changed_files(repo: Path) -> list[str]:
    out = run(["git", "status", "--porcelain", "--untracked-files=all"], repo)
    files: list[str] = []
    for line in out.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return sorted(set(files))


def diff_text(repo: Path) -> str:
    parts: list[str] = []
    for cmd in (["git", "diff", "--cached", "--unified=0"], ["git", "diff", "--unified=0"]):
        try:
            parts.append(run(cmd, repo))
        except Exception:
            pass
    # Include untracked text files lightly.
    for f in changed_files(repo):
        p = repo / f
        if p.exists() and p.is_file() and f.split("/")[-1].count(".") and p.stat().st_size < 500_000:
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            if run(["git", "ls-files", "--others", "--exclude-standard", "--", f], repo).strip():
                parts.append(f"diff --git a/{f} b/{f}\n+++ b/{f}\n" + "\n".join("+" + line for line in text.splitlines()))
    return "\n".join(parts)


def added_lines_by_file(diff: str) -> dict[str, list[str]]:
    current: str | None = None
    out: dict[str, list[str]] = {}
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/") :]
            out.setdefault(current, [])
            continue
        if line.startswith("diff --git "):
            current = None
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            out.setdefault(current, []).append(line[1:])
    return out


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    files = changed_files(repo)
    diff = diff_text(repo)
    added_by_file = added_lines_by_file(diff)

    core_hits = [f for f in files if f in CORE_RISK_PATHS]
    toolset_hit = "toolsets.py" in files
    scan_added = "\n".join(
        "\n".join(lines)
        for path, lines in added_by_file.items()
        if path != "scripts/jarvis_architecture_guard.py"
        and path.endswith((".py", ".sh", ".bash", ".yaml", ".yml", ".toml", ".json"))
    )
    old_home_hits = []
    for pat in OLD_HOME_PATTERNS:
        if re.search(pat, scan_added):
            old_home_hits.append(pat)
    env_vars = sorted(set(re.findall(r"HERMES_[A-Z0-9_]+", scan_added)) - ALLOWED_INTERNAL_ENV)
    non_edge = [f for f in files if not f.startswith(EDGE_PREFIXES) and f not in {"AGENTS.md", "SELF_ARCHITECTURE.md"}]

    print("Jarvis/Hermes Architecture Guard")
    print("================================")
    print(f"Repo: {repo}")
    print(f"Changed files: {len(files)}")
    if files:
        for f in files:
            print(f"- {f}")
    print()

    warnings: list[str] = []
    if core_hits:
        warnings.append("CORE_RISK_PATHS touched: " + ", ".join(core_hits))
    if toolset_hit:
        warnings.append("toolsets.py changed: new/default tool exposure requires Footprint Ladder justification")
    if env_vars:
        warnings.append("New/changed HERMES_* vars need config.yaml-first justification: " + ", ".join(env_vars))
    if old_home_hits:
        warnings.append("Possible hardcoded old Hermes home path found in added lines")
    if non_edge:
        warnings.append("Non-edge files changed; classify each as upstreamable-core-fix vs Jarvis-local: " + ", ".join(non_edge[:20]))

    if warnings:
        print("Architecture warnings requiring explicit justification:")
        for w in warnings:
            print(f"! {w}")
        print()
        print("Required justification before handoff/commit:")
        print("1. Classification: upstreamable-core-fix / Jarvis-local / deprecated / local-ops")
        print("2. Why edge implementation is insufficient if core was touched")
        print("3. Config gate / rollback path")
        print("4. Tests run with real output")
        print("5. SELF_ARCHITECTURE.md updated if runtime architecture changed")
        return 1

    print("No architecture guard warnings. Continue with normal tests and review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
