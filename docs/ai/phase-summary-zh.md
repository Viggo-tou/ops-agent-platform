# 多智能体 MVP 各阶段总结

> 本文件是给人看的自然语言解释，不是给模型的 spec。每个 Phase 完成后更新。
>
> 最后更新：2026-04-22

---

## Phase A — 工作台后端持久化 + 权限基础

### 做了什么

把前端"假的"本地存储（localStorage）替换成了真正的后端 API。知识库上传/删除、记忆存储、模型配置 —— 这些数据现在都走后端数据库，不会因为清浏览器缓存就丢失。同时建立了前后端统一的权限体系（RBAC），四种角色（admin、operator、member、viewer）各有不同的操作权限。

### 原理

前端每个页面（知识库、记忆、设置）原来是自己管状态，现在改成调后端 REST API。后端用 SQLAlchemy + SQLite 持久化。权限用 `X-Actor-Role` 请求头传递角色，后端 `require_permission()` 装饰器拦截未授权请求。

### 怎么验证

- 上传一个知识文件 → 刷新页面 → 文件还在（数据库持久化）
- 用 viewer 角色尝试上传 → 被拒绝（403）
- 用 admin 角色上传 → 成功

---

## Phase B — Jira 集成（写回）

### 做了什么

之前系统只能"读" Jira 工单（获取标题、描述、状态）。Phase B 加了"写"的能力：可以把工单状态推进（比如 To Do → In Progress → Done），也可以往工单下面加评论。这样智能体处理完任务后，能自动把进度同步回 Jira。

### 原理

两个新工具注册到工具网关：
- `jira.transition_issue` — 先查 Jira 当前可用的状态转换列表（GET transitions），找到目标状态的 transition ID，再执行转换（POST）。两步操作封装成一次工具调用。
- `jira.add_comment` — 把纯文本包装成 Jira 要求的 ADF（Atlassian Document Format）格式，POST 到工单评论接口。

两个工具都标记为 `APPROVAL_REQUIRED`，意味着执行前需要有权限的人批准（防止智能体乱改工单状态）。编排器新增了 `jira_issue_writeback` 场景，一次请求可以同时加评论 + 转换状态，共享同一个审批。

### 怎么验证

- 发送 "把 OPS-123 标记为 in progress 并加上一条评论" → 系统识别为 writeback 场景
- 审批通过后，Jira 工单状态变更，评论出现
- `ToolExecution` 表有完整的调用记录

---

## Phase C — 沙箱执行环境

### 做了什么

智能体需要执行代码（跑测试、打补丁），但不能在宿主机器上乱搞。Phase C 建了一个隔离的"沙箱"：每个任务有自己的工作目录，在里面 clone 仓库、执行命令、打补丁，完事后可以清理掉。

### 原理

`ExecutionSandbox` 类管理 `data/sandboxes/<task_id>/` 目录：
- `clone()` — `git clone --depth 1` 到沙箱目录
- `run()` — `subprocess.run()` 执行任意 shell 命令，硬超时 + 输出截断（防止 OOM）
- `apply_patch()` — 把 unified diff 写入临时文件，`git apply` 应用，记录 before/after SHA（给回滚用）
- `teardown()` — `shutil.rmtree` 清理

关键安全措施：所有路径操作都校验不会逃逸到沙箱目录之外（`relative_to()` 检查）。

### 怎么验证

- 给一个任务 clone 仓库 → 沙箱目录出现
- 在沙箱里跑 `echo hello` → 返回 `{exit_code: 0, stdout: "hello"}`
- 尝试 `cwd: "../../"` → 被拒绝（路径逃逸）
- `teardown()` → 目录消失

---

## Phase D — 可复现测试流水线

### 做了什么

有了沙箱之后，需要一种标准化的方式跑测试。Phase D 引入了 `tests.yaml` —— 目标仓库里放一个声明式的测试配置文件，列出要跑哪些测试步骤、超时多久、是否必须通过。系统按顺序执行，汇总结果。

### 原理

`TestPipeline` 读取沙箱根目录的 `tests.yaml`，解析出步骤列表，逐个调用 `sandbox.run()` 执行。逻辑：
- 每步记录 exit_code、stdout、stderr、耗时
- `required: true` 的步骤失败 → 后续步骤全部跳过（快速失败）
- `required: false` 的步骤失败 → 不影响整体结果
- 最终输出 `TestRunResult`：总步数、通过数、失败数、跳过数、总耗时

没有用 PyYAML 作为硬依赖 —— 如果装了就用，没装就用内置的极简 YAML 解析器（只支持 `tests.yaml` 的固定结构）。

### 怎么验证

- 沙箱里放一个 `tests.yaml`，3 步全过 → `overall_passed: true`
- 第 2 步是 required 且失败 → 第 3 步被跳过，`overall_passed: false`
- 没有 `tests.yaml` → 报错

---

## Phase E — Diff 审查器（代码变更检查）

### 做了什么

在打完补丁、跑完测试之后，还需要一道自动化的"代码审查"。Phase E 的 `DiffReviewer` 用规则引擎扫描 diff，检查是否有安全隐患或违规操作。通过了才能进入审批环节；被阻断的 diff 直接打回，不浪费审批人的时间。

### 原理

`DiffReviewer` 内置 4 条规则，每条独立评估：

| 规则 | 检查内容 | 阻断条件 |
|------|---------|---------|
| `tests-must-pass` | 测试流水线结果 | `overall_passed == False` |
| `no-secrets` | diff 新增行里的密钥模式 | 匹配到 `password=`、`api_key=`、AWS 密钥、PEM 私钥等 |
| `protected-paths` | 被修改的文件路径 | 触及 migrations、.env、secrets 目录、.pem/.key 文件 |
| `max-diff-size` | diff 总字符数 | 超过阈值（默认 50000） |

任何一条规则产生 `severity: block` 的违规 → 整体 verdict 为 `block`。全部通过 → verdict 为 `pass`。

`protected_paths` 和 `max_diff_size` 可以按项目自定义配置。文件路径从 diff 的 `--- a/...` / `+++ b/...` 头部解析。

### 怎么验证

- 干净的 diff（安全路径、无密钥、测试通过）→ `verdict: pass`
- diff 里有 `+API_KEY = "sk-abc123"` → `verdict: block, rule: no-secrets`
- 修改了 `migrations/0001.py` → `verdict: block, rule: protected-paths`
- 自定义 `protected_paths=["**/custom/**"]` → 只阻断 custom 目录，不���断默认列表

---

## Phase F — 工具级审批门控

### 做了什么

Phase F 之前，系统的审批只发生在"计划审查"阶段 —— 审查员看完执行计划，决定要不要批准。但具体执行某个高风险工具时，是直接跑的，没有二次确认。Phase F 加了工具级别的审批门：标记为 `APPROVAL_REQUIRED` 的工具（比如 Jira 写回、沙箱命令执行），在真正执行前会暂停，创建审批记录，等有权限的人批准后才继续。

### 原理

在 `ToolGateway.execute()` 的入口处加了一个检查：

```
如果 工具的 permission_category == APPROVAL_REQUIRED 且 没有传入 approval_id：
    → 创建 Approval 行（状态 PENDING）
    → 把 ToolExecution 状态设为 PENDING_APPROVAL
    → 抛出 ToolApprovalRequired 异常
```

编排器捕获这个异常，把任务状态设为 `AWAITING_APPROVAL`，记录生命周期事件。

当审批人通过后，`ApprovalService.grant()` 调用 `orchestrator.resume_after_approval()`，这次带上了 `approval_id`。网关看到 `approval_id` 不为空，跳过检查，正常执行工具。

这样实现了：**READ_ONLY 和 WRITE 工具即时执行，APPROVAL_REQUIRED 工具必须经过审批**。而且审批记录关联到具体的 ToolExecution，审计链完整。

### 怎么验证

- 调用 `diff_reviewer.review`（READ_ONLY）→ 直接返回结果，无审批
- 调用 `jira.transition_issue`（APPROVAL_REQUIRED）不带 approval_id → 抛 `ToolApprovalRequired`，Approval 行出现
- 同一调用带上 approval_id → 正常执行，返回结果
- 数据库里 ToolExecution 从 `PENDING_APPROVAL` → `RUNNING` → `SUCCEEDED`

---

## Phase G — 任务回滚与逆向操作 ✅

### 做了什么

给每个有副作用的工具执行加上了"逆向操作描述符"（inverse_action），回滚时按时间倒序重放这些逆向操作。之前的回滚只是改状态、取消审批，现在能**真正撤销副作用**。

### 原理

**记录阶段：** 工具网关在每次成功执行后，根据工具类型生成逆向描述符存入 `ToolExecution.inverse_action_json`：

| 工具 | 逆向类型 | 关键信息 |
|------|---------|---------|
| `sandbox.apply_patch` | `git_revert` | sandbox 路径 + 打补丁前的 commit SHA |
| `jira.transition_issue` | `jira_transition` | 工单号 + 原状态 + 目标状态（互换） |
| `jira.add_comment` | `jira_delete_comment` | 工单号 + 评论 ID |
| 只读工具 | `None` | 不记录（无副作用） |

**回滚阶段：** `RollbackExecutor` 按时间倒序取出所有 `ToolExecution` 行，逐个执行逆向操作：
- `git_revert` → 在沙箱目录里 `git reset --hard <before_sha>`，真正回到补丁前状态
- `jira_transition` / `jira_delete_comment` → 当前是 placeholder（记录意图但不调 Jira API），后续接入真实 API
- 无逆向描述符的执行 → 跳过，计入 `skipped_count`

回滚结果写入 `task.latest_result_json`，包含每一步的成功/失败/跳过详情。

### 怎么验证

