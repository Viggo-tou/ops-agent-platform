# Stage Log — 项目 stage-级别活动追踪

> **强制纪律**：每开一个 stage、每 dispatch 一次任务、每完成一项改动，**必须**在本文件末尾追加 entry。这是「下次 LLM 进来能看懂"我们走到哪了"」的唯一可靠源。
>
> 本文件 **append-only**。不要回头改旧 entry。如有错，加新 entry 注明。
>
> 阅读顺序：从下往上看 → 最新进度在底部。
>
> 跟其他 doc 的区别：
> - `SESSION_HANDOFF.md` = 一次会话级粗粒度（一天 / 一次重启）
> - `docs/ai/phase-summary-zh.md` = phase 级（A/B/C/.../Z 一个大段一篇白话）
> - `STAGE_LOG.md` = stage 级（**比 session 细，比 phase 粗**；一个聚焦的工作单元 = 一个 stage）

---

## 模板（每个 entry 格式）

```
### Stage <N> — <短标题>
**Open:** YYYY-MM-DD HH:MM (UTC+10) by <Claude session-id 或 user>
**Status:** OPEN | DISPATCHED | CLOSED-DONE | CLOSED-DROPPED | BLOCKED
**Layer:** L1 | L2 | L3 | L4 (按 2026-04-28 issue 分层；见底部解释)
**Trigger:** 为啥开这个 stage（一句话）

#### 步骤
- HH:MM 做了什么（事实，不是计划）
- HH:MM 又做了什么
- HH:MM dispatch codex `<task-id>` 在 worktree `<path>`

#### Close 摘要（CLOSED-* 时填）
**Close:** YYYY-MM-DD HH:MM
**结果:** 1-3 句白话
**产出文件:** `path/to/file.ext` (+N/-M 行) — 一行一个
**没做的:** 还欠的事（开新 stage 跟踪）
**Lesson:** 下次类似情况要怎么避坑（可选，但鼓励写）

---
```

## Layer 分类（2026-04-28 定，对应 issue 分层）

- **L1 流程纪律** — branch / worktree audit、stage 记录本身、PR gate 制度等
- **L2 架构改造** — retrieval 多路、planner、anchor gate 重构、evidence 池等大块
- **L3 局部 bug** — 一个具体失败题目的修复（< ~100 行）
- **L4 UX / 可观测** — 失败诊断、provider 可观测、错误类型化、sandbox preflight

---

## Stage 记录从这里开始（最新在底部）

---

### Stage 1 — L1 branch / worktree state audit

**Open:** 2026-04-28 13:00 (UTC+10) by Claude (current session)
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律)
**Trigger:** 今天 dispatch 第一个 ticket 时发现 worktree 里 QA benchmark 已基本完工，但我（Claude）和用户都没意识到 → 证明缺一个总览。先把所有 branch / worktree / 浮动改动清出来。

#### 步骤
- 13:00 创建 `docs/ai/STAGE_LOG.md` + 在 `CLAUDE.md` 加 Stage Log Discipline 强制纪律
- 13:05 运行 `git branch -a` + `git worktree list` + 每个 worktree vs `checkpoint/pre-reclassify` 的 ahead/behind/dirty 统计
- 13:10 整理结论（见下）

#### Close 摘要

**Close:** 2026-04-28 13:15
**结果:** 29 个 worktree + 33 个 branch。其中 11 个 worktree 是 0-ahead 已废，12 个是有 unique 工作没合回，3 个是 `.claude/worktrees/agent-*` 临时态。**最大风险**：`qa-benchmark` worktree（39 commit 超前 + 29 dirty 文件），**关键产物 baseline 27.06% / D-tier 失败分析 / Claude Code CLI judge** 全部只活在这里，没合回 main 也没合回 checkpoint。

**Branch 状态总览**

| 类别 | 数量 | 处理建议 |
|---|---|---|
| 0 ahead, 0 dirty (已并入 checkpoint，可删) | 10 个 worktree | `git worktree remove` + `git branch -d` |
| .claude/worktrees/agent-* (临时态) | 3 个 | 用户自己判断，多半可删 |
| 1 commit ahead (single-feature 分支等待合并) | 7 个 | 逐个评估：合并 / 丢弃 / 留 |
| 多 commit ahead (重大未合并工作) | 5 个 | **需要整合方案**（见下） |

**待合并的重大工作（按 commits ahead 排序）**

