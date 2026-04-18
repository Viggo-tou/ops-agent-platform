# T-040 — Validator & harness hardening: "no-found > fabrication"

## 核心原则

**LLM 找不到目标时必须显式 `no_found` 拒绝，禁止编造**。当前 harness + validator 任一环都不能把"源码里没有目标锚点"这种情况识别出来，结果 MiniMax 幻觉出了与 P69-10 无关的 patch，Jira 依然被 transition 成 Done（见 T-039 E2E 遗留证据 `3212b6c9`）。

## 根因定位（由 T-039 grant-path E2E 实证）

1. 请求 `"Jira P69-10: remove hardcoded Minij username..."` 被路由到 `knowledge_source_path=D:\项目\HandymanApp-master`
2. `grep -rn "Minij" HandymanApp-master` → **0 命中**；`masterAdmin` → 0；角色 `"Admin"`/`"Staff"` → 0
3. 即 P69-10 描述的是**另一个仓库**（疑似 `HostedDashboard-main`）的工作
4. `spec_conformance.check` 4 条规则里：
   - `hit_delta` / `must_touch` 只认**引号包裹**的锚点 → 裸词 `Minij` 被忽略 → `anchors=[]`
   - `anchors=[]` 时两条规则直接跳过
   - `shadow_implementation` 只在 "全新建" 时触发，不适用
   - `planner_must_touch` 循环自证：planner 自报要改 4 个 .kt → patch 改了那 4 个 → 过
5. `build_goal_attestation.all_goals_met` 在 anchors=[] 时 fallback 到 `not destructive_verbs` → 动词存在则 False，但**不反向 block**，只作为观测字段
6. `test_pipeline.run` 工具调用失败即视为 `skipped`，Kotlin 是否能编译未知

## 修复范围

三条防线，任一兜住就不会让幻觉 patch 过 approval：

### 防线 1 — harness 输入侧（planner/translation）

**文件**：`apps/backend/app/agents/translation.py`、`apps/backend/app/agents/schemas.py`

1. 在 `translation_json` 里新增字段 `anchors: list[str]`：
   - 由 translation LLM 从自然语言请求里抽取"需要出现/消失"的具体标识符：字符串字面量、驼峰/PascalCase token、全大写常量、明显的 identifier。
   - 不强制用户加引号（T-039 案例里 `Minij` 就是裸词）。
   - Prompt 要求："extract anchors only if they are specific enough to grep the codebase with; if the request is generic prose, return []."

2. `GeneratedPlan.must_touch_files` 校验收紧（`agents/schemas.py` + validator）：
   - planner 声明的 must_touch 必须来自"锚点所在文件 ∪ `knowledge.search` 命中的文件"。
   - 否则 planner 输出时就被 reject，重试。

### 防线 2 — harness 预检（任务创建时）

**文件**：`apps/backend/app/orchestrator/service.py`（`bootstrap_task` 或翻译完成后的早停点）

在 translation 完成后、plan 生成前：

```python
anchors = translation.get("anchors") or []
if anchors and self._resolve_knowledge_source_path() is not None:
    missing = [a for a in anchors if not _anchor_exists_in_tree(source_tree, a)]
    if missing and len(missing) == len(anchors):
        # 全部锚点都不在知识源里 → 硬拒绝
        set_task_status(task, TaskStatus.FAILED, ...)
        task.latest_result_json = {
            "message": (
                "## Task rejected: anchors not found\n\n"
                f"The request references {missing!r} but none of these "
                f"appear in the configured knowledge source "
                f"({source_tree.name}). This likely means the task is "
                f"targeting a different repository. Please verify the "
                f"knowledge source configuration."
            ),
            "result": {"scenario": "anchor_not_found", "missing_anchors": missing},
        }
        return
```

**只在全部锚点都找不到时 fail-fast**；部分命中走正常流程（可能是 rename + 新概念）。

### 防线 3 — codegen prompt 出口

**文件**：`apps/backend/app/agents/service.py`（codegen 的 system/user prompt 构造）

给 LLM 显式的拒绝出口：

```
If the anchors listed in `anchors` do not appear in any retrieved source
file, you MUST respond with:
{"error": "anchors_not_in_context", "missing_anchors": [...]}
instead of generating a patch. Do NOT invent plausible replacements.
```

当前 codegen prompt 只告诉模型"生成修复的 patch"，没有任何退路，模型面对不存在的目标只能选"幻觉最像的修改"。

### 防线 4 — validator 收紧（`spec_conformance.py`）