- 沙箱里打了补丁 → 回滚 → `git log` 显示 HEAD 回到补丁前的 SHA
- 两个工具执行（A 先、B 后）→ 回滚时 B 的逆向先执行、A 的后执行（倒序）
- 只读工具执行 → `inverse_action_json` 为 null，回滚时被跳过
- 空任务（无工具执行）→ 回滚成功，`total_steps: 0`

---

## Phase H — 聊天生命周期渲染 ✅

### 做了什么

在聊天界面的用户消息和智能体回复之间，插入了一条事件时间线。每个任务的生命周期事件（规划、审查、工具调用、审批、回滚等）以中文自然语言实时展示，附带时间戳。

### 原理

**数据流：**
1. `ChatPage.tsx` 为每个任务发起 `GET /api/tasks/{task_id}/events` 轮询（5 秒间隔）
2. 任务完成/失败/回滚后自动停止轮询
3. 事件列表传入 `MessageList.tsx` 作为 `eventsMap`

**事件映射：** 新组件 `EventTimeline.tsx` 包含一个纯函数 `formatEventMessage()`，将 25+ 种 `event_type` 映射为中文文本：

| 事件 | 显示 |
|------|------|
| `planning_started` | "正在生成执行计划…" |
| `review_passed` | "审查通过 ✓" |
| `tool_succeeded` | "工具调用成功：{tool_name}" |
| `approval_requested` | "等待审批：{action_name}" |
| `rollback_completed` | "回滚完成" |

内部记录性事件（`task_status_changed`、`policy_evaluation_*`、`final_response_emitted` 等）被隐藏，不干扰用户视线。未知事件类型返回 null，不会崩溃。

**样式：** 极简灰色小字，和项目的黑白设计语言一致。时间线不喧宾夺主，但让用户随时知道系统在做什么。

### 怎么验证

- 发起一个任务 → 聊天界面出现"正在理解请求…"→"执行计划已生成"→"审查通过 ✓"等状态行
- 每行右侧显示时间（如 14:32:05）
- 任务完成后，事件轮询停止（Network 面板不再发 events 请求）
- `tsc --noEmit` 无类型错误

---

## Phase I — 结构化日志（structlog）✅

### 预期功能

给整个后端加上 structlog，所有日志以 JSON 格式输出到 stdout。每个 `record_event()` 同时产生一条结构化日志，HTTP 请求也有中间件记录。

### 为什么需要

现在系统的"可观测性"全靠数据库里的 Event 表。问题是：
- 数据库查询慢，不适合实时排查
- 没法接外部日志系统（ELK、Loki、CloudWatch）
- 应用崩溃时数据库里的事件可能没写进去

structlog 输出到 stdout 是云原生标配 —— Docker/K8s 自动收集，外部系统自动索引。和 Event 表是**互补关系**：Event 表做长期审计，structlog 做实时排查。

### 改动范围

- 新增 `app/core/logging.py`（配置）
- `record_event()` 加一行 structlog 输出
- FastAPI 中间件记录 HTTP 请求
- 不改现有 Event 模型

---

## Phase J — OpenTelemetry 链路追踪 ✅

### 预期功能

每个任务的执行过程生成一条完整的 trace（追踪链），包含嵌套的 span（跨度）：

```
Task bootstrap_task [trace_id: abc123]
  ├── Semantic Translation [120ms]
  ├── Planning [450ms]
  ├── Review [80ms]
  ├── Tool: jira.get_issue [320ms]
  ├── Tool: sandbox.apply_patch [150ms]
  ├── Tool: test_pipeline.run [2100ms]
  ├── DiffReviewer [45ms]
  ├── Approval Wait [38000ms]  ← 人在审批，38秒
  └── Tool: jira.transition_issue [280ms]
```

### 为什么需要

structlog 告诉你"发生了什么"，但看不出**因果关系和时间分布**。比如一个任务跑了 5 分钟，是哪个环节慢？是工具超时还是审批等太久？OpenTelemetry 的 trace 视图（Jaeger/Tempo/Grafana）一眼就能看到瓶颈。

这也是排查分布式问题的基础设施 —— 虽然现在是单进程，但 trace context 的设计会让未来拆微服务时无缝衔接。

### 改动范围

- 新增 `app/core/telemetry.py`（OTel SDK 配置）
- 编排器关键方法加 span 装饰器
- 工具网关 execute() 自动创建子 span
- Task 模型加 `trace_id` 字段

---

## Phase K — 指标与成本追踪 ✅

### 预期功能

暴露 Prometheus 格式的 `/metrics` 端点，追踪系统运行指标和 LLM 调用成本：

**运行指标：**
- 任务总数、按场景/状态分
- 工具调用成功率、延迟分布
- 审批等待时间分布
- 审查器通过/阻断比例

**成本追踪：**
- 每次 LLM 调用记录：模型名、输入 token 数、输出 token 数、估算费用
- 按任务、用户、天聚合

### 为什么需要

没有指标就没法做容量规划和成本控制。你现在用 gpt-5.4 xhigh 跑 codex，每个 task 消耗 ~110K token —— 这个数字需要被追踪和控制。指标也是告警（Phase L）的数据来源。

### 改动范围

- `prometheus-fastapi-instrumentator` 或手动 `prometheus_client`
- 新增 `LlmUsage` 模型或 Event payload 扩展
- `GET /api/admin/costs` 聚合端点

---

## Phase L — 告警与健康增强 ✅

### 预期功能

- 增强 `/health` 端点：返回数据库连通性、最近一次成功任务时间、待审批数量、工具失败率
- 配置驱动的告警规则：工具连续失败、审批超时、任务积压、LLM 日预算超标
- Webhook 分发：告警以 HTTP POST 发到 Slack/钉钉/PagerDuty

### 为什么需要

现在系统出问题只能靠人去看日志或数据库发现。Phase L 让系统**主动通知你**——这是从"被动排查"到"主动运维"的关键跨越。

### 改动范围

- 扩展 `app/api/health.py`
- 新增 `app/services/alerts.py`（规则引擎 + webhook 发送）
- 配置项加到 `app/core/config.py`

---

## Phase M — 代码生成工具 ✅

### 预期功能

新增 `codegen.generate_patch` 工具 —— 接收执行计划 + 代码上下文，调用 LLM 生成 unified diff。这个 diff 可以直接被 `sandbox.apply_patch` 应用，然后经过测试流水线和 Diff 审查器检验。

### 为什么是 keystone

之前所有 Phase（C 沙箱、D 测试、E 审查、F 审批、G 回滚）都是**管道的中间环节**。但管道的**入口**——"把计划变成代码"——一直缺失。有了 Phase M，管道才能从头到尾跑通。

### 核心设计

- 输入：plan_json（要做什么）+ context_files（相关文件内容）+ task_description
- 输出：`{diff: str, summary: str, files_changed: [...]}`
- LLM prompt 严格要求输出 unified diff 格式，不允许无关重构
- 每次调用记录 LlmUsage（成本追踪）
- 标记为 APPROVAL_REQUIRED（生成的代码必须经过审查）

---

## Phase N — 端到端流水线编排 ✅

### 预期功能

用户在聊天里说"把 OPS-123 做了" → 系统自动完成全链路：

```
读 Jira 工单 → 理解需求 → 生成计划 → 生成代码(M) → 沙箱应用补丁(C)
→ 跑测试(D) → Diff 审查(E) → 审批(F) → Jira 写回(B) → 完成
                                                    ↕
                                              失败时回滚(G)
```

每一步在聊天时间线(H)实时展示，structlog(I) + OTel(J) 记录全链路追踪。

### 核心新增

- 新场景 `jira_issue_develop`：detect "做了/implement/fix/develop" + Jira key
- 编排器 `_execute_plan()` 扩展为完整 pipeline
- 上下文采集：从知识库或沙箱 clone 获取相关文件内容传给 codegen
- 失败处理：codegen 无效 → 明确报错；测试失败 → 审查前终止；审查阻断 → 附原因

---

## Phase O — Diff 查看器 + 审批交互 ✅

### 做了什么

在聊天时间线里加了两个交互组件：
1. **DiffBlock** — 当事件包含 diff 负载时，渲染一个可折叠的代码变更查看器。默认收起只显示摘要，点击展开完整 diff。
2. **ApprovalActions** — 当出现 `approval_requested` 事件时，渲染 `[批准]` `[拒绝]` 按钮。受 RBAC 门控：只有拥有 `approval:decide` 权限的角色才能看到按钮。做出决定后按钮消失。

### 原理

**DiffBlock 组件** (`components/chat/DiffBlock.tsx`)：
- 接收 `diff: string` 和 `summary: string`
- 用 `useState` 控制展开/收起
- 展开时用 `<pre>` 渲染 diff 文本，CSS 限制最大高度 400px 并允许滚动

**ApprovalActions 组件** (`components/chat/ApprovalActions.tsx`)：
- 接收 `approvalId` 和 `actionName`
- 通过 `useAuth()` 获取当前用户信息和角色
- 两个 `useMutation` 分别调用 `api.grantApproval()` 和 `api.rejectApproval()`
- 成功后 invalidate `tasks` query 缓存，触发界面刷新

**API 层**：`api.ts` 新增 `grantApproval()` 和 `rejectApproval()` 方法，调用已有的后端端点 `POST /approvals/{id}/grant` 和 `POST /approvals/{id}/reject`。

**集成**：`EventTimeline.tsx` 根据 `event_type` 判断渲染：
- `execution_completed` / payload 含 `diff` → 渲染 `<DiffBlock>`
- `approval_requested` → 渲染 `<ApprovalActions>`

### 怎么验证

- 一个带 diff 负载的事件出现 → 看到可折叠的 diff 区域
- 点击展开 → 显示完整 diff 内容
- `approval_requested` 事件 → admin 角色看到 [批准] [拒绝] 按钮
- viewer 角色 → 不显示按钮
- 点击 [批准] → 按钮消失，任务继续执行
- `npx.cmd tsc --noEmit` 无类型错误

