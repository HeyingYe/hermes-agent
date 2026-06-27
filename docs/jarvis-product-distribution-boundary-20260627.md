# Jarvis Product Distribution Boundary and Migration Plan (2026-06-27)

## 0. Purpose

This document turns the architecture principle in `SELF_ARCHITECTURE.md` into an executable boundary for Jarvis-on-Hermes work.

Goal:

> Keep Hermes as the upstream-synced engine, package Jarvis as a versioned product distribution, and preserve each user's runtime overlay.

This is a design and migration checklist, not a task log. Code remains the immediate source of truth; update this document when distribution ownership or migration rules change.

## 1. Current code-backed state

Verified anchors in the local checkout:

- Engine architecture source: `SELF_ARCHITECTURE.md`.
- Local audit note: `docs/jarvis-hermes-architecture-optimization-20260627.md`.
- Gateway optional-service seam: `gateway/background_services.py`.
- Built-in Kanban compatibility registration: `gateway/kanban_watchers.py::register_kanban_gateway_background_services`.
- Background-process lifecycle seam: `tools/process_registry.py::{BackgroundProcessEvent,register_background_process_subscriber}`.
- Built-in Kanban background-process bridge: `tools/kanban_background_bridge.py`.
- Existing profile distribution manifest support: `hermes_cli/profiles.py::_read_distribution_meta` and user docs `website/docs/reference/profile-commands.md`.
- Existing Dashboard plugin manifest example: `plugins/kanban/dashboard/manifest.json`.
- Feishu/Lark platform code is plugin-owned under `plugins/platforms/feishu/`, not `gateway/platforms/feishu_*`.

Current compatibility facts:

- Generic Hermes defaults are neutral for Kanban gateway dispatch and background-process tracking.
- `kanban.dispatch_in_gateway` and `kanban.track_background_processes` are explicit opt-ins in config.
- Dispatcher-spawned workers can still receive `kanban_*` lifecycle tools through the explicit worker path.
- Built-in Kanban watcher/bridge modules are still compatibility bridges; they are not yet a separately versioned Jarvis distribution package.

## 2. Ownership model

### 2.1 Hermes Engine

Owns stable generic runtime primitives:

- Agent loop, turn lifecycle, model routing, tool registry and dispatch.
- Gateway platform framework, optional background service registry, cron framework.
- Profile/config/session/memory primitives.
- Plugin/MCP/skill loading surfaces.
- Permission, approval, and security boundaries.

Engine rules:

- Defaults are generic and safe for non-Jarvis users.
- Product-layer services are opt-in and best-effort.
- Engine code may expose generic hooks only when there is a concrete consumer and behavior-contract tests.
- Engine code must not hardcode Boss/user/company workflow, Feishu group routing, private chat IDs, or Jarvis-specific defaults.

### 2.2 Jarvis Product Distribution

Owns productized Jarvis capability that should update together:

- Official Jarvis plugins and Dashboard extensions.
- Kanban-first orchestration modules and worker launchers.
- Jarvis profile templates such as `jarviscode`, `jarvisresearch`, `jarvisreview`.
- Cron templates, skills, and operating playbooks.
- Health checks and migration scripts.
- Compatibility adapters for engine hooks during staged migrations.

Distribution rules:

- Versioned independently from Hermes upstream.
- Installable/updateable/rollbackable without editing Hermes engine files.
- Ships manifests declaring owned files, required Hermes version, config migrations, and rollback notes.
- May enable Jarvis defaults in Jarvis profiles, but must not change generic Hermes defaults.

### 2.3 User Runtime Overlay

Owns user/company-specific local state:

- Secrets, OAuth tokens, API keys, `.env`, auth stores.
- Private profiles, profile-local `config.yaml` overrides, memory/session DBs.
- Private skills, scripts, cron jobs, allowlists, routing policy, business connectors.
- Workspace-specific Feishu/Lark chat IDs and group-push rules.

