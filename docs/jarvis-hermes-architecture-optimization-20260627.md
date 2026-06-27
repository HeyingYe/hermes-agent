# Jarvis/Hermes 架构现状审计与优化方案（2026-06-27）

## 0. 输入与真实检查

- 架构输入：Lark Docx `GvRydaFa6otejlx1JdMj1a6vpqf`，revision `14`。
- 文档主题：Hermes 自架构地图，核心原则是“薄核心，丰富边缘”，并强调 prompt caching、严格 role alternation、工具 schema 成本、profile-aware 路径、gateway/cron/kanban 等运行链路。
- 源码位置：`/Users/heyingye/Jarvis/.hermes/hermes-agent`。
- 当前分支：`feat/feishu-topic-sessions`。
- 代码规模粗测：排除 `.git/node_modules/venv/.venv/dist/build/tmp/logs/data` 等依赖、生成和运行态目录后，纳入统计的源码/文档文件共 `4872` 个、约 `1,845,842` 行；其中 Python `2389` 文件 / `1,136,529` 行，Markdown `1366` 文件 / `434,831` 行，TypeScript `915` 文件 / `212,627` 行。
- 运行状态检查：`hermes status --all` 显示 Gateway running（launchd，检查时 PID `50758`），Feishu configured（home `oc_822b59dd8fda0cf547702d6a758fa70c`）；`gateway.log` 显示 11:34 后 gateway 重启成功、Feishu websocket connected、Kanban dispatcher embedded in gateway。当前任务未修改生产配置。
- 现有架构文档缺口：仓库内未发现 `SELF_ARCHITECTURE.md`，但架构守卫 skill 要求 Hermes/Jarvis 代码任务先读该文件；当前实际架构地图存在于 Lark 文档和 AGENTS/skills 中。

## 1. 当前代码状态结论

### 1.1 已经做对的方向

- `hermes_cli/config.py` 中 `kanban.dispatch_in_gateway` 已是 default-off，并明确 generic Hermes gateway 不应产生 Kanban DB polling 或 worker-spawn side effect。
- `hermes_cli/config.py` 中 `kanban.track_background_processes` 已是 default-off，避免普通 `terminal(background=true)` 自动写 Kanban。
- `gateway/background_services.py` 已提供 optional background service registry，产品层服务仅在 config/env gate opt-in 后才导入。
- `gateway/run.py` 当前只调用通用 optional service registration/start seam，不再直接硬继承 Kanban watcher mixin。
- `tools/process_registry.py` 已抽出背景进程事件 seam；`tools/kanban_background_bridge.py` 单独承接 Kanban mirror subscriber，且默认关闭。
- `toolsets.py` 已有 `coding` posture toolset，说明核心 tool schema 成本优化已经开始具备方向。

### 1.2 仍然存在的架构债 / 本轮已收口项

- 已收口：`toolsets.py` 的 `_HERMES_CORE_TOOLS` 曾包含 `kanban_*` 工具。虽然工具 handler 有 check_fn gate，但从“core narrow waist”角度，产品/编排工具仍出现在共享 core list 中。本轮已把 `kanban_*` 从共享 core platform toolsets 移出，保留显式 `kanban` toolset，并依赖 `model_tools.get_tool_definitions()` 在 `HERMES_KANBAN_TASK` 存在时自动追加 worker lifecycle tools。
- `gateway/background_services.py` 的 generic registry 仍内置知道 Kanban 配置键并直接 import `gateway.kanban_watchers`。这是从硬继承改成了 config-gated import，但尚未完全 plugin/distribution 化。
- `tools/kanban_background_bridge.py` 仍位于 core `tools/` 下，属于 P1 兼容 subscriber，而非 Jarvis plugin。
- 已收口：`hermes_cli/config.py` 中 Kanban 配置存在 source-of-truth 漂移：文件前段曾定义 `kanban.auto_subscribe_on_create`，但后段重复的 `kanban` dict 覆盖了它，运行时 `DEFAULT_CONFIG['kanban']` 不含该 key；同时 `tools/kanban_tools.py` 仍通过 `cfg_get(..., default=True)` 把它当默认开启。本轮已删除被覆盖的重复块，把 `auto_subscribe_on_create=True` 合并到最终 `DEFAULT_CONFIG['kanban']`，并加测试锁定。
- 架构地图没有仓内 canonical source。Lark 文档更新方便，但代码审查、PR、CI、离线维护需要 repo-local source 或生成/同步机制。
- 代码规模较大，`gateway/run.py` 仍超过 17k 行，`hermes_cli/config.py` 超过 6.7k 行。虽然这不是单点 bug，但会放大产品层泄漏、配置 fallback、测试定位和 upstream sync 成本。