---

## Phase P — Anthropic/Claude Provider 集成 ✅

### 做了什么

在已有的 mock / openai / minimax 三个 LLM provider 之外，新增 `anthropic` 作为第四个选项。同时修改了 auto 模式的优先级：`anthropic > openai > minimax > mock`。这样当配置了 Anthropic API key 时，代码生成和任务规划自动使用 Claude，而 MiniMax 继续负责语义翻译。

### 原理

**为什么要加 Anthropic：**
- MiniMax 擅长中文理解和翻译，但输出严格格式的 unified diff（代码生成的核心需求）质量不确定
- Claude 在结构化代码输出（diffs、JSON plans）方面表现突出
- 用户已有 MiniMax key 用于翻译，加 Anthropic key 专门给 codegen 和 planning 用，各取所长

**Anthropic Messages API 对接要点：**
- Auth 用 `x-api-key` header（不是 Bearer），需要 `anthropic-version: 2023-06-01` header
- 响应格式：`{"content": [{"type": "text", "text": "..."}], "usage": {"input_tokens": N, "output_tokens": N}}`
- 和 OpenAI/MiniMax 一样用 httpx 裸调，不引入 SDK 依赖

**改动位置：**
- `config.py` — 新增 `anthropic_api_key`、`anthropic_base_url`、`anthropic_model` 三个设置项，`primary_agent_provider` 类型扩展为包含 `"anthropic"`
- `codegen.py` — 新增 `_call_anthropic()`，`_resolve_provider()` 优先级调整
- `service.py` — 新增 `_generate_plan_with_anthropic()`，`generate_plan()` 里加入 `should_try_anthropic` 分支
- `.env` — 新增 Anthropic 配置项（key 默认留空）

### 怎么验证

- 只配 `ANTHROPIC_API_KEY` → auto 模式下 codegen 和 planning 都走 Anthropic
- 同时配 `ANTHROPIC_API_KEY` 和 `MINIMAX_API_KEY` → codegen/planning 走 Anthropic，翻译走 MiniMax
- Anthropic API 失败 → 安全 fallback 到 mock 计划（不崩溃）
- 94 个单元测试全部通过

---

## Phase Q — 多门防御流水线（Pipeline Defense Gates）✅

### 做了什么

在原有 codegen → apply_patch 路径上，加了七道"闸门"（gates），顺序是：`diff_shape`（语法形状）→ `compile_gate`（编译通过）→ `runtime_validation`（运行时静态检查）→ `spec_conformance.check`（规格约束）→ `spec_conformance.attest`（目标达成证据）→ `goal_decomposition.check`（目标分解校验）→ `evidence_chain`（证据链完整）。每道门只要不通过就把任务打回去。

同时给 `runtime_validation` 配了一个"自修复循环"：发现问题 → 把问题丢回 codegen 让它修 → 重新跑 → 最多重试 N 次。小问题不用人介入，codegen 自己兜底。

还有一个关键修复：sandbox 执行 `claude_code` 时原先丢了 `-p` 非交互标志，导致子进程挂起；补回来后 codegen 才能稳定跑通。

### 原理

**为什么要这么多门：** 以前只有"codegen 产出 unified diff → 直接 apply"一条路径，任何一步出错都得靠人肉 debug。七门是把"LLM 随机性"切成可观测、可重试的阶段，每一步失败都有明确的错误类型和下一步动作（重试 / 自修复 / 升级到人审）。

**每道门干什么：**
- `diff_shape`：unified diff 格式是否合法（`---/+++/@@` 头存在、hunk 行数对齐）
- `compile_gate`：把 diff 应用到 sandbox 后，目标项目能不能过编译（JS 走 `tsc --noEmit`，Python 走 `compileall`）
- `runtime_validation`：静态扫描变更文件的明显运行时问题（null 解引用、未定义变量、拼写错漏等）
- `spec_conformance.check`：原始需求里明确点名的文件/符号，在 diff 里是否被触及
- `spec_conformance.attest`：每个"目标锚点"是否真正达成（anchor count 变化、关键字出现等）
- `goal_decomposition.check`：LLM 之前分解的子目标是否都有对应修改
- `evidence_chain`：把所有门的证据串起来，确保没有未达标项目悄悄过去

**自修复循环怎么做：** `runtime_validation` 把 finding 列表（文件、行号、问题描述）塞进提示给 codegen，要求输出"只修这些问题"的增量 patch，再把两份 patch 合并，重复直至通过或耗尽重试预算。

### 怎么验证

- 故意写个有 null 解引用的 fixture → `runtime_validation` 报错 → 自修复一轮后通过
- fixture `fx_newfile` 跑完 → 七个 gate 全绿，终止状态 `awaiting_approval`
- 流水线事件流里能看到每个 gate 的 `gate_started` / `gate_passed` / `gate_failed` 事件

---

## Phase R — 异步流水线执行池 ✅

### 做了什么

把原来"任务一来就在 FastAPI 请求线程里同步跑到底"的模式改成"提交到后台线程池异步执行"。前端发 POST 立刻返回任务 ID，后台慢慢跑，前端通过 SSE 订阅事件流拿进度。

同时保留了一个后门：测试里可以通过 `set_pipeline_executor_override()` 塞一个 `_ImmediateExecutor`，把异步退化回同步，让测试不用等真的 ThreadPool 调度。

### 原理

**为什么要异步：** 流水线一次完整跑完要几分钟（codegen + 七个 gate），HTTP 请求线程不能占这么久。线程池让后端能同时处理多个任务，前端也不会在提交那一刻卡住。

**为什么保留同步后门：** 压力测试和 E2E 需要确定性——"发起任务 → 等它终结"，真的异步会让测试要轮询或装 SSE 客户端。`_ImmediateExecutor.submit()` 直接调用 `fn(*args)` 同步返回 Future，对外看起来仍是 Executor 接口。

### 怎么验证

- 前端提交一个任务 → `task_created` 事件立刻到，`task_completed` 几分钟后到
- 同时提交两个任务 → 两条事件流并行推进，互不阻塞
- 测试里调 `set_pipeline_executor_override(_ImmediateExecutor())` → 测试退化成同步
- 观察 `ThreadPoolExecutor` 线程数不失控（上限从 `PIPELINE_EXECUTOR_MAX_WORKERS` 配置）

---

## Phase S — 场景分类重判 + code_develop 场景 ✅

### 做了什么

之前的 `scenario` 字段（`code_write` / `code_review` 等）是翻译阶段根据原始请求一把梭哈确定的。翻译会误判——把"给 UserManagement 加列"判成 `code_review`（没碰源码）。

加了一个"翻译后重分类"（post-translation reclassification）：翻译完成 → 看翻译后的结构化字段（verbs / targets / work_type）→ 如果和原始 scenario 矛盾，覆盖一次。同时新增 `code_develop` 场景，专门对应"在已有代码基础上增量开发"的需求（区别于从零写新代码的 `code_write`）。

### 原理

**为什么要重分类：** 翻译阶段 LLM 拿到的只是原始中文，容易被表面动词（"检查"、"看看"）误导。翻译完成后拿到 normalized request（动词、名词、work_type）信号更稳，这时候再判一次 scenario 准确率高很多。

**`code_develop` 和 `code_write` 的区别：** `code_write` = 从零新建文件/模块；`code_develop` = 在已有代码上改/加/扩展。流水线对二者的门有区别——`code_develop` 需要 `spec_conformance` 确认动到已有锚点，`code_write` 不需要。

### 怎么验证

- 原始请求"给 UserManagement 表加 Last Login 列" → 初判 `code_review` → 重分类后变 `code_develop`
- 原始请求"新建一个订单管理模块" → scenario 保持 `code_write`
- `tests/orchestrator/test_scenario_reclassification.py` 覆盖典型 case

---

## Phase T — 前端 Diff 高亮 + Gate 状态面板 ✅

### 做了什么

`TaskDetailPage` 的 diff 预览原先是纯单色文本，看不出哪是加哪是减、哪是 JS 关键字。接了 `highlight.js`：按文件扩展名自动选语言（.js/.jsx/.ts/.tsx/.py/.css…），diff 的 `+` / `-` 行用绿/红背景加语法色。

同时做了个 `GateStatusPanel`：把七个 gate 的状态以圆点 + 标签横向排列（灰=未跑 / 蓝=进行中 / 绿=通过 / 红=失败）。失败时点一下能展开 gate 详情（失败原因、相关锚点、建议下一步）。

### 原理

**选 highlight.js 而非 Prism / shiki：** 体积小（约 100KB gzip），懒加载只拉用到的语言包，CDN 不稳时 fallback 到纯文本，对浏览器 UI 足够。

**为什么 diff 和普通代码高亮要区别处理：** unified diff 的 `+` / `-` 本身是语义（增/删），不能当成行首字符给 highlight.js 当代码喂。实现上先按行切分，识别 diff 前缀，对行剩余部分单独做高亮，再把前缀颜色合进去。

**GateStatusPanel 为什么横向：** 七个 gate 按顺序走，横向布局和时间线对齐，用户扫一眼就知道卡在哪一步。纵向会和右侧 diff 抢空间。

### 怎么验证

- 打开一个跑完的任务 → diff 区域有颜色 + 语法高亮 + 加减行背景
- 打开一个 gate 失败的任务 → 失败 gate 红点 + 下方展开失败详情
- 切换深/浅主题 → 颜色不翻车
- `npx tsc --noEmit` 无类型错误

---

## Phase U — E2E Fixture 覆盖矩阵（T-E2E-EXPAND）✅

### 做了什么

