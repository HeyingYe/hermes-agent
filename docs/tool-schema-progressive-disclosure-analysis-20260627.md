# 工具 schema 与渐进披露成本优化分析（2026-06-27）

## 0. 验证层级与边界

本报告只做分析与 rollout 建议，不直接删除工具、不改默认工具暴露策略。

验证层级：

- 配置存在：已读取 `toolsets.py`、`tools/tool_search.py`、`model_tools.py`、`hermes_cli/tools_config.py`、`hermes_cli/config.py`、`tests/tools/test_tool_search.py`。
- 单点调用可用：已运行 `scripts/tool_schema_cost_report.py` 生成真实 schema 成本 JSON/Markdown；已运行 Python 读取真实 `_HERMES_CORE_TOOLS`、`CONFIGURABLE_TOOLSETS`、`_DEFAULT_OFF_TOOLSETS`、平台 bundle。
- 完整链路 E2E：本任务未启动真实 CLI/gateway/cron 会话，也未连接真实 MCP server；因此对 rollout 只给测试矩阵和 shadow 建议，不宣称默认策略已 ready。

注意：本任务运行在 Kanban worker 环境，`HERMES_KANBAN_TASK` 会使 `model_tools.get_tool_definitions()` 自动追加 `kanban` toolset。为避免把 worker-only 工具误判为普通用户默认成本，下面的主数据用 `env -u HERMES_KANBAN_TASK` 重新测量；worker-only kanban 成本单独列出。

## 1. 真实工具列表与配置证据

### 1.1 `_HERMES_CORE_TOOLS` 当前真实列表

来源：`toolsets.py`。

数量：39 个工具名。

```text
web_search, web_extract,
terminal, process, read_terminal,
read_file, write_file, patch, search_files,
vision_analyze, image_generate,
skills_list, skill_view, skill_manage,
browser_navigate, browser_snapshot, browser_click, browser_type, browser_scroll, browser_back, browser_press, browser_get_images, browser_vision, browser_console, browser_cdp, browser_dialog,
text_to_speech,
todo, memory,
session_search,
clarify,
execute_code, delegate_task,
cronjob,
ha_list_entities, ha_get_state, ha_list_services, ha_call_service,
computer_use
```

重要边界：`kanban_*` 已不在 `_HERMES_CORE_TOOLS`。`toolsets.py` 明确说明：dispatcher-spawned workers 通过 `HERMES_KANBAN_TASK` 自动追加显式 `kanban` toolset；orchestrator profile 可 opt-in；generic Hermes session 不应支付 Kanban 产品层工具 schema 成本。

### 1.2 平台 bundle 与 opt-in 配置

来源：`toolsets.py`、`hermes_cli/tools_config.py`。

- 平台 bundle：`hermes-cli`, `hermes-cron`, `hermes-telegram`, `hermes-discord`, `hermes-feishu`, `hermes-webhook`, `hermes-gateway` 等。
- 可配置 toolset 数量：25。
- 可配置 toolsets：`web`, `browser`, `terminal`, `file`, `code_execution`, `vision`, `video`, `image_gen`, `video_gen`, `x_search`, `tts`, `skills`, `todo`, `memory`, `context_engine`, `session_search`, `clarify`, `delegation`, `cronjob`, `homeassistant`, `spotify`, `discord`, `discord_admin`, `yuanbao`, `computer_use`。
- 默认关闭：`discord`, `discord_admin`, `homeassistant`, `spotify`, `video`, `video_gen`, `x_search`。
- 平台限制：`discord` / `discord_admin` 只在 Discord 平台配置 UI 中出现。
- Webhook 默认安全工具：`web_search`, `web_extract`, `vision_analyze`, `clarify`，不暴露 terminal/file。

### 1.3 tool_search 渐进披露真实机制

来源：`tools/tool_search.py`、`model_tools.py`、`hermes_cli/config.py`。

当前机制：

- `tools.tool_search.enabled` 默认 `auto`，`threshold_pct=10`，`search_default_limit=5`，`max_search_limit=20`。
- 渐进披露只替换 deferrable tools：MCP toolset 前缀工具，或非 `_HERMES_CORE_TOOLS` 工具。
- `_HERMES_CORE_TOOLS` 永不 defer。
- 激活后 deferrable tools 被三个 bridge schema 替代：`tool_search` / `tool_describe` / `tool_call`。
- `handle_function_call()` 对 bridge 做 session toolset scope：restricted session 只能搜索/调用自己已授权 toolsets 内的 deferrable tools。
- `tool_call` 会递归为 underlying tool，pre/post hooks、approval、middleware 都针对真实工具名触发。