| Worktree | branch | ahead | dirty | 关键内容 |
|---|---|---|---|---|
| `qa-benchmark` | feat/qa-accuracy-benchmark | **39** | 29 | QA benchmark 全套实现 + 27.06% baseline + D-tier 失败分析 + CLI judge + T-CHAT-APPROVAL-UX 已合 + 大量 orchestrator 重构 |
| `chat-approval-ux` | feat/chat-approval-ux | 26 | 1 | T-CHAT-APPROVAL-UX 独立分支（与 qa-benchmark 重叠） |
| `repair-cap` | feat/pipeline-repair-cap | 26 | 2 | 旧版 T-PIPELINE-REPAIR-CAP（IMPL 版本已在 checkpoint，疑似可丢） |
| `skip-qa` | feat/skip-pipeline-for-qa | 20 | 0 | T-SKIP-PIPELINE-FOR-QA + batch1 + MiniMax synthesis |
| `kb-synthesis` | feat/kb-llm-synthesis | 18 | 0 | MiniMax 合成 + batch1 |
| `merge-optimizations` | integrate/optimizations-batch1 | 16 | 0 | batch1 整合（per Apr 22 handoff 已 ship 到 main `6a35bdc`，疑似可删） |
| `spec-additive` | feat/spec-conformance-additive | 3 | 6 | T-SPEC-ADDITIVE Phase 1+2 + T-PLANNER-BUILD-FILTER |

**Single-commit ahead（小决策）**：
- `e2e-fixtures` — T-E2E-EXPAND 16 fixture（per Apr 22 handoff 已合 batch1）
- `parallel-gates` — T-PARALLEL-GATES（per handoff 已合 batch1）
- `prompt-cache` — T-PROMPT-CACHE（per handoff 已合 batch1）
- `pytest-xdist` — T-PYTEST-XDIST（per handoff 已合 batch1）
- `sandbox-template` — T-SANDBOX-TEMPLATE（per handoff `52aa143` 已 revert，**这个分支已废**）
- `scenario-reclassify` — feat/scenario-reclassify（4 dirty）
- `stress-test` — T-STRESS

**已并入 checkpoint 可删的 worktree（10 个）**：
`abc-integrated` / `ast-chunking` / `claim-binding` / `evidence-chain` / `evidence-filter` / `fs-workspace` / `provider-observability` / `qrw-default-on` / `repair-cap-impl` / `route-lang`

**主工作树脏文件 39 个**：包括我今天加的 5 个 spec（已 commit 到 `docs/ops-strategic-specs-2026-04-28` `f416249`）+ 之前 session 留下的散乱 png/yml/codex-last-message.txt 等。

**产出文件:**
- `docs/ai/STAGE_LOG.md` (+本 entry)
- `CLAUDE.md` (+ Stage Log Discipline 段落 + 必读列表加 STAGE_LOG.md)

**没做的（开新 stage 跟踪）：**
- 真正动手清理 worktree（删/合并/还需用）— Stage 2 决策
- 写**强制 PR gate 文档**（"以后所有优化要 cite benchmark before/after"） — Stage 3
- 主工作树 39 dirty 文件清理（区分"我今天的"和"上次留的"）— Stage 4

**Lesson:**
1. **下次 session 启动**：必读 `STAGE_LOG.md` 最后 5-10 entry，再决定开什么新 stage
2. **每次 dispatch codex 之前**：先检查同名 branch / worktree 是否已存在（`git worktree list`）。今天 dispatch QA benchmark 差点重做整套
3. **任何"看起来该新建"的分支**：先 `git log --all --grep='<keyword>'` 验证一下是不是有人已经做过

---

### Stage 2 — Commit 今日 docs 工作（STAGE_LOG + CLAUDE.md + phase-summary）

**Open:** 2026-04-28 13:30 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律)
**Timebox:** 5 分钟（实际：3 分钟）
**Trigger:** 把今天加的 stage discipline + phase 更新落到 git，避免下次 session 拿不到。继续在已有 `docs/ops-strategic-specs-2026-04-28` 分支上加 commit。

#### 步骤
- 13:30 entry 写入 STAGE_LOG（本条）
- 13:31 `git add` STAGE_LOG.md + CLAUDE.md + phase-summary-zh.md
- 13:32 commit `c3af1a0 docs: STAGE_LOG discipline + phase AA-AE catch-up`（+346 -1）