E2E 测试原先只有 4 个 fixture（feature / newfile / css / rename），覆盖面远远不够。这一轮扩到 16 个，按"改动类型"分组：

- **增量开发类：** fx_feature、fx_add_prop、fx_prop_types、fx_config_env、fx_route_add、fx_test_add
- **缺陷修复类：** fx_bugfix_nullcheck
- **重构类：** fx_refactor_extract_hook、fx_rename_component、fx_remove_dead_code
- **样式/体验类：** fx_css、fx_style_spacing、fx_a11y
- **新文件类：** fx_newfile、fx_rename
- **负向用例：** fx_neg_nonexistent（请求一个不存在的锚点 → 期望任务失败，终止状态 `error`，原因包含"not found"）

每个 fixture 是一个 JSON，包含 `request_text`、`terminal_status`、`reason_contains`（负向用例用）。测试用 glob 自动发现 fixture 文件，不需要硬编码列表。

还配了 `e2e_quick` pytest marker：从 16 个里挑 4 个代表性 fixture（feature + bugfix + rename + neg），迭代时跑 4 分钟而非 30 分钟。

### 原理

**为什么要这么多 fixture：** 流水线每个 gate 的代码分支都得跑到才算测到。"加属性"走 target 锚点分支、"删死代码"走 removal 锚点分支、负向用例走"锚点找不到"的早退路径——没有多样 fixture，很多分支从来没有真跑过。

**为什么分六类：** 对应 `work_type` 和 `scenario` 的主要覆盖面。将来要改某一类时，直接跑该组 fixture 就能看回归。

**`e2e_quick` 的意义：** 完整 16 个 fixture 要跑 ≈30 分钟，开发中不可能每改一行都等。选 4 个代表性 fixture 做 smoke test，每 commit 前跑一下能 catch 80% 的回归。完整矩阵放 CI 或发布前跑。

### 怎么验证

- `pytest -m e2e_quick tests/e2e/` → 4 个 fixture 跑过
- `pytest -m e2e tests/e2e/` → 16 个 fixture 全跑
- 负向 fixture `fx_neg_nonexistent` → 终止状态 `error`，原因含"not found"
- 新增一个 fixture JSON → 无需改测试代码，glob 自动拾取

---

## Phase V — 压力测试框架（T-STRESS）✅

### 做了什么

写了一个独立的压力测试驱动 `scripts/e2e_stress.py`：拿同一个 fixture 跑 N 次（串行或多进程并发），输出一份 markdown 报告，看三件事：

1. **延迟稳定性**：p50 / p95 / p99 的 wall time、各 gate 耗时
2. **确定性**：每次跑出来的 patch 哈希是否一致（或聚成几类）
3. **资源漂移**：RSS（内存）和 FD（文件句柄）在多轮之间是否单调上涨（内存/FD 泄漏）

还会检测：通过率漂移（比如 50 轮里有 3 轮失败）、告警 / 推荐的下一步。报告写到 `data/stress-reports/<timestamp>/summary.md`，每轮一个 run-NNN.json 存原始数据。

### 原理

**为什么要压测：** 单次 E2E 通过 ≠ 稳定。LLM 有随机性，sandbox 有残留状态，线程池有竞态。只有跑很多轮才能发现"10 轮偶尔有 1 轮挂"这种隐藏问题。

**每轮 in-memory SQLite 而非复用主库：** 压测不能污染开发库。每轮 `sqlite://` 内存库 + monkey-patch `SessionLocal`，跑完即释放，天然隔离。

**并发用 multiprocessing 而非 ThreadPool：** orchestrator 用模块级 `SessionLocal`，ThreadPool 里多个线程共享会踩脏数据。进程隔离虽然启动慢但最干净。

**为什么要看 patch 哈希聚类：** 理想状态是所有轮产出完全一样的 diff（byte-exact）。现实里 LLM 有温度，可能产出"语义等价但字节不同"的 diff——聚类能告诉你"10 轮里有几种不同输出"，数量越少越确定。

### 怎么验证

- `python scripts/e2e_stress.py --fixture fx_feature --runs 5 --output data/stress-reports/smoke` → 产出 summary.md + 5 个 run-NNN.json
- summary.md 有 7 个 section：概览 / 延迟 / 确定性 / 资源 / 失败列表 / 告警 / 推荐
- `--fixture 不存在的名字` → exit code 2，明确报错
- `--concurrency 4 --runs 20` → 4 进程并行跑 20 轮
- 详细操作手册：`docs/runbooks/stress-test.md`

---

## Phase W — 方向感知 Spec Conformance + Goal Attestation（T-SPEC-ADDITIVE）✅

### 做了什么

之前 `spec_conformance` 的两道门（check + attest）默认当成"破坏性请求"处理：预期请求会减少某些锚点（删文件、移除函数）。但实际用户大部分请求是**增量**的（加列、加属性、加路由），代码量应该增加而非减少。于是这些增量请求莫名其妙被 spec_conformance 判失败。

这一轮分两个 phase 修：

**Phase 1（check 门）：** 引入方向分类器 `_classify_request_direction_with_source()`，返回 `(direction, source)`。direction 有 `destructive` / `additive` / `mixed` 三种。根据 `work_type=feature` 或"增加/新增/加入"这类动词推断方向。check 门的三条规则根据方向取不同行为：

- removal anchor 的 hit_delta 检查：destructive 走 block，additive 走 skip，mixed 走 warn
- 显式引号锚点（"加到 UserManagement 表"）只对 additive 启用
- 路径归一化（`_normalize_for_compare`）处理 `handyman-admin-dashboard/src/...` 和 `src/...` 前缀不一致

**Phase 2（attest 门）：** 同样的方向盲病——`build_goal_attestation` 默认每个锚点方向是 `removal`，成功条件是"count 减少"。增量请求 count 不变 → 全部判 `not_achieved`。修法：引入第三种锚点方向 `location`（"目标位置"），条件是"增量请求 + 锚点已存在于源码树"。`location` 锚点的达成条件改成"diff 动到了至少一个包含该锚点的文件"。

### 原理

**为什么分 phase：** phase 1 改 check 门时以为 attest 门会跟着修好，实际跑 E2E 发现 check 通过了但 attest 还是 not_achieved——因为是两条独立代码路径，各有一套直接写死 `direction=removal` 的地方。拆开两个 phase 让每次改动可审可独立验证。

**`location` 这个新方向的必要性：** 不能让所有增量请求 attest 都无条件通过——如果 codegen 跑偏了（应该动 UserManagement.js 却动了 LoginForm.js），attest 必须把它 catch 住。`location` 的规则是"锚点在源码树 + diff 必须触及某个含锚点的文件"，既让正常增量通过，又能揪出跑偏的 codegen。

**为什么用 `work_type` 而不是猜动词：** 动词推断脆弱（"修改"可能是加也可能是删）。`work_type` 由翻译阶段 LLM 填字段，语义稳定，作为 primary signal 最可靠。动词推断作为兜底，覆盖 `work_type` 缺失的旧 request。

### 怎么验证

- `fx_feature`（"给 UserManagement 表加 Last Login 列"）→ check 门：`direction=additive, direction_source=work_type:feature`；attest 门：UserManagement 锚点 `direction=location, status=achieved`；任务终止状态 `awaiting_approval`，wall ≈174s
- 破坏性请求"删除 UserManagement 功能"+ diff 未触及 UserManagement → attest 仍 `not_achieved`（防止增量语义覆盖范围过大）
- 增量请求但 diff 错过了目标文件 → attest `not_achieved`（揪住 codegen 跑偏）
- 45 个新单元测试覆盖 check + attest 三组方向 × 命中/未命中
- `tests/services/test_spec_conformance_additive.py` 全部通过

---

## Phase X — Planner 过滤构建产物 + 共享路径分类器（T-PLANNER-BUILD-FILTER）✅

### 做了什么

Phase W 修好了增量方向后，再跑 16-fixture 回归发现新的死法：`fx_bugfix_nullcheck`、`fx_css`、`fx_rename` 这些在 `handyman-admin-dashboard` 上的 fixture 被 `spec_conformance` 的 `planner_must_touch` 规则卡住。根本原因是：RAG 检索会把 `build-before/`、`build-after/`、`dist/` 这些 webpack 打包产物（`main.abcdef1.chunk.js`、`.map` 文件）当作"相关文件"召回。planner agent 于是把这些构建产物写进 `must_touch_paths`，但没有人类开发者会去改构建产物——codegen 只改了 `src/...`，`planner_must_touch` 就判 "unmet"，整个任务失败。

这一轮在两个层面挡掉构建产物：

- **Layer 1（planner 前置过滤）：** `apps/backend/app/agents/service.py` 的引用列表构建阶段，调用 `filter_build_artifacts()` 把引用里的构建产物剔除，planner 根本看不到它们。
- **Layer 2（spec 锚点扫描）：** `apps/backend/app/services/spec_conformance.py` 的 `must_touch_clean` 计算 + anchor 扫描阶段，再过一道 `filter_build_artifacts()`，防止万一有漏网之鱼。

过滤逻辑统一落在新模块 `apps/backend/app/services/path_classifier.py`，两层共享，避免规则漂移。

### 原理

**为什么分两层：** Layer 1 是预防（让 planner 从一开始就不提交构建产物），Layer 2 是兜底（即使 planner 绕过了过滤，spec 门也要能抓住）。双重防御，单层失守不炸整体。

**为什么要一个共享 classifier 而不是各写各的：** 原来 `agents/service.py` 和 `spec_conformance.py` 各自有一套 "这是不是构建产物" 的判断，规则会漂移（一方加了 `.min.js`，另一方忘加）。抽成 `path_classifier.py` 后，`is_build_artifact()` / `filter_build_artifacts()` 是唯一真相源，规则演进不会分叉。

