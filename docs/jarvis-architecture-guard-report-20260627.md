# Jarvis Architecture Guard Report (2026-06-27)

## Scope

This report records the first repo-local architecture-guard run after adding:

- `scripts/jarvis_architecture_guard.py`
- `scripts/tool_schema_cost_report.py`
- `tests/test_jarvis_architecture_guard.py`
- `tests/test_tool_schema_cost_report.py`

The goal is to make the Hermes/Jarvis architecture rules executable without adding any model-facing tools.

## Architecture guard result

Command:

```bash
python scripts/jarvis_architecture_guard.py --root . --format text
```

Observed result:

```text
Jarvis architecture guard: errors=0 warnings=8
```

Warnings by code:

- `internal-env-compat-shim`: 8

Interpretation:

- There are no blocking architecture errors in the current branch.
- The 8 warnings are the known Kanban compatibility env shims:
  - `HERMES_KANBAN_DISPATCH_IN_GATEWAY`
  - `HERMES_KANBAN_TRACK_BACKGROUND`
- These shims are acceptable only as internal compatibility bridges during the P1 migration. User-facing non-secret behavior must remain in `config.yaml` / Jarvis distribution manifests.

## Guarded invariants

The script currently fails on:

- `kanban_*` tools in shared `_HERMES_CORE_TOOLS`.
- `kanban.dispatch_in_gateway` or `kanban.track_background_processes` defaulting to `True` in generic `DEFAULT_CONFIG`.
- Hardcoded stale per-user `.hermes` absolute paths.

The script warns on:

- `Jarvis Dashboard` product wording inside engine-owned files.
- Internal Kanban compatibility env shims that must not become user-facing config.

## Tool schema cost result

Command:

```bash
python scripts/tool_schema_cost_report.py --toolsets hermes-cli --format markdown --top 12 --output docs/tool-schema-cost-report-20260627.md
```

Primary evidence is in:

- `docs/tool-schema-cost-report-20260627.md`

Observed headline metrics:

- `hermes-cli`: 29 tools, 57,478 schema chars, estimated ~14,370 tokens.
- `coding`: 26 tools, 46,442 schema chars, estimated ~11,611 tokens.
- `hermes-feishu`: 34 tools, 60,479 schema chars, estimated ~15,120 tokens.

Largest individual schema contributors observed in `hermes-cli`:

- `cronjob`: 7,803 chars / ~1,951 tokens.
- `delegate_task`: 7,707 chars / ~1,927 tokens.
- `session_search`: 5,787 chars / ~1,447 tokens.
- `terminal`: 5,512 chars / ~1,378 tokens.
- `skill_manage`: 4,037 chars / ~1,010 tokens.

Interpretation:

- The biggest cost is not from Kanban anymore; it is from legitimate core capabilities with long schemas.
- Do not remove these tools blindly. Next optimization should be measurement-driven and staged.
- The likely safe path is shorter descriptions / schema factoring first, then progressive disclosure for niche or plugin/MCP surfaces, then profile/posture defaults only after shadow measurement.

## Rollout recommendation

1. Keep `jarvis_architecture_guard.py` as a local CI/pre-commit-style check for Jarvis/Hermes branches.
2. Run it in non-strict mode now; treat warnings as migration backlog.
3. After Kanban watcher/bridge registration moves into Jarvis distribution, turn the env-shim warnings into errors or remove the shim scanner.
4. Run `tool_schema_cost_report.py` before and after any toolset/core-schema changes.
5. For schema reduction, first target wording and progressive disclosure, not capability deletion.
6. Any default toolset/posture change must run CLI, gateway, cron, profile, and Feishu-focused regression tests.
