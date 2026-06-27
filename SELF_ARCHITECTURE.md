# Hermes Self Architecture

This file is the repo-local architecture map for maintainers and AI coding agents working on this checkout. It exists so architecture reviews do not depend only on external chat history or Lark/Feishu documents.

Related mirrors / context:

- Lark Docx architecture map: `GvRydaFa6otejlx1JdMj1a6vpqf`.
- Local audit and optimization note: `docs/jarvis-hermes-architecture-optimization-20260627.md`.
- Jarvis product distribution boundary and migration plan: `docs/jarvis-product-distribution-boundary-20260627.md`.
- Architecture guard and tool schema cost reports: `docs/jarvis-architecture-guard-report-20260627.md`, `docs/tool-schema-cost-report-20260627.md`, `docs/tool-schema-progressive-disclosure-analysis-20260627.md`.
- Contributor guardrails: `AGENTS.md`.

If this file and code disagree, treat the code and tests as the immediate source of truth, then update this file in the same change that changes runtime architecture.

## 1. Product shape

Hermes is a personal AI agent runtime that runs the same agent core across:

- CLI / TUI / desktop sessions.
- Messaging gateway platforms such as Feishu/Lark, Telegram, Discord, Slack, webhooks, and others.
- Cron jobs and durable background workflows.
- Worker and delegated-agent contexts.

The architectural goal is:

> Thin core, rich edges.

The engine should own stable primitives and lifecycle seams. Product-specific behavior should live in plugins, sidecars, profile templates, cron templates, skills, scripts, MCP servers, or distribution overlays.

## 2. Non-negotiable invariants

1. **Per-conversation prompt caching is sacred.** Do not mutate the system prompt, toolset, or cached context shape mid-conversation except through sanctioned compression / reset paths.
2. **Strict message role alternation.** Do not inject synthetic user messages mid-loop. Every tool call must be paired with a tool result.
3. **Core tool schema stays narrow.** New model-facing core tools are expensive because they are sent on every API call. Prefer existing tools, CLI + skill, service-gated tools, plugins, MCP, then new core tools only as a last resort.
4. **Behavior config belongs in `config.yaml`.** `.env` is for secrets. User-facing non-secret behavior should not be introduced as a new `HERMES_*` env var.
5. **Paths are profile-aware.** Use `get_hermes_home()` or equivalent; do not hardcode `~/.hermes` or a user-specific home path.
6. **Profiles stay independent.** Clone/copy at creation is acceptable; live inheritance from default profile is not.
7. **Optional product services must be config-gated and best-effort.** A product service must not prevent generic gateway startup, process tracking, cron, or the core agent loop from functioning.

## 3. Layering model

### 3.1 Hermes Engine

Owns generic runtime primitives:

- Agent loop and turn lifecycle: `run_agent.py`, `agent/conversation_loop.py`, `agent/turn_finalizer.py`.
- Model / provider routing: `agent/*`, provider adapters, auxiliary model helpers.
- Tool discovery and dispatch: `tools/registry.py`, `model_tools.py`, `toolsets.py`.
- Gateway framework: `gateway/run.py`, platform adapters, gateway service lifecycle.
- Cron framework: `cron/jobs.py`, `cron/scheduler.py`.
- Session and state primitives: `hermes_state.py`, profile/config/session stores.
- Plugin / MCP / skill loading surfaces.
- Security and permission boundaries.

Engine defaults must be neutral for a generic Hermes install.

### 3.2 Product distribution layer

A product distribution can provide opinionated behavior by composing engine surfaces:

- Plugins and dashboard extensions.
- Sidecars and background services.
- Profile templates and toolset defaults.
- Cron templates and workflow blueprints.
- Skills and worker launchers.
- Migration / health-check scripts.

For the local Jarvis distribution, Kanban-first workflows, Dashboard views, Claude Code worker packaging, and profile roles belong here unless they are generic enough to be upstreamed.

### 3.3 User runtime overlay

User or enterprise-specific state lives outside the engine:

- Secrets and credentials.
- Private profiles and local routing policy.
- Private skills, scripts, cron jobs, allowlists, business connectors.
- Workspace-specific Lark/Feishu chat IDs or group-push rules.

Product updates must not overwrite this layer. Migrations should lint, adapt schemas, or suggest patches rather than clobbering local state.

### 3.4 Update and migration plane

Track these independently:

- Hermes engine version / upstream sync point.
- Product distribution version.
- User overlay compatibility.

A migration should have a preflight check, explicit rollback strategy, and a list of owned files.

## 4. Core request lifecycle

High-level flow for an interactive or gateway message:

1. Platform / CLI receives user input.
2. Gateway or CLI resolves session identity, profile, config, and enabled toolsets.
3. Agent prompt builder assembles stable system context plus project/user/runtime context.
4. Model call runs with OpenAI-style messages and tool schemas.
5. Tool calls are dispatched through `model_tools.handle_function_call()` and tool registry handlers.
6. Tool results are appended with correct role pairing.
7. Loop continues until a final text response or max-turn / safety stop.
8. Finalizer persists state, delivers response, and records telemetry.

Hot paths must preserve prompt-cache compatibility and role alternation.

## 5. Tool and toolset architecture