#### Close 摘要
**Close:** 2026-04-28 13:33
**结果:** 3 文件 commit 到 `docs/ops-strategic-specs-2026-04-28` 分支，未 push（用户决定本地）
**产出文件:**
- `docs/ai/STAGE_LOG.md` (+132 新文件)
- `docs/ai/phase-summary-zh.md` (+195/-0)
- `CLAUDE.md` (+19/-0)
**没做的:** 未 push
**Lesson:** docs commit 真的就是 5 分钟以内的事，timebox 准。

---

### Stage 3 — 整合 qa-benchmark worktree 回 checkpoint/pre-reclassify

**Open:** 2026-04-28 13:35 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律) → 实际推进到 **Phase 1（测量地基）** 完工 95%
**Timebox:** 30 分钟（实际：15 分钟）
**Trigger:** Stage 1 audit 标识 `qa-benchmark` 是最大风险（39 commit ahead + 29 dirty）。benchmark dataset / runner / judge / baseline / D-tier 失败分析全活在那个 worktree，不整合就等于"测量基线没有"。

#### 步骤
- 13:35 entry 写入 STAGE_LOG（本条）
- 13:38 分类 39 commit：8 个纯 benchmark + 5 个 chat-approval + 5 个 redundant repair-cap + 22 个 batch1/T-SKIP-PIPELINE/T-KB-LLM-SYNTHESIS 等架构合并
- 13:40 发现 **main vs checkpoint 已分叉**（互有独占 commit）—— 这是后续要单独处理的 meta 问题
- 13:42 创建新 worktree `D:/项目/ops-worktrees/qa-bench-integration` + 新分支 `feat/qa-benchmark-integration` from checkpoint/pre-reclassify
- 13:45 cherry-pick 8 个 benchmark commit（fb67194 → 0cea861 → 704dbf1 → 2eda909 → 1e271e8 → 46c0e61 → d5d457e → 6e94238）→ **零冲突**
- 13:48 sanity check：34 题 / 正确 tier 分布 / 4 文件齐全

#### Close 摘要
**Close:** 2026-04-28 13:50
**结果:** Benchmark 全套（dataset + runner + judge + baseline 报告 + 失败分析）以 8 commit 形式落到独立 feature 分支 `feat/qa-benchmark-integration`，**未合 checkpoint**（等用户决定）。零冲突 = 后续 merge 风险极低。

**产出文件（在新分支上）:**
- `docs/ai/tasks/T-QA-ACCURACY-BENCHMARK.md` (+70 新文件 — 注意这是 Apr 23 的旧版 spec，跟我今天写的 Apr 28 重写版有差异，merge 时需要决策保留哪版)
- `apps/backend/tests/benchmarks/__init__.py` + `qa_benchmark_dataset.jsonl` (+35)
- `apps/backend/scripts/run_qa_benchmark.py` + `apps/backend/tests/benchmarks/README.md` (+718)
- `docs/ai/benchmarks/qa-baseline-2026-04-23.md` (含 2 份 run JSONL)
- `docs/ai/benchmarks/qa-complex-failure-analysis.md` (+12)
- `apps/backend/scripts/run_qa_benchmark.py` 升级（+109 Anthropic judge, +186 CLI judges, +57 N=3 multi-sample）

**没做的（开新 stage 跟踪）:**
- `feat/qa-benchmark-integration` 合到 checkpoint —— **用户最终决策**
- 原 `qa-benchmark` worktree 里另外 31 个非 benchmark commit（chat-approval-ux + 旧版 repair-cap + batch1 等）—— 留在原地，由 Stage 6+ 决定怎么处理
- main vs checkpoint 分叉的 meta 整合 —— 单独 stage 跟
- T-QA-ACCURACY-BENCHMARK.md 新旧两版的合并决策 —— 用户拍板
- 用 cherry-pick 后的 dataset 重新跑 baseline 验证 27.06% 是否在 checkpoint 基础上可复现

**Lesson:**
1. **Cherry-pick 比 full merge 安全得多** — 39 commit 的 worktree 看起来吓人，但分类后真正"我要的部分"只占 8 commit，且都不碰核心代码
2. **Cherry-pick scope 由 commit message 前缀决定**：`feat(bench)` / `feat(qa-bench)` / `docs(bench)` 这种约定省了我大量时间
3. **下次类似情况先做 commit 分类再决定 strategy**，不要一上来就 `git merge`

