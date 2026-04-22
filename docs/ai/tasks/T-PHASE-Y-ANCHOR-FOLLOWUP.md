# T-PHASE-Y-ANCHOR-FOLLOWUP — fx_neg_nonexistent 的 anchor-check 回归

**状态：** 待办
**优先级：** 中（不阻塞发布，但削弱了一道防御线）
**发现：** 2026-04-22 session，batch1 集成测试时

## 问题

Phase Y 把 `DESTRUCTIVE_WORK_TYPES` 改为空集后，`fx_neg_nonexistent` 这个负面 fixture 的行为变了：

| 状态 | 走的分支 | 是否触发 "anchors not found" 拦截 |
|---|---|---|
| batch1 前（main + e2e fixtures 裸跑） | `knowledge.search` | ✅ 触发（[service.py:4489](apps/backend/app/orchestrator/service.py#L4489) 写出 `## Task rejected: anchors not found`） |
| batch1 合并后 | `code_develop` → `codegen` → `spec_conformance` | ❌ 不触发，走到后面失败，原因 message 里没有 "anchors not found" |

这是**真实行为差异**，不是 flakiness。

## 根因假设

`_verify_anchors_exist_in_source()`（Defense line 2）只在 knowledge.search 分支触发，不在 code_develop 分支触发。Phase Y 让某些请求的场景分类从 knowledge 改成 code_develop（因为 work_type 默认不再破坏性），结果这些请求绕过了 anchor check。

## 建议修复方向

给 `_verify_anchors_exist_in_source()` 加一个覆盖点，让 code_develop 分支在进入 codegen 之前也跑一次 anchor 验证。位置：`apps/backend/app/orchestrator/service.py`，在 `execution_started role=action stage=action` 事件前插入。

## 验收标准

- 把 `fx_neg_nonexistent` 单跑一轮，终止时 `task.latest_result_json.message` 里必须包含 `"anchors not found"` 和 `"PaymentsDashboard"` 子串
- 不回归 `fx_bugfix_nullcheck` / `fx_css` / `fx_newfile`（这三个本来就不依赖 anchor check）
- 不影响 knowledge.search 分支的原有拦截行为

## 参考

- Phase Y 定义：`docs/ai/phase-summary-zh.md` ~L685
- Baseline 证据：`d:/项目/ops-worktrees/e2e-fixtures` @ commit `e76c4f4` 上 fx_neg_nonexistent 日志里有 `Task rejected: anchors not found` 字样
- Ship 点：`main` @ commit `6a35bdc`（batch1 merge）之后出现回归