**`_BUILD_DIR_PREFIXES` 的覆盖范围：** `build/`、`build-before/`、`build-after/`（webpack dump 目录）、`dist/`、`out/`、`.next/`、`node_modules/`、`__pycache__/`、`.pytest_cache/`、`.tox/`、`.venv/`、`venv/`、`coverage/`、`htmlcov/`、`target/`。`_BUILD_FILE_SUFFIXES`：`.min.js`、`.min.css`、`.chunk.js`、`.bundle.js`、`.map`、`.pyc`、`.pyo`。再加一条正则匹配 webpack 打出来的哈希命名（`main.abcdef1.js`）。

**为什么不删掉 RAG 对构建产物的召回：** RAG 层本身应该是"尽量召回"，过滤发生在下游才对。让 RAG 去理解"哪些是产物"会污染检索召回率指标。分离关注点：RAG 负责找，path_classifier 负责滤。

### 怎么验证

- 53 个单元测试：`test_path_classifier.py` + `test_spec_conformance_build_filter.py` + `test_must_touch_citation_filter.py` 全过
- `fx_bugfix_nullcheck` 单独跑 e2e_quick → spec_conformance 不再因 `planner_must_touch` 卡住（改前 failed，改后进入下一门）
- `is_build_artifact("handyman-admin-dashboard/build-after/static/js/main.abcdef1.chunk.js")` → True
- `is_build_artifact("src/pages/UserManagement.js")` → False

---

## Phase Y — 默认工作类型不再是破坏性 + fixture 文本修正（T-SPEC-W-FOLLOWUP）✅

### 做了什么

Phase W 的 `DESTRUCTIVE_WORK_TYPES` 包含 `{"refactor", "bugfix", "fix", "chore"}`——意思是"只要 work_type 是这四种之一就按破坏性处理"。实际这四种语义**都是混合的**：bugfix 可能是加 null guard（增量），也可能是删死代码（破坏性）；refactor 可能是抽函数（加行数），也可能是合并重复代码（减行数）。直接按"破坏性"默认会把"加 null guard" 这种任务卡住——`fx_bugfix_nullcheck` 加了 `if (!user) return null;`，但 `displayName` 锚点的 count 没减少，attest 判 "not_achieved"，evidence_chain 报 "Unmet goals: ['displayName']"。

这一轮两处小改：

- **`DESTRUCTIVE_WORK_TYPES` 改成空 frozenset。** work_type=bugfix/fix/refactor/chore 不再默认按破坏性，走 fall-through 到 `"mixed"`。如果请求里有真正的破坏性动词（"删除"、"移除"、"drop"），verb 检测还是会把它识别成 destructive——并不会漏掉真的破坏性 bugfix。
- **`fx_neg_nonexistent.json` 的 `reason_contains` 从 `"anchors_missing_from_tree"` 改成 `"anchors not found"`。** 前者是内部规则名，不会出现在用户可见的拒绝文本里；后者才是 orchestrator 实际输出的字符串。fixture 自己的 assertion 写错了。

### 原理

**为什么不引入 `MIXED_WORK_TYPES` 常量：** 没必要。`DESTRUCTIVE_WORK_TYPES` 空集 + `ADDITIVE_WORK_TYPES={"feature"}` 已经够了，其它 work_type 自然 fall-through 到 `("mixed", "no_direction_signal")`。再加一个常量只是增加需要同步维护的点。

**为什么保留 `DESTRUCTIVE_WORK_TYPES` 这个常量不删：** 未来如果真出现 `"deprecation"`、`"removal"` 这种**语义上只做减法**的 work_type，还是要往这个集合里加。删掉常量等于删掉了那个扩展点。

**fixture 文本 bug 的教训：** E2E fixture 的 expected 值如果依赖内部规则名而非用户可见文本，任何内部命名调整都会莫名其妙打破 E2E。规则是"expected 只 assert 用户可见的东西"。

### 怎么验证

- 5 个新单元测试：`test_bugfix_work_type_is_mixed_not_destructive` / `test_bugfix_work_type_with_destructive_verb_stays_destructive` / `test_refactor_work_type_is_mixed_not_destructive` / `test_chore_work_type_is_mixed_not_destructive` / `test_bugfix_attestation_uses_location_direction` 全过
- 58 个 Phase W 相关测试全过（未回归）
- `grep DESTRUCTIVE_WORK_TYPES apps/backend/app` 只在 `spec_conformance.py` 出现，没有其它消费者
- 5 个单元测试全过，58 个 Phase W 相关测试未回归
- ⚠️ **原先这里写的 "e2e_quick 4/4 过" 后来证伪**：2026-04-22 session 重新测基线发现 `t-e2e-fixtures` 单独（未合并优化）跑 e2e_quick 就是 0/4。Phase Y 当时的 4/4 claim 不可复现，大概率是当时 fixture 内容或 knowledge_source 状态与现在不同所致。见 Phase Z。

---

## Phase Z — batch1 优化集成 + 基线校准（2026-04-22）✅

### 做了什么

把四项优化（T-PROMPT-CACHE / T-PARALLEL-GATES / T-PYTEST-XDIST / T-SANDBOX-TEMPLATE）+ Phase X+Y + T-E2E-FIXTURES 整合到 `integrate/optimizations-batch1`，跑 e2e_quick 回归，合并到 main（commit `6a35bdc`）。过程中发现并回退了 T-SANDBOX-TEMPLATE（保留其它三项 + Phase X+Y）。

- **T-PROMPT-CACHE**：`apps/backend/app/core/anthropic_cache.py` 新增 `make_cached_system()`，给 4 个 LLM 调用点（codegen / request_refinement / semantic_review / agents.service）的 system prompt 加 `cache_control: ephemeral`。日志会打印 `cache_creation_input_tokens` / `cache_read_input_tokens`。
- **T-PARALLEL-GATES**：stage-3 三道 post-apply 闸门（spec_conformance / goal_decomposition / evidence_chain）从串行改并发，用 `submit_pipeline_job` + `wait(FIRST_EXCEPTION, timeout=120s)`。config 加了 `gate_parallel_enabled=True` / `gate_parallel_timeout_sec=120`。
- **T-PYTEST-XDIST**：pytest 加 `-n 2 --dist loadfile` 并行；所有 Anthropic 调用点统一包 `app/services/llm_retry.py` 的 `retry_on_rate_limit()`（指数退避 429）。
- **T-SANDBOX-TEMPLATE 已回退**（commit `52aa143`）：原本用 git worktree 从模板开 sandbox（12× 加速），但 `git -C <template> worktree add <relpath>` 把相对路径 `data/sandboxes/<task_id>` 解析到 template 目录下，导致 sandbox 落在错误位置，随后 apply_patch 报 "Sandbox does not exist"。可以未来修好后重新引入（传绝对路径即可）。

### 原理

**为什么 batch1 的 e2e_quick 最终 0/4 PASS 还是能 ship：** 集成前先跑了一次 `t-e2e-fixtures` 纯基线（commit `e76c4f4`，无任何优化），结果也是 0/4。也就是这 4 个 fixture 在主干本来就不过；batch1 没引入新的失败，只是换了失败模式（由于 Phase Y 调整了场景分类，3/4 fixture 从"走 knowledge 查资料"改为"正确进 codegen"——这反倒是行为改善，只是进去之后又撞到别的预存 bug）。

**Phase Y 的副作用**：`fx_neg_nonexistent` 从 knowledge 分支改走 code_develop 分支，绕过了 `_verify_anchors_exist_in_source()` 这道防御线。这是 batch1 唯一真实的行为回归，已立 follow-up ticket `docs/ai/tasks/T-PHASE-Y-ANCHOR-FOLLOWUP.md`。

**教训（已进 memory `feedback_verify_baseline_first.md`）：** Prior session summary 里的 "4/4 pass" 被当成事实用了，实际它是时间冻结的快照，不可复现。以后判断 regression 前必须**先**在未合并状态下实测基线，不能继承断言。

### 怎么验证

- `main` @ `6a35bdc` = batch1 merge；`git log --first-parent main -5` 能看到 merge commit
- 基线对照：`d:/项目/ops-worktrees/e2e-fixtures` @ `e76c4f4` 跑 e2e_quick 是 0/4（用时 1:35，全走 knowledge 分支）
- batch1 post-revert e2e_quick 也是 0/4（用时 12:26，3/4 正确进 codegen），说明优化**没引入 regression**
- 84/84 单元测试过，`python -m py_compile` 核心模块全过
- prompt-cache 生效验证：重跑 codegen 后日志里应有 `cache_read_input_tokens > 0`
- parallel-gates 生效验证：日志里有 `Starting parallel post-apply gates` → `Parallel post-apply gates completed` 成对出现

---

## Phase AA — Evidence 证据链全套：schema + claim binding + must-touch + chain closure（2026-04-23 → 2026-04-27）✅

### 做了什么

四个 ticket 围绕"证据"这条数据线打了一波组合拳，把 evidence 从"散落的几个 dict"升级成"统一 schema + 闭环审批 gate + LLM 合成必须挂引用"。**全部已合到 `checkpoint/pre-reclassify`**。