**Phase 1 状态**：实现在 `feat/qa-benchmark-integration` 分支待 merge。原 roadmap Phase 1 出口标准是「main 上有一组可复现的 A/B/C/D 分数」—— 离 main 还差一步 merge 决策。本质上 **Phase 1 已 95% 完工，进入 Phase 2/3/6 工作合理**。

---

### Stage 4 — Dispatch T-FAILURE-DIAGNOSIS 到 codex

**Open:** 2026-04-28 13:55 (UTC+10) by Claude
**Status:** CLOSED-DONE (codex 完工，等用户审 + commit)
**Layer:** L4 (UX/可观测) → 推进 **Phase 6（治理 UX）** 1/6 → 2/6
**Timebox:** 15 分钟 dispatch + codex 跑（实际：dispatch 5 分钟，codex 跑 ~22 分钟）
**Trigger:** Stage 3 完成 = Phase 1 essentially done。下一最高 ROI 是 **Phase 6 让 Ops 失败时自己说话**——直接消除"用户来问 Claude 才知道根因"这个流程毒点。Spec 已在 `docs/ai/tasks/T-FAILURE-DIAGNOSIS.md` 写好。

#### 步骤
- 13:55 entry 写入 STAGE_LOG（本条）
- 13:56 创建 worktree `D:/项目/ops-worktrees/failure-diagnosis` + branch `feat/failure-diagnosis` from `checkpoint/pre-reclassify`
- 13:58 dispatch codex（PID 46742, model gpt-5.5, reasoning_effort=medium, full-auto, workspace-write sandbox）
- 14:18 codex 完工 — 13/13 focused test 过；总 backend test 160 个跑了 151 过、8 失败（pre-existing 非 ASCII 路径 bug，跟改动无关）；197,269 tokens 用了

#### Close 摘要
**Close:** 2026-04-28 14:20
**结果:** Codex 按 spec 完整交付。代码留 dirty 在 worktree，**未 commit（按 spec 让 Claude 审）**。

**产出文件（在 `feat/failure-diagnosis` worktree）:**
新建：
- `apps/backend/app/services/failure_diagnosis.py` — 主模块
- `apps/backend/app/services/failure_diagnosis_prompts.py` — prompt 模板
- `apps/backend/tests/services/test_failure_diagnosis.py` — 13 unit tests（全过）
- `apps/backend/tests/orchestrator/test_failure_diagnosis_integration.py` — 集成测试
- `apps/web/src/components/chat/AwaitingApprovalBlock.tsx` — UI 渲染诊断块（注：跟 qa-benchmark worktree 已有的同名组件可能冲突，未来 merge 时要协调）

修改：
- `apps/backend/app/core/config.py` (+4) — 4 个新 settings (`failure_diagnosis_enabled` / `_timeout_seconds` / `_max_events` / `_keyfile_head_chars`)
- `apps/backend/app/core/enums.py` (+1) — `FAILURE_DIAGNOSIS_GENERATED` event type
- `apps/backend/app/models/task.py` (+2) — `failure_diagnosis` 字段 doc
- `apps/backend/app/orchestrator/service.py` (+20) — `_mark_awaiting_approval` + `_mark_task_failed` 加 hook
- `apps/web/src/components/chat/MessageList.tsx` (+3) — render
- `apps/web/src/components/tasks/ApprovalPanel.tsx` (+4) — render
- `apps/web/src/styles.css` (+66) — confidence badge + diagnosis block styling
- `SESSION_HANDOFF.md` (+36) — codex 自己写的 manifest

**没做的（开新 stage 跟踪）:**
- 用户审 codex 的代码（最关键）
- commit 到 feat/failure-diagnosis 分支
- 考虑跟 qa-benchmark worktree 里的 `AwaitingApprovalBlock.tsx` 协调（这俩是独立实现的同名组件）
- 真实 task 上验证诊断生效（需要 backend 跑起来 + 触发一次失败）
- frontend 验证（codex 报告 npm 不能 lstat 非 ASCII 路径，跟之前 HandymanApp 撞同一坑）

**Lesson:**
1. **Codex 比预期快**：medium reasoning + 250 行 spec 跑 22 分钟，不是 60 分钟。下次估时调低
2. **Spec template "Do NOT git commit" 子句**：codex 严格遵守，留 dirty。这是对的，让 Claude 把 review 关
3. **Pre-existing test failures 报告了原因**：codex 区分了"我引入的"和"撞到环境的"——这是 spec 没明示但 codex 主动做的好事
4. **Worktree 沙箱有真实约束**：codex 不能创建 git tag（git common dir 写不进），不能跑 `claude` CLI（npm cache 沙箱外）。这俩限制要 spec 时考虑
5. **`AwaitingApprovalBlock.tsx` 撞名警示**：未来 merge feat/failure-diagnosis + feat/chat-approval-ux 时会有 component 冲突。下次写 spec 时应先 grep 全 worktree

