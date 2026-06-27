#!/usr/bin/env python3
"""Jarvis/Hermes architecture guard.

This is a lightweight repo-local scanner for architecture invariants that are
cheap to verify statically. It intentionally lives in scripts/ rather than as a
model-facing tool: agents and maintainers can run it through terminal without
expanding the core tool schema.
"""

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ArchitectureIssue:
    severity: str  # "error" or "warning"
    code: str
    path: Path
    line: int
    message: str
    match: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["path"] = str(self.path)
        return data


_CORE_TOOLSET_RE = re.compile(
    r"_HERMES_CORE_TOOLS\s*=\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)
_STRING_LITERAL_RE = re.compile(r"['\"](?P<value>kanban_[A-Za-z0-9_]+)['\"]")
_DEFAULT_TRUE_RE = re.compile(
    r"['\"](?P<key>dispatch_in_gateway|track_background_processes)['\"]\s*:\s*True\b"
)
_STALE_USER_HOME_RE = re.compile(r"/Users/heyingye/\.hermes")
_INTERNAL_ENV_RE = re.compile(
    r"\bHERMES_KANBAN_(?:TRACK_BACKGROUND|DISPATCH_IN_GATEWAY)\b"
)
_PRODUCT_TERM_RE = re.compile(r"\b(?:Jarvis|Kanban|Dashboard)\b")
_HERMES_ENV_RE = re.compile(r"\b(HERMES_[A-Z0-9_]+)\b")
_SECRET_ENV_FRAGMENT_RE = re.compile(
    r"(?:API_)?KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL|AUTH|OAUTH|JWT|BEARER",
    re.IGNORECASE,
)
_ALLOWED_NON_BEHAVIOR_ENV_VARS = {
    "HERMES_HOME",
    "HERMES_UID",
    "HERMES_GID",
    "HERMES_ARGS",
    "HERMES_MODEL",
}
_ALLOWED_NON_BEHAVIOR_ENV_SUFFIXES = ("_PATH", "_DIR", "_DIRECTORY", "_URL", "_URI")
_USER_FACING_ENV_SKIP_DIRS = {
    "apps",
    "docker",
    "optional-skills",
    "plugins",
    "skills",
    "website",
}
_USER_FACING_CONFIG_HINT_RE = re.compile(
    r"\b(?:export|set)\s+(?:HERMES_[A-Z0-9_]+)\s*=",
    re.IGNORECASE,
)
_TOOL_SCHEMA_REPORT_RE = re.compile(r"tool-schema-cost-report.*\.md$")
_SCHEMA_METRIC_RE = re.compile(r"^Estimated tokens:\s*(?P<tokens>[\d,]+)\s*$", re.MULTILINE)
_TOOL_SCHEMA_TOKEN_WARNING_THRESHOLD = 20_000

_ENGINE_PRODUCT_TERM_FILES = (
    "toolsets.py",
    "model_tools.py",
    "run_agent.py",
    "gateway/run.py",
    "gateway/background_services.py",
    "tools/process_registry.py",
    "hermes_cli/config.py",
)
_INTERNAL_ENV_FILES = (
    "gateway/background_services.py",
    "gateway/kanban_watchers.py",
    "tools/process_registry.py",
)
_TEXT_SCAN_SUFFIXES = {".py", ".md", ".sh", ".yaml", ".yml", ".json", ".toml"}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _rel(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _TEXT_SCAN_SUFFIXES:
            yield path


def _scan_core_tool_product_leaks(root: Path) -> list[ArchitectureIssue]:
    path = root / "toolsets.py"
    text = _read_text(path)
    if not text:
        return []
    issues: list[ArchitectureIssue] = []
    for block in _CORE_TOOLSET_RE.finditer(text):
        body = block.group("body")
        for match in _STRING_LITERAL_RE.finditer(body):
            value = match.group("value")
            offset = block.start("body") + match.start("value")
            issues.append(
                ArchitectureIssue(
                    severity="error",
                    code="core-tool-product-tool",
                    path=_rel(root, path),
                    line=_line_for_offset(text, offset),
                    message=(
                        f"Product-layer Kanban tool {value!r} is listed in "
                        "_HERMES_CORE_TOOLS. Keep Kanban in the explicit "
                        "kanban toolset / worker path, not generic platform core."
                    ),
                    match=value,
                )
            )
    return issues


def _scan_default_on_kanban_side_effects(root: Path) -> list[ArchitectureIssue]:
    path = root / "hermes_cli" / "config.py"
    text = _read_text(path)
    issues: list[ArchitectureIssue] = []
    for match in _DEFAULT_TRUE_RE.finditer(text):
        key = match.group("key")
        issues.append(
            ArchitectureIssue(
                severity="error",
                code="default-on-kanban-side-effect",
                path=_rel(root, path),
                line=_line_for_offset(text, match.start()),
                message=(
                    f"kanban.{key} defaults to True in generic config. "
                    "Product-layer gateway/process side effects must default off "
                    "and be enabled by Jarvis distribution/profile config."
                ),
                match=match.group(0),
            )
        )
    return issues


def _scan_stale_user_home(root: Path) -> list[ArchitectureIssue]:
    issues: list[ArchitectureIssue] = []
    for path in _iter_text_files(root):
        text = _read_text(path)
        for match in _STALE_USER_HOME_RE.finditer(text):
            issues.append(
                ArchitectureIssue(
                    severity="error",
                    code="stale-hermes-home-hardcode",
                    path=_rel(root, path),
                    line=_line_for_offset(text, match.start()),
                    message=(
                        "Found hardcoded old user Hermes home. Use get_hermes_home(), "
                        "HERMES_HOME, or profile-aware path helpers instead."
                    ),
                    match=match.group(0),
                )
            )
    return issues


def _scan_product_terms(root: Path) -> list[ArchitectureIssue]:
    issues: list[ArchitectureIssue] = []
    for rel_path in _ENGINE_PRODUCT_TERM_FILES:
        path = root / rel_path
        text = _read_text(path)
        for match in _PRODUCT_TERM_RE.finditer(text):
            issues.append(
                ArchitectureIssue(
                    severity="warning",
                    code="product-term-in-engine-file",
                    path=Path(rel_path),
                    line=_line_for_offset(text, match.start()),
                    message=(
                        "Engine-owned file mentions a Jarvis product surface. "
                        "Keep wording generic or move behavior to the distribution layer."
                    ),
                    match=match.group(0),
                )
            )
    return issues


def _is_secret_env_var(name: str) -> bool:
    return bool(_SECRET_ENV_FRAGMENT_RE.search(name))


def _is_allowed_non_behavior_env_var(name: str) -> bool:
    return name in _ALLOWED_NON_BEHAVIOR_ENV_VARS or name.endswith(_ALLOWED_NON_BEHAVIOR_ENV_SUFFIXES)


def _scan_user_facing_non_secret_hermes_env(root: Path) -> list[ArchitectureIssue]:
    issues: list[ArchitectureIssue] = []
    for path in _iter_text_files(root):
        rel_path = _rel(root, path)
        # Python files can legitimately contain internal env bridges; this guard
        # is aimed at primary repo docs/scripts that teach behavior via HERMES_*.
        if path.suffix == ".py" or any(part in _USER_FACING_ENV_SKIP_DIRS for part in rel_path.parts):
            continue
        text = _read_text(path)
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not _USER_FACING_CONFIG_HINT_RE.search(line):
                continue
            for match in _HERMES_ENV_RE.finditer(line):
                env_name = match.group(1)
                if _is_secret_env_var(env_name) or _is_allowed_non_behavior_env_var(env_name):
                    continue
                issues.append(
                    ArchitectureIssue(
                        severity="warning",
                        code="non-secret-hermes-env-behavior-config",
                        path=rel_path,
                        line=line_no,
                        message=(
                            f"User-facing non-secret Hermes behavior setting {env_name!r} found. "
                            "Behavior config belongs in config.yaml, setup/profile manifests, "
                            "or an internal bridge rather than user .env/shell instructions."
                        ),
                        match=env_name,
                    )
                )
    return issues


def _scan_tool_schema_budget_reports(root: Path) -> list[ArchitectureIssue]:
    issues: list[ArchitectureIssue] = []
    for path in _iter_text_files(root):
        rel_path = _rel(root, path)
        if not _TOOL_SCHEMA_REPORT_RE.search(str(rel_path)):
            continue
        text = _read_text(path)
        match = _SCHEMA_METRIC_RE.search(text)
        if not match:
            continue
        tokens = int(match.group("tokens").replace(",", ""))
        if tokens <= _TOOL_SCHEMA_TOKEN_WARNING_THRESHOLD:
            continue
        issues.append(
            ArchitectureIssue(
                severity="warning",
                code="tool-schema-budget-regression",
                path=rel_path,
                line=_line_for_offset(text, match.start("tokens")),
                message=(
                    f"Tool schema report estimates {tokens} tokens, above the "
                    f"{_TOOL_SCHEMA_TOKEN_WARNING_THRESHOLD} token review threshold. "
                    "Investigate schema growth before adding model-facing tools."
                ),
                match=str(tokens),
            )
        )
    return issues


def _scan_internal_env_shims(root: Path) -> list[ArchitectureIssue]:
    issues: list[ArchitectureIssue] = []
    for rel_path in _INTERNAL_ENV_FILES:
        path = root / rel_path
        text = _read_text(path)
        for match in _INTERNAL_ENV_RE.finditer(text):
            issues.append(
                ArchitectureIssue(
                    severity="warning",
                    code="internal-env-compat-shim",
                    path=Path(rel_path),
                    line=_line_for_offset(text, match.start()),
                    message=(
                        "Kanban compatibility env shim found. Keep this internal; "
                        "user-facing non-secret behavior belongs in config.yaml or "
                        "Jarvis distribution/profile manifests."
                    ),
                    match=match.group(0),
                )
            )
    return issues


def scan_repository(root: str | Path = ".") -> list[ArchitectureIssue]:
    """Return architecture issues for *root*.

    Default CLI exit status treats only ``severity == 'error'`` as failing.
    Warnings are reported for migration follow-up without breaking current
    compatibility bridges.
    """
    root = Path(root).resolve()
    issues: list[ArchitectureIssue] = []
    issues.extend(_scan_core_tool_product_leaks(root))
    issues.extend(_scan_default_on_kanban_side_effects(root))
    issues.extend(_scan_stale_user_home(root))
    issues.extend(_scan_product_terms(root))
    issues.extend(_scan_user_facing_non_secret_hermes_env(root))
    issues.extend(_scan_tool_schema_budget_reports(root))
    issues.extend(_scan_internal_env_shims(root))
    return sorted(issues, key=lambda i: (i.severity != "error", str(i.path), i.line, i.code))


def issue_counts(issues: Sequence[ArchitectureIssue]) -> tuple[int, int]:
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")
    return errors, warnings


def format_text(issues: Sequence[ArchitectureIssue]) -> str:
    errors, warnings = issue_counts(issues)
    lines = [f"Jarvis architecture guard: errors={errors} warnings={warnings}"]
    if not issues:
        lines.append("OK: no architecture issues found.")
        return "\n".join(lines)
    for issue in issues:
        loc = f"{issue.path}:{issue.line}"
        lines.append(f"[{issue.severity.upper()}] {issue.code} {loc}: {issue.message}")
        if issue.match:
            lines.append(f"  match: {issue.match}")
    return "\n".join(lines)


def format_json(issues: Sequence[ArchitectureIssue]) -> str:
    errors, warnings = issue_counts(issues)
    payload = {
        "errors": errors,
        "warnings": warnings,
        "issues": [issue.to_dict() for issue in issues],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan (default: current directory)")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--strict-warnings",
        action="store_true",
        help="Exit non-zero when warnings are present as well as errors",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    issues = scan_repository(args.root)
    if args.format == "json":
        print(format_json(issues))
    else:
        print(format_text(issues))
    errors, warnings = issue_counts(issues)
    if errors or (args.strict_warnings and warnings):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