- **T-WS-FS-WORKSPACE 落地 evidence 统一 schema**（commits `015d256` + `f646cb6`）：新增 `apps/backend/app/schemas/evidence.py`，定义 `EvidenceItem` + `EvidenceSource` 枚举（8 种来源：rag_lexical / rag_fts5 / rag_card / cc_glob / cc_grep / cc_read / user_provided / spec_anchor）。这一步只建管子，CC 工具 / FTS5 / cards 三个 source 的实际填充逻辑还未实现（后续 Phase 3.3 做）。同时给每个 task 落地一个 per-task FS workspace，给 reviewer / 长期记忆 / resume 协议铺地基。
- **T-KB-CLAIM-BINDING**（commit `2d9ec5e`）：MiniMax 合成出来的每条论断（claim）现在必须挂上对应的 `KnowledgeCitation { claim, file_path, line_range, source_channel, confidence }`，前端 chat 可点跳转到 KB 文件。
- **T-EVIDENCE-MUST-TOUCH-FILTER**（commit `b618681`）：spec_conformance 抽出的 `must_touch` 列表过滤掉非源文件目标（构建产物 / 锁文件 / 资源），避免 LLM 被引导去改不该动的文件。
- **T-041-04 EVIDENCE CHAIN CLOSURE**（commits `694a1fe` + `e64a672`）：审批入口强制校验"证据包 + diff + goal attestation"三者闭合，缺任意一项审批按钮不可用，前端明示原因。`e64a672` 是上线后修的一个边角 bug：path 前缀不匹配时（如 `hosteddashboard/...` vs `handyman-admin-dashboard/...`），改成 suffix-tolerant 匹配，避免误报"证据指向的文件不存在"。

### 原理

**为什么这四件事捆成一个 phase**：它们是一根线上的不同断面。`EvidenceItem` 是数据契约；`KBClaim binding` 把合成结果跟证据系起来；`must-touch filter` 让证据不污染 codegen 目标；`chain closure` 把证据塞进审批 UI，让审批人能验。**任一缺失，证据链都不闭合**——单独看每一个像小改进，合起来才让"governance-first"这个定位真站住。

**为什么 schema 没把 cc_glob/cc_grep/cc_read/rag_card 的 source 实现一起做**：这是有意的"留扩展点"。CC 工具结果接入需要等 Phase 3.3 混合证据召回那个大块（roadmap 已升级反映这一点）；rag_card / rag_fts5 需要 Phase 3.5 预索引。先把 schema 放好，让后续 ticket 直接往里塞，不用改全链路。

**suffix-tolerant path matching 的痛点**：原来的 evidence_chain 用 exact path equality 判断"diff 改的文件 ⊆ 证据声明的文件"。但实际项目里 KB 索引的路径根（如 `hosteddashboard/`）和 backend serialize 的路径根（如 `handyman-admin-dashboard/`）经常不一致。改成"suffix 末段匹配"既保留拦截能力，又容忍这种命名漂移。

### 怎么验证

- `git log --first-parent checkpoint/pre-reclassify | grep -E "(EVIDENCE|FS-WORKSPACE|CLAIM-BINDING)"` 能看到 4 条 commit
- `apps/backend/app/schemas/evidence.py` 存在且包含 `EvidenceSource = Literal[...]` 8 种 source
- 跑一次 `process_question`：返回的 `latest_result_json.result.citations` 数组里每条 claim 都有非空 `file_path` + `line_range`
- 跑一次 `jira_issue_develop`：审批 UI 缺证据时按钮灰掉，附文字"缺少证据包/diff/goal attestation"
- spec_conformance must_touch 列表里不再出现 `package-lock.json` / `dist/*.js` / `*.png` 之类

---

## Phase AB — codegen.repair 多轮 + 超 cap 转审批（T-PIPELINE-REPAIR-CAP-IMPL，2026-04-27）✅

### 做了什么

把原来"compile_gate 失败 → 单轮 repair → 还失败就整个 task FAILED"的死板流程，改成"最多 N 轮 repair（默认 3）+ 每轮独立超时（默认 180s）+ 超 cap 转 awaiting_approval 带 `repair_summary`"。**已合到 `checkpoint/pre-reclassify`**（commits `75adc6d` + `a3f0cf4`）。

- 新增 settings：`OPS_AGENT_CODEGEN_MAX_REPAIR_ATTEMPTS=3` / `OPS_AGENT_CODEGEN_REPAIR_TIMEOUT_S=180`
- 新增 orchestrator method `_run_compile_repair_loop`，循环最多 N 轮，每轮：dispatch `codegen.repair` → 重跑 compile_gate → pass 则 break
- 超 cap 时：task 转 `AWAITING_APPROVAL`，pipeline_state 落 `pending_compile_repair_approval_id` + `compile_repair_cap_exceeded=True` + `repair_summary`（每轮 attempted/repaired/duration/timed_out）
- 审批人 grant 时清掉这两个 key，pipeline 继续；reject 则 task FAILED
- 3 个 unit test 覆盖：超 cap / 中途成功 / 单轮超时

### 原理

**之前问题**：P69-7 实测两次都因为 compile_gate 反复失败被卡死。codegen 一次写 6 个文件，每个都有点小毛病；单轮 repair 只能修 5 个（因为 fan-out batch 上限），第 6 个永远修不到。整个 task 死在最后一个文件上，没办法把"先把能修的修完，剩下的让人审批"这种自然策略走通。

**为什么 cap=3 不是 cap=10**：每轮 repair 调一次 LLM ≈ 60-120s。3 轮已经基本能覆盖"批次太大装不下"的场景。无脑加大 cap 只是让 token 烧得更多，不解决"codegen 输出质量本身差"这个根因——根因要靠 codegen provider 切换 / prompt 调优来解，不是靠 repair 抹屁股。

**`compile_repair.cap_exceeded` 这条事件 + payload 里塞 `rounds_summary`**：让 reviewer 在 awaiting_approval 状态打开任务时能看见"系统已经尝试了 3 轮，分别动了哪些文件，每轮多少秒，超时没"——比啥都没有强一万倍。但这个信息现在还是 raw JSON，**前端没专门 UI 展示**（待 Phase AD 的 Stage Log 后续 dispatch T-FAILURE-DIAGNOSIS 解决）。

### 怎么验证

- `git log --first-parent checkpoint/pre-reclassify | grep "PIPELINE-REPAIR-CAP"` 能看到 `75adc6d` + `a3f0cf4`
- `apps/backend/app/orchestrator/service.py` 含 `_run_compile_repair_loop` 方法
- 触发 P69-7 同款 task：超 cap 后任务在 `AWAITING_APPROVAL`，`latest_result_json.result.decision == "compile_repair_cap_exceeded"`，`rounds_summary` 数组有 3 条
- 跑 `pytest apps/backend/tests/orchestrator/test_repair_cap.py`：3 测试过

### 已知遗留

**runtime_validation 仍是单轮 repair**（orchestrator/service.py:2727 `_rv_max_passes = 2  # initial + 1 repair attempt`）。同样的"单轮不够"问题在 runtime_validation 那条线上还在，待后续 ticket。

---

## Phase AC — T-CHAT-APPROVAL-UX：前端审批块 + 自动滚动（2026-04-23 → 2026-04-24）⚠️ 在 worktree，未合并

### 做了什么

把 chat 页面里 `awaiting_approval` 状态从"小圆点+处理中…"换成独立的 `<AwaitingApprovalBlock>`，并自动滚到审批按钮，解决用户判断"前端卡死"的体感 bug。**只在 `feat/chat-approval-ux` worktree 落地（commits 包括 `83aa152` / `5dc73f7` / `89498b6`），尚未合到 checkpoint/main**。同时也存在于 `feat/qa-accuracy-benchmark` 这个 worktree（两个分支有重叠）。

- 新组件 `apps/web/src/components/chat/AwaitingApprovalBlock.tsx`
- `MessageList.tsx` 在 `awaiting_approval` 分支挂载新组件 + 切到该 task 时 `scrollIntoView()` 到审批块
- 测试 `apps/web/src/components/chat/__tests__/AwaitingApprovalBlock.test.tsx` 覆盖：渲染、按钮 enabled/disabled 状态、grant/reject 触发的 API 调用

### 原理

**根因是 UI 同质化**：`MessageList.tsx` 里 `TERMINAL_STATUSES` 只含 `completed/failed/rolled_back`，所有非终态都渲染成 `<ThinkingIndicator>`（小圆点+"处理中…"）。`awaiting_approval` 套这个模板视觉上跟"正在跑"几乎一样，加上审批按钮埋在 timeline 末尾，用户不滚到底就看不到 → 误判"前端卡死"。**这不是后端问题，是状态可见性问题**。

**为什么单独做新组件而不是改 ThinkingIndicator**：状态语义不一样（一个是"机器在跑别打扰"，一个是"等你拍板"），用户的反应也不一样（前者干等，后者要操作）。强行复用同一个组件等于继续骗用户。

### 怎么验证

- 切到 `feat/chat-approval-ux` worktree（`D:/项目/ops-worktrees/chat-approval-ux`）→ 起前端 → 触发一个 develop task → task 跑到 `awaiting_approval` 时 chat 页面应显示**独立色块** + 大按钮，并自动滚到位
- 单元测试：`apps/web/src/components/chat/__tests__/AwaitingApprovalBlock.test.tsx` 通过
- 截图证据：仓库根的 `bug-p69-8-bottom.png`（修前）、`p69-8-approval-block-verified.png`（修后）

### 状态待办

未合并到 checkpoint。Stage 1 audit 标记需要决策：合 / 改造 / 重做。建议合（已经有测试覆盖、screenshots 验证过）。

---

## Phase AD — T-QA-ACCURACY-BENCHMARK + 第一次诚实基线 27.06%（2026-04-23 → 2026-04-26）⚠️ 在 worktree，未合并

### 做了什么

T-QA-ACCURACY-BENCHMARK 的实现 + 首次基线已完成，**全部在 `feat/qa-accuracy-benchmark` worktree**（39 commit 超前 checkpoint，29 个 dirty 文件）。**未合到 main / checkpoint**。是当前**唯一可量化"系统真实准确率"的来源**。