**Phase 6 进度**：1/6 → **2/6**（T-PIPELINE-REPAIR-CAP ✅ + T-FAILURE-DIAGNOSIS ✅ pending review）。如果 T-CHAT-APPROVAL-UX 也合，就是 3/6。

---

### Stage 5 — Roadmap Phase 3 重排：CC agentic 优先，AST chunking 降级

**Open:** 2026-04-28 14:25 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律) → 影响 **Phase 3** 整体推进路径
**Timebox:** 10 分钟（实际：12 分钟）
**Trigger:** 用户挑战 "AST chunking 是否值得做"，理由："代码库小，CC 直接 grep 一遍更快 ROI 更高"。重新评估发现：
- 实测 KB 几十文件，CC 全库 ripgrep ~100ms，单文件 Read ~200-500ms，3 轮 agent ≈ 6-10s（**比当前 RAG 13-18s 还快**）
- AST chunking 只解决 A/B 档部分；C/D 档（多跳）完全不沾
- CC grep mode 能解决 A-05 / D-01 / D-04 等大半 baseline 失败题
- AST 工程量 ~300 行 / CC ~500 行 — 多 200 行换得 D 档真破局
- 用户明示 provider chain：claude_code CLI → codex CLI → minimax（**不要 anthropic**）

#### 步骤
- 14:25 entry 写入 STAGE_LOG（本条）
- 14:28 编辑 `docs/release-roadmap.md`：Phase 3.0 重写为"CC agentic 检索"；原 AST chunking 移到"3.0-fallback"段；依赖图 + Phase 3.5 + 4 处其它引用同步修改
- 14:35 写新 spec `docs/ai/tasks/T-KB-CC-AGENTIC-RETRIEVAL.md`（318 行，SPEC TEMPLATE v2）：CC tool wrapper + agent loop + 用户指定的 provider chain (claude_code → codex → minimax，不含 anthropic) + 19 个测试用例 + EvidenceItem mapping + 配置 + 失败模式表

#### Close 摘要
**Close:** 2026-04-28 14:37
**结果:** Roadmap Phase 3 路径已更新；新 spec 已写就绪可 dispatch。原 T-KB-AST-CHUNKING 保留但优先级降为 P2（fallback 方案）。

**产出文件（在主工作树 `docs/ops-strategic-specs-2026-04-28` 分支）:**
- `docs/release-roadmap.md`（5 处编辑：Apr-26 update 段落 / Phase 3.0 整段重写 / 新增 3.0-fallback 段 / Phase 3.5 备注 / 依赖图）
- `docs/ai/tasks/T-KB-CC-AGENTIC-RETRIEVAL.md`（+318 新文件）

**没做的:**
- 没动 T-KB-AST-CHUNKING 的 spec 内容（status 字面没改 — 但 roadmap 已经把它降级为 fallback，下次有人读 spec 应通过 roadmap 知道优先级变了）
- 没 commit 这次改动（一会儿 Stage 6 一起 commit）
- 没真 dispatch CC agentic spec（用户决定）

**Lesson:**
1. **用户的 architectural challenge 很有价值** — 我原方案被推翻是好事，避免了 300 行白做工
2. **小代码库 + agentic** 是个被低估的组合 — RAG 本来是为大语料库设计的，几十文件用 RAG 是杀鸡用牛刀
3. **provider chain 写进 spec 的 Background 段非常关键** — 否则 codex 实现时会用默认 chain（含 anthropic），跟用户意图相反

**Phase 3 路径变化**：
- 原：Phase 3.0 AST chunking 硬前置 → 3.1 query → 3.2 引用链 → 3.3 多路融合 → 3.4 rerank → 3.5 预索引
- 新：Phase 3.0 **CC agentic 检索** 硬前置 → 3.1 query → 3.2 引用链 ✅ → 3.3 hybrid 多 source 融合（含 cards / FTS5 / CC 整合）→ 3.4 rerank → 3.5 预索引（CC 路径下大部分不需要）
- AST chunking 保留为 3.0-fallback，仅在大代码库或 CC 不可用时启用