## 2. 目标架构

### 2.1 分层

1. Hermes Engine：agent loop、model/tool dispatch framework、gateway/cron/profile/plugin framework、session/memory/config/security primitives。
2. Jarvis Product Distribution：Jarvis plugins、sidecars、Dashboard extensions、profile templates、cron templates、skills、worker launchers、health checks、migration manifests。
3. User Runtime Overlay：Boss/企业私有 profiles、routing、allowlist、secrets、private skills/scripts/cron、business connectors。
4. Update & Migration Plane：Hermes upstream sync、Jarvis distribution version、overlay compatibility、migration/rollback/preflight。

### 2.2 核心不变量

- 不破坏 per-conversation prompt caching。
- 不破坏严格 message role alternation。
- 新能力默认不扩大 model-facing core tool schema。
- 非 secret 行为配置进入 `config.yaml`，不新增用户可见的非 secret `HERMES_*` 开关。
- Jarvis-local 行为必须可禁用、可迁移、可回滚，且不覆盖用户 overlay。
- 所有路径必须 HERMES_HOME/profile-aware，不能回到 `~/.hermes` 硬编码。

## 3. P0/P1/P2 方案

### P0：收口剩余默认副作用与 core 泄漏

范围：只做低风险、行为合同型收口。

- 已完成：横查 `_HERMES_CORE_TOOLS` 中产品层工具，确认并实施 `kanban_*` 从共享 core list 移到显式 `kanban` toolset / dispatcher worker 自动追加路径。
- 为 generic Hermes 添加负向测试：默认配置下不导入 Kanban watcher、不注册 Kanban subscriber、不创建 Kanban task、不启动产品层 background service。
- 为 Jarvis/Kanban opt-in 添加正向测试：显式 config/env gate 后 watcher/subscriber 可注册、可处理事件、错误 best-effort 不影响 core lifecycle。
- 已完成：合并 `kanban.auto_subscribe_on_create` 默认值，避免运行时默认依赖隐式 fallback；后续仍需继续横查所有 `cfg.get(..., True)` / exception fallback / env override。
- 已完成一处：清理 `gateway/kanban_watchers.py` 中 “dispatch_in_gateway defaults to true” 的 stale 注释；后续继续清理 core 注释和测试中的 Jarvis-specific 叙述。

验收：

- 已验证：`tests/test_toolsets.py`、`tests/tools/test_kanban_toolset_exposure.py`、`tests/tools/test_kanban_tools.py`、`tests/hermes_cli/test_kanban_core_functionality.py`、`tests/tools/test_process_registry.py`、`tests/tools/test_kanban_background_bridge.py`、`tests/gateway/test_kanban_notifier_watcher_dispatch_gate.py` 聚焦回归通过。
- 已新增/更新测试覆盖：generic Hermes 默认不暴露 `kanban_*`；`HERMES_KANBAN_TASK` worker 自动获得 lifecycle tools；`DEFAULT_CONFIG['kanban']` 显式包含 `auto_subscribe_on_create=True`；`dispatch_in_gateway` 和 background tracking 默认关闭。
- 已验证：`rg "GatewayKanbanWatchersMixin|Jarvis Dashboard should|dispatch_in_gateway defaults to true|cfg\.get\([^\n]*dispatch_in_gateway[^\n]*True|cfg\.get\([^\n]*track_background_processes[^\n]*True"` 无残留。

### P1：Jarvis Product Distribution 边界

范围：设计并逐步迁移，不一次性大搬迁。