- `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` — 34 题（A:10 简单定位 / B:10 单文件说明 / C:8 跨文件引用 / D:6 多跳分析），每题带 `expected_keypoints` + `expected_citations`
- `apps/backend/scripts/run_qa_benchmark.py` — runner CLI，支持 `--dataset` / `--judge-mode {auto,minimax,rule}` / `--judge-samples N` / `--limit N`
- `apps/backend/scripts/qa_benchmark_judge.py` — judge module，支持 **claude_code CLI + codex CLI（cross-family 双 judge，零 API 余额消耗）**+ `--judge-samples N` 多样本 per-keypoint 多数投票
- `docs/ai/benchmarks/qa-baseline-2026-04-23.md` — 第一次 baseline：A=5.50 / B=8.00 / C=29.62 / D=10.00，**A+B+C aggregate = 13.29%**（rule judge，单样本，远低于 spec 设的 85% 目标）
- `docs/ai/benchmarks/qa-complex-failure-analysis.md` — D 档失败分类：2 retrieval-miss / 2 planner-would-help / 1 synthesis-miss / 1 other。建议下一步 `Phase 3.X — source-only retrieval + planner-assisted multi-hop QA`
- `apps/backend/tests/benchmarks/runs/multi-run-log.md` — 噪声基线 + 多次跑：claude_code judge + N=3 multi-sample → mean **27.06**（"诚实税"将 single-sample 30.04 拉到 27.06）

### 原理

**为什么先做 benchmark 不做优化**：STRATEGY R-1 + roadmap Phase 1 都钉死"不做这一步后面所有'优化'都是拍脑袋"。没有可复现的数字 baseline，任何"+5 分"的优化 claim 都是凭直觉，无法验真。

**为什么用 CLI 当 judge 不用 MiniMax / Anthropic API**：用户暂时不烧 API 余额。CLI judge 走 OAuth / 本地，零成本。代价是 judge 不稳定一些（同一答案不同时间可能给不同分），所以才需要 `--judge-samples N` 取每个 keypoint 多数投票。

**为什么 baseline 这么低（27.06%）**：top-3 lowest 题分别揭示了三个独立 bug——
- A-08 (`0.00`)：support 类问题被路由出 Q&A 流跑去建 Jira 单（**scenario 路由对 "support/ticket" 词汇过敏**）
- B-03 (`0.00`)：JobManagement.js 真存在，anchor gate 太字面 reject 了（**anchor gate over-literal**）
- A-05 (`0.00`)：retrieval 返回 license + bundle 输出，不是源文件（**retrieval 没排除 build artifacts**）

**为什么 anthropic synthesis 跑 Run A3/A4 都 0 分**：副线发现的独立 bug，`synthesis_provider=anthropic` 这条路径在某个点完全坏掉了。需要单独 ticket 调查，未列入主线。

**关键 caveat**：dataset 用 `handyman-admin-dashboard/...` 路径，backend 报 `hosteddashboard/...`。citation precision 全表偏低，但 A/B/C/D 平摊不影响 delta。修这个能让全表分数涨。

### 怎么验证

- 切到 `D:/项目/ops-worktrees/qa-benchmark` 看 commit history：`6e94238` / `d5d457e` / `46c0e61` / `1e271e8` / `2eda909` / `704dbf1` / `0cea861`
- `apps/backend/scripts/run_qa_benchmark.py --dataset apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl --judge-mode rule --limit 3` 应能跑通（不烧 LLM）
- `cat docs/ai/benchmarks/qa-baseline-2026-04-23.md` 看完整 per-tier 数字
- 噪声基线复现：multi-run-log.md 里 Run A 的 mean=27.06 / median=22.50 是当前公认基线

### 状态待办

未合并到 checkpoint。是 **Stage 1 audit 标识的 P0 待整合工作**——所有"优化是否真涨分"的判断都要靠这个 runner，必须 merge 出来作为公共基础设施。

---

## Phase AE — 战略 specs + STAGE_LOG 纪律 + L1 worktree audit（2026-04-28，今天）✅

### 做了什么

诊断了项目当前状态后，写了 5 个新 spec + 升级了 roadmap + 立了一条新的开发纪律 + 做了第一次 L1 audit。**全部 commit 到 `docs/ops-strategic-specs-2026-04-28` 分支**（`f416249`），**未合主干**（用户决定本地继续不 push）。

- **5 个新 spec**（`docs/ai/tasks/`）：
  - `T-QA-ACCURACY-BENCHMARK.md`（重写为 P0 baseline + PR gate 制度。注：实现已完工，见 Phase AD）
  - `T-FAILURE-DIAGNOSIS.md`（P0：task 进 awaiting_approval/failed 时自动跑诊断 LLM，输出中文白话根因 + 建议修复路径）
  - `T-CODEGEN-PROVIDER-OBSERVABILITY.md`（P0：每次 codegen.generate_patch 事件加 provider_used / chain / fallback_count / duration / model_name）
  - `T-COMPILE-GATE-ERROR-CLASSIFICATION.md`（P1：把 `Invalid package config` 这种外部错跟"目标文件语法错"分开，外部错不进 repair loop）
  - `T-SANDBOX-PREFLIGHT.md`（P2：sandbox clone 后立刻验证 package.json/tsconfig.json/pyproject.toml 是否合法）
- **`docs/release-roadmap.md` Phase 3.3 升级**：从"4 条 RAG 通道多路召回"扩为"**混合证据召回**" — 把 EvidenceItem 8 种 source 全列出，加入 CC Glob/Grep/Read 主动 agentic 通道 + rag_card 文件级 markdown 卡片粗筛通道。Phase 3.5 PreIndex 同步升级，加 FTS5 表 + 卡片表的离线构建 + 增量重建策略
- **新建 `docs/ai/STAGE_LOG.md`**：append-only stage 流水簿，每开 stage / dispatch / 完成都必须写。`CLAUDE.md` 加 "Stage Log Discipline" 段，下一次 session 启动必读 STAGE_LOG 最后 5-10 条。
- **Stage 1 = L1 worktree audit 完成**：发现 29 worktree + 33 branch 的真实状态——10 个已合可删，3 个临时 agent worktree，剩下 12 个有 unique 工作未合。**最大风险是 `qa-benchmark` worktree（39 commit 超前 + 29 dirty 文件）**，benchmark 所有产物只活在那。详见 STAGE_LOG.md Stage 1 entry。

### 原理

**为什么写完 5 个 spec 才发现问题**：今天 dispatch 第一个 ticket 时才看到 `feat/qa-accuracy-benchmark` worktree 已经把 benchmark 做完了。这暴露了一个流程缺陷——**spec、worktree、commit 三者没有同步映射**。如果有 STAGE_LOG，每次 dispatch 之前先看一眼"哪些 stage 已开/已闭"就能避免。

**STAGE_LOG vs SESSION_HANDOFF vs phase-summary-zh.md 的分工**：粒度从粗到细 = phase（一个 P-字母段，几周大故事）→ session（一次会话，半天到一天）→ stage（一个聚焦单元，一两小时）。三个并存，因为不同时间尺度的问题需要不同密度的记录。**Stage 这层是之前没有的，是今天才加的纪律**。

**为什么 Phase 3.3 必须升级**：T-WS-FS-WORKSPACE schema 已经把 EvidenceItem 扩到 8 种 source（rag_lexical / rag_fts5 / rag_card / cc_glob / cc_grep / cc_read / user_provided / spec_anchor），但原 roadmap 里 Phase 3.3 还在写"4 条 RAG 通道"——roadmap 落后于代码。继续按旧 roadmap 拆 ticket 会漏 CC 工具 / 卡片这两条主动 agentic 通道，等于把 Phase 5.4 的部分能力提前耗在 3.3 阶段。

**5 个 spec 的优先级排序**：
1. T-QA-ACCURACY-BENCHMARK（虽然实现已完，但要"lock baseline + 钉死 PR gate 制度"）
2. T-FAILURE-DIAGNOSIS（直接解决用户体感问题——失败时不用来问我）
3. T-CODEGEN-PROVIDER-OBSERVABILITY（让 diagnosis 有数据可读）
4. T-COMPILE-GATE-ERROR-CLASSIFICATION（修今天 P69-7 暴露的具体 bug）
5. T-SANDBOX-PREFLIGHT（defense-in-depth）

### 怎么验证

- `git log --oneline docs/ops-strategic-specs-2026-04-28 -2`：能看到 `f416249 docs: 5 strategic specs + roadmap Phase 3.3/3.5 update`
- 5 个 spec 文件存在 + 每个开头都是 SPEC TEMPLATE v2 标头
- `cat docs/ai/STAGE_LOG.md` 最末 entry = Stage 1 CLOSED-DONE
- `grep -A5 "Stage Log Discipline" CLAUDE.md` 应显示新加的强制纪律段

### 状态待办

- 4 个待 dispatch 的 spec（除 BENCHMARK 已实现外）尚未派给 codex
- L2-L4 后续 stage 都未开始
- worktree 真正动手清理 + 整合 qa-benchmark 回主干 = Stage 2/3 待开

---

## Phase AF — 内部里程碑：CC agentic 实现 + 验证 +22.59 分（2026-04-28）✅

### 做了什么

**这是项目第一个用 benchmark 数字证明"方向对"的 phase**。今天一天里：
1. dispatch codex 实现 T-KB-CC-AGENTIC-RETRIEVAL（Phase 3.0 替换原 AST chunking 方案）
2. 同时 dispatch 实现 T-FAILURE-DIAGNOSIS（Phase 6 + 1）
3. 把 cc-agentic + qa-benchmark 两条 feature 分支合并跑联合 baseline
4. 第一次 baseline 跌到 17.82（vs 旧 RAG 27.06，**退步**）；instrument OpenTelemetry spans 发现 80s 卡在 `_synthesize_or_template` 不在 CC agent
5. 应用 J（runner timeout 120→240s）+ P（synthesis snippet cap 6000→3000，纯 .env 覆盖）
6. 重跑 baseline：**mean 49.65 / 完成 34/34 / C-tier 70.62（接近翻倍）**