现有回归测试覆盖：

- core tools never defer。
- unknown tools 保持 visible，避免 silently dropped。
- threshold gate / config parsing。
- bridge search/describe/call 基本行为。
- scoped catalog 防止 restricted session 通过 bridge 调用 out-of-scope plugin/MCP 工具。

## 2. 真实 schema 成本数据

主数据命令（脚本默认会临时剥离 `HERMES_KANBAN_TASK` / `HERMES_KANBAN_RUN_ID` / `HERMES_KANBAN_CLAIM_LOCK`，因此即使从 worker 里运行也测量普通 session 成本）：

```bash
python scripts/tool_schema_cost_report.py --toolsets hermes-cli --format json --top 50
python scripts/tool_schema_cost_report.py --toolsets hermes-feishu --format json --top 60
python scripts/tool_schema_cost_report.py --toolsets all --format json --top 80
```

如需有意测量 worker-only board tool 成本，使用 `--include-worker-kanban`。

运行时出现非阻塞警告：`named custom provider 'subcode' has no resolvable api_key`。该警告来自本地模型/provider 配置解析，不影响 tool schema 静态统计。

### 2.1 默认 CLI（worker env 移除后）

- `hermes-cli`: 29 tools, 57,550 chars, ~14,388 tokens。

按 toolset：

| toolset | tools | estimated tokens |
|---|---:|---:|
| cronjob | 1 | 1,957 |
| delegation | 1 | 1,927 |
| terminal | 2 | 1,682 |
| browser | 10 | 1,507 |
| session_search | 1 | 1,447 |
| file | 4 | 1,427 |
| skills | 3 | 1,316 |
| memory | 1 | 691 |
| code_execution | 1 | 595 |
| image_gen | 1 | 575 |
| clarify | 1 | 478 |
| todo | 1 | 331 |
| tts | 1 | 240 |
| vision | 1 | 226 |

最大单工具：

| tool | toolset | chars | estimated tokens |
|---|---|---:|---:|
| cronjob | cronjob | 7,827 | 1,957 |
| delegate_task | delegation | 7,707 | 1,927 |
| session_search | session_search | 5,787 | 1,447 |
| terminal | terminal | 5,512 | 1,378 |
| skill_manage | skills | 4,061 | 1,016 |
| memory | memory | 2,764 | 691 |
| execute_code | code_execution | 2,380 | 595 |
| image_generate | image_gen | 2,299 | 575 |
| clarify | clarify | 1,911 | 478 |
| patch | file | 1,872 | 468 |
| search_files | file | 1,710 | 428 |

### 2.2 Feishu/Lark 平台

- `hermes-feishu`: 34 tools, 60,551 chars, ~15,138 tokens。
- 相比 CLI 额外成本：约 +750 tokens。
- Feishu extra：`feishu_doc` 1 tool / ~97 tokens；`feishu_drive` 4 tools / ~655 tokens。

结论：Feishu platform extras 本身不是主要成本来源；主要成本仍来自 shared core 内的大 schema。

### 2.3 all toolsets（非 worker env）

- `all`: 38 tools, 62,473 chars, ~15,619 tokens。
- 相比 `hermes-cli` 只多约 +1,231 tokens，因为多数 opt-in 工具受 check_fn/credential gate 或插件未启用影响，未进入当前 model-facing schema。

### 2.4 worker-only Kanban 成本

在 Kanban worker 环境中测量 `hermes-cli` 会自动多出 `kanban` toolset：

- `kanban`: 7 tools, 13,205 chars, ~3,305 tokens。
- 最大 Kanban tools：`kanban_create` ~1,107 tokens，`kanban_complete` ~824 tokens，`kanban_block` ~436 tokens。

结论：Kanban 成本很高，但已经被限定在 worker/orchestrator 场景；不应回到 shared core。

## 3. 哪些工具必须保留在 core / 默认显式工具里

这里的“保留”不是说永远不能被用户禁用，而是指不应被 tool_search 默认 defer，也不应从核心默认能力中静默迁走。

### 3.1 必须保留：基础执行与文件系统

