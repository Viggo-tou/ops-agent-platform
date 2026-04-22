# T-SKIP-PIPELINE-FOR-QA — 知识问答场景跳过 plan/review/execute 阶段

**Status:** 待开
**Priority:** P1
**Created:** 2026-04-22

## 问题

对于 `scenario == "process_question"`（纯知识问答），当前 orchestrator 仍然会跑：

- translation（LLM 调用 1 次）
- plan（LLM 调用 1 次）
- review（LLM 调用 1 次）
- knowledge retrieval（本地）
- 回答拼接（模板）

但 process_question **不需要** plan / review：用户只想得到答案，不执行任何 action。这三次 LLM 调用是无效开销，贡献了 ~60% 的延迟。

## 目标

在 orchestrator 路由里对 `scenario == "process_question"` 走精简管线：

```
translation（轻量，只提 grounding_terms）→ knowledge retrieval → synthesis（T-KB-LLM-SYNTHESIS）→ return
```

不跑 plan、review、execute、approval。

## 实施要点

1. 在 `apps/backend/app/orchestrator/service.py` 的 scenario 分发里，`process_question` 分支直接走 knowledge path，不进 `_execute_develop_pipeline` 或 planner 流程。
2. `translation` 保留但只用 fallback 版本（`build_fallback_semantic_translation_payload`），不必调 MiniMax。
3. 历史 pipeline event 保留 compatibility：仍然 emit `translation_done` / `knowledge_retrieved` / `completed`，跳过的阶段不要 emit 空的 started/failed。
4. 防御：如果 scenario 判定本身错了（用户问题被误分类成 jira_issue_plan），不要绕过 plan，只在显式 `process_question` 时走 shortcut。

## 验收

- `process_question` 场景端到端耗时 < 3s（现在 ~6-12s，且大头在 plan/review）。
- 非 QA 场景行为完全不变（回归测试防护）。
- 新增测试：`test_process_question_skips_planner_and_reviewer`，mock planner/reviewer，断言它们的方法没被 called 时 answer 仍然返回。

## 依赖

- T-KB-LLM-SYNTHESIS（synthesis 路径必须可用，否则 shortcut 会退回模板答案，用户体验没改善）

## 不做

- 动态学习什么场景可以跳过——先硬编码 process_question，下一步再考虑。