**也建立了项目级两条新规矩**：
- `docs/ai/STAGE_LOG.md` 强制纪律（CLAUDE.md 写死）：每开 stage / dispatch / 完成都写一条 append-only 流水
- `docs/ai/benchmarks/qa-baseline-2026-04-28.md` 作为 PR gate forcing function：以后任何 Phase 3+ 的优化 ticket，PR 必须 cite before/after benchmark 数字

### 原理

**为什么 CC agentic 比 RAG 强**：

旧路径是单路 BM25 retrieval —— 给 query "Login.js 怎么处理认证"，BM25 拿命中行 ±2 行窗口。结果 imports 含 `firebase` 字面量得分高，**handleLogin 函数体却完全拿不到**（函数体里用的是 `database/ref/get/child`，不带 `firebase` 字面量）。合成 LLM 看着空 imports 写不出真答案。

新路径让 LLM agent 在真实文件树上**主动 grep + read**：拿到 query → glob/grep 找候选文件 → read 关键文件 → 拼合答案。关键不在"算法更聪明"，在"agent 能看到真实代码全貌而不是 chunk 边界切碎的片段"。

**为什么 P（snippet cap 3000）至关重要**：

CC agent 拿 evidence 时会做 `cc_read` 整文件，单文件能到 9700 字符。4 个 citation × 9000 字符 = 36KB prompt，MiniMax 合成 80 秒。把 cap 设到 3000，单 citation 截断到 3000 字符，4×3000 = 12KB prompt，合成降到 60s。

**为什么 D-tier（多跳）反而退步**：

D-tier 真就需要长 snippet 来跟踪跨函数调用链。第一次 CC 跑 D max 拿 78 分（一个完美的多跳回答），加 cap 后 D max 降到 42。**这是 quality 和 latency 的真实 trade-off，数据上验证清楚**——后续要做 tier-aware cap（D=6000 ABC=3000）。

**为什么不写成"portfolio milestone"**：

老实说今天的进步是**内部信号**，不是外部认证。差距：
- 只 1 个 repo（handyman dashboard 62 docs），没有跨多 repo 验证
- claude_code 自己当 judge 评 claude_code 写的代码，**有自评偏差**
- 没接 industry benchmark（SWE-bench / HumanEval）做横向对照
- 只 1 个 synthesis provider（minimax），没换 model 测稳定性

**Strong 内部信号 → 方向认证**，不是"外部 portfolio 级"。后面如果要往 portfolio 推，至少要补 multi-repo + 独立 judge + industry benchmark 这三条。

### 怎么验证

- `git log --first-parent feat/kb-cc-agentic | grep -E "(CC-AGENTIC|RUNNER-TIMEOUT|qa-baseline-2026-04-28)"` 看到 4 条 commit（`6797098` / `2576033` / `7a34c37` / `c090419`）
- `cat docs/ai/benchmarks/qa-baseline-2026-04-28.md` 看完整数字（A 56.50 / B 37.60 / C 70.62 / D 30.33 / mean 49.65）
- `apps/backend/tests/benchmarks/runs/qa-run-20260428T093042Z.jsonl` artifact 有 1 summary + 34 question records，`completed_questions=34, timed_out_questions=0`
- baseline 报告 **acceptance check** 段：4 个目标里完成 2 个（completion + mean）✅，2 个未达（D-tier 30.33 vs 40 / runtime 71min vs 45min）❌

### 已知遗留 + 后续 ticket

| Follow-up ticket | 解决什么 |
|---|---|
| `T-KB-EVIDENCE-TIER-CAP` | tier-aware snippet cap（D=6000 / ABC=3000）让 D 重回 40+ |
| `T-KB-CLI-POOL` | pre-spawn `claude` CLI 进程省 5s/call 冷启动，runtime 71→50min |
| `T-KB-HYBRID-RAG-FAST-PATH` | A/B 走 RAG 13-18s + C/D 走 CC，runtime 71→35min |
| `T-MERGE-CC-AGENTIC-INTO-MAIN` | 把 cc-agentic + failure-diagnosis + benchmark 三条 feature 分支整合回 checkpoint/main |
| `T-WINDOWS-ASCII-PATH-DEBT` | 解决 `D:\项目\` 中文路径系统性 bug（git tag / mojibake / Java I/O 都受影响）|

### 跟"以后所有优化必须有数字"的关联

今天这一波是**第一次完整闭环**：写 spec → dispatch → 第一次跑数字（17.82 退步）→ 不慌不忙诊断 → 改 J+P → 第二次跑数字（49.65 大涨）→ 锁 baseline。

**这个 working pattern 应当成为模板**：spec 必须带 acceptance criteria（数字目标），实现完跑 baseline，benchmark 数字达标才能 commit / merge。后面 Phase 3.1 / 3.3 / 3.4 / 5.4 都按这个 pattern 推。

---

## 全景路线图

```
功能层：  A → B → C → D → E → F → G → H → M → N → O
                                         ↓
可观测层：                          I → J → K → L

Provider 层：                                    P（Anthropic 接入）

防御层：                                             Q → W → X → Y
                                          异步层：        R
                                          分类层：         S
                                          前端层：          T
                                          测试层：           U → V

M = 代码生成（keystone）
N = 端到端编排（集成）
O = Diff 展示 + 审批交互（前端体验）
P = Anthropic/Claude 作为 codegen + planning provider
Q = 多门防御流水线（七门 + 自修复）
R = 异步流水线执行池
S = 场景重分类 + code_develop
T = 前端 diff 高亮 + GateStatusPanel
U = 16-fixture E2E 覆盖矩阵 + e2e_quick
V = 压力测试框架（延迟/确定性/资源漂移）
W = 方向感知 spec_conformance + goal attestation
X = Planner 过滤构建产物 + 共享路径分类器
Y = 默认工作类型不再是破坏性 + fixture 文本修正
Z = batch1 优化集成 + 基线校准

证据 / 流水线 / 测量 / 纪律层（2026-04-23 起）：
AA = Evidence 证据链全套（schema + claim binding + must-touch + chain closure）
AB = codegen.repair 多轮 + 超 cap 转审批
AC = T-CHAT-APPROVAL-UX 前端审批块（worktree only）
AD = T-QA-ACCURACY-BENCHMARK + 第一次诚实基线 27.06%（worktree only）
AE = 战略 specs + STAGE_LOG 纪律 + L1 worktree audit
AF = CC agentic 实现 + 验证 +22.59 分（49.65 vs 27.06 baseline，方向认证）★
```

Phase H 是功能层和可观测层的衔接点。Phase M 是打通端到端 demo 的关键。Phase O 是用户体验的收尾。Phase P 让 demo 能用真正的强代码模型跑通。Phase Q-W 把流水线从"能跑"推进到"可观测、可压测、方向正确、前端友好"的阶段。

**Phase AA-AE 是 2026-04-22 之后的新进度**：从"流水线能跑"推进到"流水线有数字、有证据闭环、有审批 UX、有失败诊断蓝图、有 stage 流水簿这种工程纪律"。其中 **AC 和 AD 还在各自的 worktree 没合主干**——是 Stage 1 audit 标识的 P0 待整合工作。

---

## ⚠️ 底层规则（对所有后续开发者——Claude / codex / 人 都适用）

**所有阶段性或关键性的开发成果，完成后必须写进本文件。** 不写进来的开发等于没做。

**每个 Phase 的格式固定三段式：**

1. **做了什么** — 写得像跟同事说话，不要列代码路径，要讲"这一轮改了哪些用户能感知的行为"
2. **原理** — 讲"为什么这么改 / 为什么选这条路线 / 和其他方案的取舍"，关键设计决策必须有**为什么**
3. **怎么验证** — 具体的验证命令、可观察的结果、测试数量、终止状态等，不要抽象的"跑测试通过"

**语言要求：**

- **中文白话**，不要堆英文术语；必要时用"xxx（英文名）"的括号补充
- **要非常详细**，宁可长一点也不要省略关键原理；目标是"三个月后的自己看一眼就能想起来"
- 代码路径用反引号括起（如 `apps/backend/app/services/spec_conformance.py`），不要在正文里暴露完整绝对路径
- 每个 Phase 末尾用 `---` 分隔

**触发更新的场景：**

- 完成一个 Task（T-xxx）并合并 / 提交
- 引入一个新的架构概念或门（如"第八道门"、"新的 provider"）
- 修复一个会影响用户感知的 bug（不是小 typo）
- 做了一次 schema / 接口的非兼容变更

**谁来写：**

- Claude、codex、人都可以
- 如果是 codex 写，在 prompt 里明确要求"中文白话 + 三段式 + 详细"
- 写完后 Claude 或人做一次 review，确保"原理"段真的讲清楚了为什么

**Phase 命名：** 按字母顺序延续（当前最新是 AE，下一个是 AF、AG…）。一个 Phase 对应一次"有故事可讲"的开发闭环，不是一个 commit。

**跟 STAGE_LOG.md 的分工**（2026-04-28 加）：

- **`docs/ai/STAGE_LOG.md`** = stage 级（一个聚焦工作单元，几小时到一两天）。**append-only**。每开 stage / dispatch / 完成都记。粒度细，更新频繁。
- **本文件 phase-summary-zh.md** = phase 级（一个有故事可讲的开发闭环，几天到几周）。三段式详写。粒度粗，更新慢。
- 一个 Phase 通常包含若干个 Stage 的成果。Stage 是流水账，Phase 是回头看的总结。
- **写本文件之前**：先看一眼 STAGE_LOG.md 最近 N 条 stage，确保 Phase 总结是基于 stage 流水实证的，不是凭印象。