- `terminal`, `process`
- `read_file`, `write_file`, `patch`, `search_files`
- `execute_code`

理由：它们是 Hermes “能完成任务”的基础能力；大量任务的第一步就是读文件、跑命令、验证结果。把这些放到 tool_search 会增加 round trip，并会让模型在不知道参数 schema 的情况下做错误 bridge 调用。安全边界不靠隐藏 schema，而靠审批、tool middleware、路径 guard、command approval、redaction 和 sandbox scope。

优化方向：压缩描述，不迁出 core。

### 3.2 必须保留：技能/记忆/计划/会话 recall

- `skills_list`, `skill_view`, `skill_manage`
- `memory`
- `todo`
- `session_search`

理由：Hermes 的自改进和跨 session 工作流依赖这些工具。`session_search` 成本高（~1,447 tokens），但它解决“不要让用户重复说明”的核心体验；`skill_manage` 成本高（~1,016 tokens），但用于维护 procedural memory。

优化方向：

- 优先缩短 schema 描述、减少示例密度。
- 可研究 posture-based default：coding posture 是否默认保留 `session_search`，但不能无验证直接移除。
- `skill_manage` 可考虑把 supporting-file 子操作拆到更短 schema 或二阶段 describe，但这属于功能设计变更，需要 E2E。

### 3.3 必须保留：Web/Browser/Vision 基础多模态

- `web_search`, `web_extract`
- `browser_*`
- `vision_analyze`

理由：这些是通用 agent 能力；browser 单工具不大但数量多（10 tools / ~1,507 tokens）。真实网页交互需要完整动作集，否则模型会卡在页面操作中。`browser_cdp` / `browser_console` 虽偏高级，但对调试动态应用和抓 JS 错误有价值。

优化方向：

- 保留 `browser` toolset 为用户可配置项。
- 可以研究 “browser_basic + browser_debug” 拆分：默认保留 navigate/snapshot/click/type/scroll/back/press/get_images/vision，`browser_console` / `browser_cdp` / `browser_dialog` 作为 debug 子集 opt-in。风险是现有 browser automation 流程可能依赖 console 检错，需要回归。

### 3.4 场景保留：`clarify`, `delegate_task`, `cronjob`, `text_to_speech`, `image_generate`

- `clarify`：交互式场景重要；但 cron/kanban headless 已通过调用方禁用或系统指令禁止。保留在 CLI/gateway 默认可理解。
- `delegate_task`：成本高（~1,927 tokens），但并行/复杂任务核心。可考虑在 lightweight posture 中关闭，不建议默认迁入 tool_search。
- `cronjob`：成本最高（~1,957 tokens），但 CLI 用户经常要求创建/管理计划任务。cron job agent 自身已禁用 cronjob/clarify/messaging，避免递归调度。
- `text_to_speech`, `image_generate`：不是每个场景都需要；当前可由 `hermes tools` 配置。若要降默认 CLI 成本，可优先评估从 default core posture 中移出，但必须做产品体验测试，因为用户可能期望“默认能生成图片/语音”。

## 4. 哪些应该迁到 opt-in / platform-specific / plugin / MCP / tool_search

### 4.1 已正确迁出或隔离：Kanban

状态：已不在 `_HERMES_CORE_TOOLS`。应保持。

建议：

- 继续禁止 `kanban_*` 回到 shared core。
- 保持 `HERMES_KANBAN_TASK` 自动追加 worker lifecycle tools。
- Orchestrator profile 通过显式 `kanban` toolset opt-in。
- 架构 guard 继续扫描 `_HERMES_CORE_TOOLS` 中的 `kanban_*`。

### 4.2 已 opt-in 且应保持 opt-in：Home Assistant、Spotify、Discord admin、Video/Video Gen、X search、Computer Use

当前状态：

- `homeassistant`, `spotify`, `discord`, `discord_admin`, `video`, `video_gen`, `x_search` 在 `_DEFAULT_OFF_TOOLSETS`。
- `computer_use` 在 `_HERMES_CORE_TOOLS`，但 tool schema 出现取决于 `check_fn`（本机当前没有出现在 29-tool CLI 数据里）。

建议：

