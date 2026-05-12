---
triggers:
  - 修代码
  - 改代码
  - 实现功能
  - 修 bug
  - 修编译错误
  - implement
  - fix bug
  - codegen
  - generate patch
  - apply patch
  - sandbox
  - diff
  - patch
task_type:
  - codegen
  - debug
---

# Codegen rules — what the agent should know before emitting `TASK_INTENT|jira_issue_develop`

## 你不直接写代码,委托后端 pipeline

用户问到"改代码 / 修 bug / 实现功能 / 修编译错误"时,你的工作是**判断意图 + 总结一句话 + 输出 marker**,不是真的去生成 diff。Pipeline 拿到任务后会:

1. 走 planner 阶段 → 出 plan_json
2. 走 codegen 阶段 → 调 `codegen.generate_patch` 工具
3. 走 sandbox 阶段 → `sandbox.apply_patch` + `sandbox.run_command` 跑编译/测试
4. 走 reviewer 阶段 → diff_reviewer + semantic_review 把关
5. 通过 → 提交审批,等用户在 `/tasks/{id}` 点同意

## 你应该跟用户**确认**的最小集

- **目标仓库**(用户没说就问;只有 1 个仓库就跳过)
- **要做什么**(一句话能 paraphrase 即可,不需要架构方案)

不要追问的:实现细节、代码风格、要不要写测试。这些是 planner / codegen 的事。

## TASK_INTENT 输出形态

```
TASK_INTENT|jira_issue_develop|<≤30 字一句话总结>
```

例:
- 用户说"完成 P69-19" → `TASK_INTENT|jira_issue_develop|完成 Jira 单 P69-19 的开发工作`
- 用户说"修 CustomerSignup.kt 的编译错误" → `TASK_INTENT|jira_issue_develop|修复 CustomerSignup.kt 的编译错误`

## 已知失败模式(不要重复)

- ❌ Diff-on-diff:codegen 应该输出**完整修改后的 diff**,不是基于上一次 diff 的二次 diff。Iteration 已经在后端处理这点。
- ❌ 跳过 plan 直接 codegen:planner 出的 file list 是 codegen 的合同,跳过会导致 codegen 改错文件。
- ❌ 在主仓库写代码:所有 codegen 必须在 `data/agent_workspace/<task_id>/` 沙箱里;直接改主仓库 = 灾难。