Overlay rules:

- Jarvis updates must not overwrite user-owned files.
- Migrations should lint/adapt and produce patch suggestions on conflict.
- Any write to external messaging channels, group memberships, permissions, or secrets requires explicit preview/confirmation.

### 2.4 Update and Migration Plane

Owns compatibility and rollout:

- Tracks Hermes engine upstream commit/version separately from Jarvis distribution version.
- Tracks user overlay schema compatibility separately from product package version.
- Runs preflight checks before migration.
- Writes a rollback bundle or gives exact rollback commands before mutating owned files.

## 3. Jarvis distribution manifest shape

Use existing profile distribution support as the near-term package surface. A Jarvis distribution repository should include `distribution.yaml` plus optional component manifests.

Minimal `distribution.yaml`:

```yaml
name: jarvis
version: 0.1.0
hermes_requires: ">=0.17.0"
description: "Jarvis product layer for Hermes"
distribution_owned:
  - SOUL.md
  - profile.yaml
  - skills/jarvis/
  - cron/templates/
  - scripts/jarvis_*.sh
  - plugins/jarvis/
  - plugins/kanban/
  - dashboard/
  - migrations/
components:
  profiles:
    - jarviscode
    - jarvisresearch
    - jarvisreview
  dashboard_plugins:
    - kanban
  gateway_services:
    - kanban-notifier
    - kanban-dispatcher
  process_subscribers:
    - kanban-background-bridge
migrations:
  preflight: migrations/preflight.py
  apply: migrations/apply.py
  rollback: migrations/rollback.py
```

Interpretation:

- `distribution_owned` is the overwrite boundary during update.
- Files outside `distribution_owned` are user overlay unless a migration explicitly asks for confirmation.
- `components` is an inventory for health checks and UI display; it is not a substitute for config gates.
- `migrations` scripts must be idempotent and HERMES_HOME/profile-aware.

## 4. Migration roadmap

### Phase A — lock the architecture source

Status: complete locally.

- Add `SELF_ARCHITECTURE.md` as repo-local architecture source.
- Link AGENTS/audit docs to it.
- Keep Lark/Feishu docs as collaboration mirrors, not the only source of truth.

Acceptance:

- Future Hermes/Jarvis code tasks can read a local architecture map before editing.
- Architecture changes update `SELF_ARCHITECTURE.md` in the same change.

### Phase B — keep compatibility bridges, but name them honestly

Status: current state.

- `gateway/background_services.py` is the generic service lifecycle seam.
- `gateway/kanban_watchers.py` remains a built-in compatibility bridge behind `kanban.dispatch_in_gateway`.
- `tools/process_registry.py` is the generic process lifecycle event seam.
- `tools/kanban_background_bridge.py` remains a built-in compatibility subscriber behind `kanban.track_background_processes` or internal env shim.

Acceptance:

- Generic Hermes startup does not import/start Kanban services by default.
- Explicit Jarvis/Kanban opt-in still works.
- Subscriber/service failures are logged and best-effort.

### Phase C — move registration entrypoints into Jarvis distribution

Target change:

- Keep engine hook APIs stable:
  - `register_gateway_background_service(name, factory)`.
  - `register_background_process_subscriber(subscriber)`.
- Move Kanban service/subscriber registration into a Jarvis-owned module, for example:
  - `plugins/jarvis/kanban_gateway_services.py`.
  - `plugins/jarvis/kanban_process_bridge.py`.
- Engine discovery should load distribution/plugin registrations through generic plugin discovery, not by importing Kanban modules directly.

Acceptance:

- Removing Jarvis distribution files leaves generic Hermes healthy.
- Installing Jarvis distribution registers the same services through plugin discovery.
- Old configs can use a compatibility alias for one release, with a migration warning.

### Phase D — package Jarvis profiles and defaults

Target change:

- Move Jarvis-specific profile roles, toolset defaults, cron templates, and worker launchers into the Jarvis distribution repo/package.
- Generic Hermes `DEFAULT_CONFIG` remains neutral.
- Jarvis profiles opt into Jarvis behavior explicitly.

Acceptance:

- `default` profile can stay a user-facing main session.
- `jarviscode`, `jarvisresearch`, `jarvisreview` are owned by Jarvis distribution, but user edits are preserved unless paths are declared distribution-owned.
- Profile update shows what will be overwritten before applying.

### Phase E — split migration and rollback from runtime

Target change:

- Add Jarvis preflight checks:
  - Hermes engine commit/version compatibility.
  - Distribution version compatibility.
  - User overlay conflict scan.
  - Required env/secrets presence without printing secrets.
  - Gateway/cron active-state warning without restarting anything by default.
- Add rollback artifacts before mutations.

Acceptance:

- A failed Jarvis update can be rolled back to previous distribution-owned files.
- User overlay files are unchanged or require explicit confirmation.
- Runtime activation such as gateway restart remains a separate approved step.

## 5. File ownership table

### Engine-owned

- `run_agent.py`, `agent/`, `model_tools.py`, `toolsets.py`.
- `gateway/run.py`, `gateway/background_services.py`, `gateway/platforms/base.py`, platform framework surfaces.
- `cron/`, `hermes_state.py`, `hermes_constants.py`, `hermes_cli/config.py`.
- Generic profile distribution mechanics in `hermes_cli/profiles.py`.

### Jarvis-distribution-owned target

- Jarvis-specific plugin modules and dashboard extensions.
- Kanban gateway service registration and Kanban process bridge registration after Phase C.
- Jarvis profile templates and worker scripts.
- Jarvis skills and cron templates.
- Jarvis health checks and migration scripts.

### User-overlay-owned

- `$HERMES_HOME/config.yaml` local overrides unless explicitly cloned into a distribution-owned profile template.
- `$HERMES_HOME/.env`, `auth.json`, memories, sessions, private cron jobs, private skills, private scripts.
- Workspace-specific chat IDs, allowlists, routing policy, and business connectors.

## 6. Compatibility and rollback rules

- Compatibility bridge code may stay temporarily in engine checkout, but it must be gated, best-effort, and documented as a bridge.
- New installs should prefer the Jarvis distribution path once it exists.
- Existing installs should get a migration warning before bridge removal.
- Rollback must restore distribution-owned files only; it must not overwrite overlay state.
- Gateway restart or service activation is not part of file migration unless the operator explicitly approves it.

## 7. Test matrix for boundary changes

For every Phase C/D/E change, run focused tests plus a combined cross-regression set:

- Generic default-off behavior:
  - Gateway starts without importing/starting Jarvis services.
  - Background process lifecycle works with no Kanban card creation.
  - Core toolsets do not expose Jarvis/Kanban lifecycle tools by default.
- Explicit opt-in behavior:
  - Jarvis/Kanban service registration works through the plugin/distribution entrypoint.
  - Dispatcher worker context still receives lifecycle tools.
  - Subscriber/service errors do not break core lifecycle.
- Migration behavior:
  - Preflight detects version mismatch and overlay conflicts.
  - Apply modifies only distribution-owned files.
  - Rollback restores prior distribution-owned files.
- Platform cross-regression:
  - Feishu plugin imports use `plugins.platforms.feishu.*` paths.
  - Existing gateway delivery/session behavior remains compatible.

## 8. Open follow-up tasks

- Implement a Jarvis distribution package/repo using `distribution.yaml` and the manifest shape above.
- Add a generic plugin-discovery registration path for gateway background services if the current plugin surface cannot register them cleanly.
- Move Kanban gateway service registration and background-process bridge registration into the Jarvis distribution module.
- Add a migration preflight script that prints engine/distribution/overlay versions and conflict list.
- Add an architecture guard command or test that scans for product terms/default-on side effects in engine-owned files.