- 定义 Jarvis distribution manifest：产品 plugin、sidecar、profile template、cron template、skill bundle、Dashboard extension、migration script 的归属与版本。
- 把当前兼容层作为 migration bridge：`gateway/background_services.py` 和 `tools/kanban_background_bridge.py` 继续存在，但明确标注为 built-in compatibility subscriber/service。
- 下一步把 Kanban watcher/bridge 的注册入口迁到 Jarvis plugin/distribution module，Hermes engine 只暴露 generic service/subscriber API。
- 设计文档已落地：`docs/jarvis-product-distribution-boundary-20260627.md`，包含 ownership model、manifest shape、迁移阶段、文件归属、rollback 与测试矩阵。
- 设计用户 overlay 保护：Jarvis update 只能建议/迁移 schema，不能覆盖 Boss 私有 cron、profiles、skills、scripts、secrets。
- Profile 职责明确：`default` 为主会话与入口；`jarviscode`、`jarvisresearch`、`jarvisreview` 为 worker profile；运行实例由 gateway/dispatcher/手动启动决定。

验收：

- 有文件级归属表：Engine / Distribution / Overlay / Migration。
- 有 migration/rollback/preflight 流程。
- 有“旧配置继续工作、新安装默认干净”的兼容策略。

### P1：补齐架构地图真实源

范围：让架构文档与代码维护闭环。

- 新增 repo-local `SELF_ARCHITECTURE.md`，或新增 `docs/architecture/SELF_ARCHITECTURE.md` 并让 AGENTS/skill 指向它。
- 明确 Lark 文档与 repo-local source 的关系：Lark 可作为协作展示层，repo 文档作为审查/CI/coding source of truth。
- 可选：增加 `scripts/architecture_guard.py`，扫描 core-leak signals。

验收：

- 架构守卫要求与仓库实际一致。
- 未来 Hermes/Jarvis 代码任务不再出现“要求读取但文件不存在”的缺口。

### P2：工具 schema 与渐进披露成本优化

范围：成本/安全优化，避免误删工具。

- 统计各 platform toolset 实际 schema 体积，识别高成本工具簇。
- 明确哪些是 hard core，哪些可进入 `coding` posture、`tool_search` deferrable、plugin/MCP、profile opt-in。
- 对 messaging/webhook 场景强化 safe toolset 和 prompt-injection 风险边界。
- 输出 rollout：先度量、再 shadow、再 opt-in、最后考虑默认变更。

验收：

- 给出真实工具列表与 schema 成本证据。
- 给出不破坏 CLI/gateway/cron/profile 的测试矩阵。

### P2：架构回归检查

范围：把人工经验变成可运行守卫。

- 扫描 core-touching files 中的 Jarvis/Kanban/Dashboard leakage。
- 扫描非 secret `HERMES_*` 行为开关。
- 扫描 HERMES_HOME 硬编码与 `~/.hermes` 旧路径。
- 扫描 default-on 产品副作用。
- 扫描 toolset core list 膨胀。

验收：

- 以 pytest 或 doctor 子检查形式运行。
- 误报有 allowlist 和原因说明。

## 4. 已确认的 Kanban 执行项

- `t_adb826de`：Hermes 架构现状审计与优化方案落地（本任务）。
- `t_b4737c5b`：P0: 收口 Hermes core 产品层泄漏。
- `t_89ad641c`：P1: 设计 Jarvis Product Distribution 边界与迁移清单。
- `t_32457741`：P1: 补齐 SELF_ARCHITECTURE/架构地图真实源。
- `t_1fb884ee`：P1: 统一产品层 hook 与后台服务扩展边界。
- `t_8bdb250d`：P2: 工具 schema 与渐进披露成本优化。
- `t_21805c59`：P2: 架构验证仪表盘与回归检查。

## 5. 风险边界

- 不在当前任务中重启/停止生产 gateway；注：本轮中途 Boss 另行批准过一次当前 gateway 重启，我只记录了重启后连通性证据，未把重启作为本审计方案的默认动作。
- 不直接修改生产配置或 profile defaults，除非 Boss 单独确认。
- 不做 git push。
- 不一次性搬迁 Kanban 全模块，先保持兼容 bridge 并用测试保护行为。
- 不把 Boss 私有工作流写入 Hermes engine。

## 6. 推荐下一步

1. 先执行 P0 `t_b4737c5b`，以测试锁定 generic Hermes default-off 行为。
2. 同步执行 P1 `t_32457741`，补齐 repo-local architecture source，降低后续审计成本。
3. 再做 P1 distribution boundary，把 Kanban watcher/bridge 从 built-in compatibility 迁到 Jarvis-managed plugin/distribution。
4. P2 工具 schema 优化和回归检查作为持续质量门，避免未来再次 core 膨胀。