1. **`_extract_quoted_anchors` 退休**，改为优先消费 `translation_json.anchors`；当 anchors 为空时，再退化成"引号 + identifier 启发式"。
2. **`anchor_not_in_tree` 升级为 block**：
   - 当前：`_find_files_containing_anchor` 返回空 → 整条锚点跳过（"nothing to assert"）。
   - 新：anchors 非空 + 含破坏性动词 + 所有锚点均 `not_in_tree` → 追加 `ConformanceFinding(rule="anchors_missing_from_tree", severity="block")`。
3. **attestation 反向 block**：
   - 当前：`all_goals_met=False` 只写进 `latest_result_json.result.goal_attestation`，不影响 verdict。
   - 新：`all_goals_met=False` 时追加 block finding；保留"降级到人工 approval"开关（config flag）。
4. **`planner_must_touch` 去循环性**：
   - 追加前置要求：planner 声明的 must_touch 文件必须与 `anchors` 命中的文件有交集，否则 rule 内部自判 False 并 block。

### 防线 5 — 最低编译门（代位 test_skipped）

**文件**：`apps/backend/app/services/sandbox.py`（或新增 `compile_check.py`）

在 `sandbox.apply_patch` 成功后、`test_pipeline.run` 之前，做语言嗅探 + 语法 dry-check：

| 语言 | 检查命令 |
|---|---|
| Python | `python -m compileall -q <file>` |
| Kotlin | `kotlinc -script -Xuse-k2 -d /dev/null <file>`（如果 kotlinc 不可用，静态 parse via `tree-sitter-kotlin` 或至少 `grep -c "^\s*fun\b"` 形状检查） |
| Java | `javac -proc:none -d /tmp <file>` |
| TS/JS | `tsc --noEmit <file>` / `node --check` |

任何一个失败 → 追加 block finding `rule="compile_check"`。不强求能链接/运行，只要求词法/语法合法。

## 验收标准

1. **回归**：T-039 reject 路径 + grant 路径在 validator 加固后仍跑通（即"对的 patch"依然过审）。需要先找到一个 HandymanApp 里真实存在的清理任务（例如 `dummyCustomers` 相关 grep 命中明确的条目）作为 positive fixture。
2. **新断言**：重新提交 P69-10 原请求（当前知识源还指向 HandymanApp，不换仓库）：
   - Expected: 在 translation 阶段或 conformance 阶段被 block，`status=failed`，`latest_result_json.message` 含 `anchors not found` 或 `anchors_missing_from_tree`
   - Forbidden: `jira_transitioned=True` 且改错文件
3. **换仓库场景**：在 .env 里把知识源改为真正含 `Minij` 的仓库（HostedDashboard-main 或其他），同样请求应正常跑完 + `jira_transitioned=True`。
4. **编译门**：人为构造一个含语法错误的 patch（例如漏 `}`），validator 必须 block，`rule="compile_check"` 出现在 `conformance.findings`。
5. **单元测试**（`apps/backend/tests/services/test_spec_conformance.py`）扩充：
   - anchors 非空 + 全部 not_in_tree → verdict=block
   - anchors 非空 + 全部 in_tree + hit_delta 减少 → verdict=pass
   - must_touch 和 anchors 交集为空 → verdict=block
   - attestation `all_goals_met=False` → verdict=block（flag 开启时）

## 不要改

- 不要改 `jira.transition_issue` 审批门逻辑（T-039 已固化）
- 不要改前端 `MessageList.tsx`（T-039-F 已完成）
- 不要往 translation_json 里加 P69-10 特化字段 —— anchors 必须是通用字段

## 工作流（executor = codex）

1. Read: `apps/backend/app/agents/translation.py`、`apps/backend/app/agents/schemas.py`、`apps/backend/app/orchestrator/service.py`（重点 line 1900-2100）、`apps/backend/app/services/spec_conformance.py`、`apps/backend/app/agents/service.py`（codegen prompt 构造段）。
2. 按防线 1→5 顺序实现，每条防线完成即跑 `cd apps/backend && pytest tests/services/test_spec_conformance.py tests/orchestrator/test_conformance_retry.py -x`。
3. 回归：提交 P69-10 原请求（`curl` 即可，不走 UI）确认在没换知识源时被 block。
4. 实测：换知识源后（由 Claude/用户协助配置），提交 P69-10，确认 jira_transitioned=True 且 patch 触到真实含 `Minij` 的文件。
5. 运行日志写入 `docs/ai/runs/T-040.log`。

## Dispatch

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-040-validator-hardening.md
```

Codex 使用上限恢复后（2026-04-17）再派发；在那之前，可以先完成防线 1（translation schema 加 anchors 字段）作为预研，由 MiniMax 或手工实现。