Key files:

- `tools/registry.py`: tool registration, schemas, handlers, check functions.
- `model_tools.py`: schema assembly, toolset filtering, dynamic schema edits, dispatch API.
- `toolsets.py`: built-in toolset names and composition.

Rules:

- A registered tool is only model-visible when selected by toolset resolution and allowed by its `check_fn`.
- Platform toolsets should not grow product-specific tools by default.
- Worker-only or product-only tools should be explicit toolsets or plugin tools.
- `tool_search` / progressive disclosure should be preferred for large non-core tool surfaces.

Current local decision:

- `kanban_*` tools are not part of shared `_HERMES_CORE_TOOLS`.
- Dispatcher-spawned workers still receive Kanban lifecycle tools because `model_tools.get_tool_definitions()` appends the explicit `kanban` toolset when `HERMES_KANBAN_TASK` is set.
- Orchestrator profiles can opt into the `kanban` toolset through config.

## 6. Gateway architecture

Key files:

- `gateway/run.py`: gateway runner, platform startup, message dispatch.
- `gateway/background_services.py`: generic optional background service registry.
- `gateway/platforms/*`: platform adapters.
- Product compatibility services such as `gateway/kanban_watchers.py` should be loaded only through config-gated seams.

Rules:

- Generic gateway startup should not hard-import or hard-start product watchers.
- Optional services are registered after their own config gate opts in.
- Service factory errors are best-effort and logged; they must not prevent gateway startup.
- Platform adapters own platform-specific transport details; generic gateway code should not know workspace-specific routing policy.

Current local decision:

- `kanban.dispatch_in_gateway` is explicit opt-in in generic config.
- Kanban watcher / dispatcher registration is currently a built-in compatibility bridge and remains a P1 candidate for product-distribution extraction.

## 7. Background processes and Kanban bridge

Key files:

- `tools/process_registry.py`: generic background process lifecycle registry and events.
- `tools/kanban_background_bridge.py`: optional compatibility subscriber that mirrors background processes to Kanban.

Rules:

- Generic background process tracking must not create product-layer artifacts by default.
- Product subscribers must be opt-in and best-effort.
- Subscriber exceptions must not break process spawn, polling, checkpointing, or completion notification.

Current local decision:

- `kanban.track_background_processes` is default `False`.
- `HERMES_KANBAN_TRACK_BACKGROUND=1` may remain as an internal compatibility shim, but user-facing behavior belongs in config.

## 8. Cron architecture

Key files:

- `cron/jobs.py`, `cron/scheduler.py`.
- `hermes_cli/cron.py` and cron tooling.

Rules:

- Cron runs are durable and self-contained; prompts must include all required context.
- Cron deliveries are framed and should not break gateway session role alternation.
- Cron toolsets must not bypass disabled toolset policy.
- Cron should not recursively schedule cron jobs.

## 9. State, config, and profile layout

Key files:

- `hermes_cli/config.py`: `DEFAULT_CONFIG`, load/save/migration helpers.
- `hermes_constants.py`: profile-aware path helpers.
- `hermes_state.py`: SQLite session store.

Rules:

- `DEFAULT_CONFIG` should have one runtime source for each key. Avoid duplicate dict literals that overwrite earlier defaults.
- Config migrations must preserve user intent and profile isolation.
- Tests should use temp `HERMES_HOME` and not touch the real user home.

Current local decision:

- `DEFAULT_CONFIG['kanban']` explicitly includes `auto_subscribe_on_create=True`, `dispatch_in_gateway=False`, and `track_background_processes=False` in the same final runtime dict.

## 10. Learning loop

Hermes improves through:

- Memory providers and user/profile memory.
- Skills and the skill curator.
- Context compression and context-engine surfaces.
- Delegation / subagents.
- Cron and Kanban durable workflows.

Rules:

- Memories are compact durable facts, not task logs.
- Procedures belong in skills, not memory.
- If a skill is outdated or wrong, patch it during the task that discovers the problem.

## 11. Diagnostics before guessing

Useful commands:

```bash
hermes status --all
hermes doctor
hermes config
hermes tools list
hermes profile list
hermes cron list
hermes kanban list --json
python -m pytest <focused-tests> -q -o 'addopts='
python -m py_compile <changed-python-files>
```

For code changes, capture real verification output before reporting done.

## 12. Core-touching checklist

Before touching these areas, classify the change and document why a plugin/sidecar/profile/cron/skill/script/MCP approach is not enough:

- `agent/conversation_loop.py`
- `run_agent.py`
- `agent/system_prompt.py`
- `agent/turn_finalizer.py`
- `model_tools.py`
- `toolsets.py`
- `gateway/run.py`
- `hermes_cli/config.py`
- `cron/scheduler.py`
- `hermes_state.py`
- default/core tool schemas

For any such change, add behavior-contract tests and verify real execution.

## 13. Updating this file

Update `SELF_ARCHITECTURE.md` when:

- Runtime architecture or ownership boundaries change.
- A product behavior is moved into or out of engine code.
- A default config behavior changes.
- A new extension seam is introduced.
- A recurring architecture pitfall is discovered.

Do not use this file as a task log. Link to task-specific audit notes under `docs/` when detailed evidence is needed.