- `homeassistant` 不应作为 generic default 成本；仅 HASS_TOKEN/显式 opt-in 后出现。
- `spotify` 保持插件/opt-in；不要 default-on。
- `discord` / `discord_admin` 保持 Discord 平台限制，不出现在其他平台配置 UI。
- `video`, `video_gen`, `x_search` 保持 opt-in，尤其 x_search 涉及付费/账号权限。
- `computer_use` 建议从 `_HERMES_CORE_TOOLS` 迁到纯 `computer_use` configurable toolset + check_fn，而不是 shared core 名单；理由是 GUI/桌面控制权限敏感，且属于高权限动作。迁移前需确认当前 check_fn gating 已足够避免 schema 出现；迁移方式应是“从 core list 移除，但保持 `computer_use` toolset 可配置”，不是删除工具。

### 4.3 平台 extras：Feishu/Yuanbao/Discord 应继续 platform-specific，不进 core

- `feishu_doc` / `feishu_drive` 当前只随 `hermes-feishu` bundle 出现；成本约 +750 tokens，可接受。
- `yuanbao` 随 `hermes-yuanbao` bundle/opt-in；不应进 core。
- `discord` / `discord_admin` 只限 Discord 平台。

建议：保持现状。未来新增 Lark/Feishu 更重工具（Base、Calendar、Task、Mail 等）应优先以 skills/CLI/plugin toolset 形式 opt-in，不要直接塞入 `hermes-feishu` 默认 bundle。

### 4.4 MCP/plugin deferrable tools：继续依赖 tool_search，补充真实 E2E

当前机制已经为 MCP/plugin 大面提供渐进披露；但本任务未连接真实 MCP server，所以只验证了代码/测试，不宣称真实 MCP E2E 通过。

建议：

- 保持默认 `tools.tool_search.enabled=auto`。
- 对 MCP/plugin 工具数 > N 或 schema tokens > threshold 的场景，使用 bridge。
- 对少量 plugin tools（低于 threshold）允许直接暴露，避免 bridge overhead。
- 补充真实 MCP catalog E2E：启动一个本地 stdio MCP server，注册 20+ fake tools，验证默认工具列表出现 bridge 而不是 20+ raw schemas；验证 `tool_search -> tool_describe -> tool_call` 成功并触发 hooks/approval。

## 5. 优化优先级建议

### P0：测量与 guard，不改默认行为

1. 保留并扩展 `scripts/tool_schema_cost_report.py`。
2. 在报告里区分普通 session 与 `HERMES_KANBAN_TASK` worker session，避免误判 Kanban 成本。
3. 增加 budget guard：例如 hermes-cli 非 worker schema estimated tokens 超过 16k 时 warning，超过 20k 时需要显式 review。不要用硬失败阻塞合法增长，先 warning。
4. 对 `_HERMES_CORE_TOOLS` 产品层泄漏继续 error。

### P1：压缩高成本核心 schema 描述

优先对象：

1. `cronjob` (~1,957 tokens)
2. `delegate_task` (~1,927 tokens)
3. `session_search` (~1,447 tokens)
4. `terminal` (~1,378 tokens)
5. `skill_manage` (~1,016 tokens)

策略：

- 删除重复解释和长示例，把复杂说明迁到 skill/docs；schema description 只保留参数契约和关键安全约束。
- 对 enum/choices 保留，不牺牲模型参数正确性。
- 对危险工具（terminal/cronjob）保留安全规则；不能为了省 token 删除审批、background/notify、delivery、headless 约束。
- 每个工具压缩后跑 schema cost 前后对比，并跑对应单元测试。

预期收益：如果前 5 个大 schema 各减 20–30%，可节省约 1,500–2,000 tokens，低风险高收益。

### P2：posture-based 默认 toolsets

已有 `coding` posture toolset：26 tools / 46,442 chars / ~11,611 tokens（来自 parent report）。这说明 posture 能显著降成本。

建议：

- coding workspace：默认用 `coding` posture，保留 file/terminal/web/browser/skills/todo/memory/session_search/delegate/execute_code，移除 cronjob/tts/image_gen/homeassistant 等。
- research/chat posture：保留 web/session_search/skills/file/vision，terminal 可保留但 code_execution/delegation/cronjob 可按用户配置。
- automation/admin posture：保留 cronjob/delegation/terminal/file。

风险：自动 posture 错判会导致用户需要工具时不可用。必须提供清晰 `/tools` 或 command-line override，并记录 “tool unavailable due to posture” 的可解释错误。

