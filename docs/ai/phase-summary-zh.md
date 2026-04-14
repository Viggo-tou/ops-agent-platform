# 多智能体 MVP 各阶段总结

> 本文件是给人看的自然语言解释，不是给模型的 spec。每个 Phase 完成后更新。
>
> 最后更新：2026-04-12

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

## 全景路线图

```
功能层：  A → B → C → D → E → F → G → H → M → N → O
                                         ↓
可观测层：                          I → J → K → L

Provider 层：                                    P（Anthropic 接入）

M = 代码生成（keystone）
N = 端到端编排（集成）
O = Diff 展示 + 审批交互（前端体验）
P = Anthropic/Claude 作为 codegen + planning provider
```

Phase H 是功能层和可观测层的衔接点。Phase M 是打通端到端 demo 的关键。Phase O 是用户体验的收尾。Phase P 让 demo 能用真正的强代码模型跑通。