### P3：拆分 browser/debug 与 management tools

可研究但不建议先做：

- `browser_debug`: `browser_console`, `browser_cdp`, `browser_dialog` opt-in。
- `skill_manage` 二阶段：默认只 list/view，manage 在需要写 skill 时通过 tool_search/opt-in 暴露。
- `cronjob` 二阶段：默认只 list/run? create/update 的 schema 很复杂。但这会影响用户自然语言“帮我设一个提醒”的成功率，需谨慎。

## 6. 安全边界建议

1. 不用“隐藏工具 schema”替代权限控制。tool_search bridge 必须继续检查 session scope，并让 underlying tool 走相同 hooks/approval。
2. MCP server 是外部工具供应方，必须保留 MCP security migration：恶意 stdio entries disabled for auditability。
3. Webhook 默认工具必须继续安全最小化，不引入 terminal/file。
4. 高权限本地工具（computer_use、terminal、file mutation、cronjob）必须依赖 approval/middleware/config gate，而不是仅靠是否出现在 core list。
5. 插件 toolsets 默认启用策略要谨慎：当前 unknown plugins default enabled；如果插件生态扩大，建议改为“bundled low-risk default enabled，third-party unknown 默认提示/opt-in”，并配套 migration。

## 7. 测试矩阵与 rollout

### 7.1 必跑单元/集成测试

当前相关测试：

- `tests/tools/test_tool_search.py`
- `tests/test_tool_schema_cost_report.py`
- `tests/test_jarvis_architecture_guard.py`
- `tests/tools/test_kanban_toolset_exposure.py`
- `tests/cron/test_scheduler.py` 中 cron enabled_toolsets/MCP 解析
- `tests/gateway/test_api_server_toolset.py`
- `tests/hermes_cli/test_*tools_config*` / `test_toolset_validation.py`（按实际文件名运行）

建议新增：

1. 非 worker `hermes-cli` schema budget test：`env -u HERMES_KANBAN_TASK` 下统计并记录 warning threshold。
2. Worker session 自动追加 Kanban test：确认 only when `HERMES_KANBAN_TASK`。
3. Realistic fake MCP E2E：20+ MCP tools 触发 tool_search auto，scoped call 成功。
4. Platform bundle delta test：`hermes-feishu` 只多 Feishu doc/drive，不引入无关 Lark mega-toolset。
5. Webhook safety test：默认 webhook toolset 不含 terminal/file/cronjob/delegation。
6. Posture regression test：coding posture 不含 cronjob/tts/image_gen，但保留 file/terminal/search/skills/delegation。

### 7.2 Rollout 阶段

1. Measure-only：每次 tool schema 变更生成 report，PR 附前后 diff。
2. Warning guard：超过预算只 warning，不阻塞。
3. Shadow posture：在日志/diagnostics 中记录“如果使用 posture X 会节省多少 tokens”，不实际改变工具。
4. Opt-in posture：允许用户/平台显式启用 coding/lightweight posture。
5. Default change：只有在 CLI/gateway/cron/Feishu/kanban worker E2E 都通过后，才考虑默认 posture 调整。

## 8. 最终建议清单

### 保留在 core / 默认可用

- terminal/process、file tools、web tools、browser basic、vision、skills、todo、memory、session_search、execute_code、delegate_task。
- cronjob 在 CLI/gateway 管理场景保留；cron job 自身继续禁用递归 cronjob。
- clarify 在交互场景保留；headless worker/cron 用系统约束禁用。

### 保持 opt-in / platform-specific / plugin

- Kanban：worker/orchestrator only，禁止回 core。
- Feishu/Yuanbao/Discord extras：platform-specific。
- Home Assistant、Spotify、Video、Video Gen、X search：opt-in/default-off。
- Computer Use：建议从 shared core 名单迁出到纯 configurable opt-in + check_fn（需单独 PR 和权限说明）。
- MCP/plugin 大面：继续 tool_search progressive disclosure。

### 不建议做

- 不要直接删除 `cronjob`、`delegate_task`、`session_search`、`terminal`、`skill_manage`。
- 不要把安全边界建立在“模型看不到 schema”上。
- 不要让插件/MCP 通过 tool_search 绕过 session toolset scope。
- 不要把平台 mega-toolsets（例如全部 Lark OpenAPI）默认塞进 `hermes-feishu` bundle。
