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

---

### Stage 6 — Commit failure-diagnosis + roadmap revisions

**Open / Close:** 2026-04-28 14:42 → 14:46 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律)
**Trigger:** 用户回 "commit"，把 Stage 4 + Stage 5 的成果都落 git。

#### 步骤
- 14:42 commit 1 在 `D:/项目/ops-worktrees/failure-diagnosis`（feat/failure-diagnosis 分支）：`31bb852 feat(diagnosis): T-FAILURE-DIAGNOSIS — auto root-cause LLM step on awaiting_approval / failed`（13 文件 +1259 −1）
- 14:45 commit 2 在主工作树（docs/ops-strategic-specs-2026-04-28 分支）：`74d9096 docs: pivot Phase 3.0 to CC agentic retrieval; add T-KB-CC-AGENTIC-RETRIEVAL spec`（3 文件 +527 −19）

#### Close 摘要
**Close:** 14:46
**结果:** 两个独立 commit 落到对应 feature 分支，**未 push** / **未 merge**（用户决定）。
**Lesson:** commit per worktree 干净 — 分支不混。codex 工作放它自己的分支，docs/spec 改动放 docs 分支。

---

### Stage 7 — Dispatch T-KB-CC-AGENTIC-RETRIEVAL 到 codex

**Open:** 2026-04-28 14:48 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L2 (架构) → 推进 **Phase 3.0**（**实现完成**）
**Timebox:** 45 分钟（实际：codex 跑 ~14 分钟 + commit 2 分钟）
**Trigger:** 用户回 "go"。Spec 已写完 (T-KB-CC-AGENTIC-RETRIEVAL.md, 318 行)，工程量 ~500 行 + 19 测试。Phase 3.0 真正的实现 step。

#### 步骤
- 14:48 entry 写入 STAGE_LOG（本条）
- 14:50 创建 worktree `D:/项目/ops-worktrees/cc-agentic` + branch `feat/kb-cc-agentic` from `checkpoint/pre-reclassify`
- 14:51 dispatch codex（PID 46807，gpt-5.5，medium reasoning，full-auto）
- 15:05 codex 完工 — 19/19 targeted test pass，227,239 tokens 用了
- 15:07 commit `6797098 feat(kb): T-KB-CC-AGENTIC-RETRIEVAL — CC CLI as primary RAG retrieval`（9 文件 +1252 −1）

#### Close 摘要
**Close:** 2026-04-28 15:08
**结果:** Phase 3.0 实现层完工。CC agentic 检索 + RAG fallback + provider chain (claude_code → codex → minimax，**不含 anthropic** 跟用户 directive 一致) 全部落地。代码 commit 在 `feat/kb-cc-agentic` 分支独立干净。

**产出文件（在 `feat/kb-cc-agentic` 分支）:**
新建：
- `apps/backend/app/services/cc_agent.py` (+237) — Glob/Grep/Read CC tool wrappers
- `apps/backend/app/services/cc_agent_loop.py` (+324) — ReAct agent loop + provider chain dispatch + budget
- `apps/backend/app/services/cc_agent_prompts.py` (+53) — 决策 prompt 模板
- `apps/backend/tests/services/test_cc_agent.py` (+112) — tool wrapper 测试
- `apps/backend/tests/services/test_cc_agent_loop.py` (+247) — agent loop 集成测试

修改：
- `apps/backend/app/core/config.py` (+11) — 6 个 cc_agent_* settings + provider_chain default
- `apps/backend/app/services/knowledge.py` (+235) — `KnowledgeService.retrieve()` CC-first / RAG-fallback
- `.gitignore` (+1)
- `SESSION_HANDOFF.md` (+33 codex 自己写的 manifest)

**没做的（开新 stage 跟踪）:**
- **真实 smoke test**：起 backend → 触发 firebase auth question → 验证 evidence 真带 cc_read source + 含 handleLogin 函数体（spec 验收 4）
- **Re-baseline**：跑 qa benchmark 看 27.06% 是不是真涨（理论 A 35→60+, B 11→35+, D 22→40+）。需要先把 qa-benchmark 那条 commit 也整合进同一个分支。
- merge 到 checkpoint / main —— 用户决定

**Lesson:**
1. **Codex 比预期快**：500 行 spec 实际 14 分钟跑完。下次 medium effort 估 15-25 分钟而不是 30-50 分钟
2. **Spec Background 段写硬约束很关键** — provider chain "no anthropic" 这种 user-specific directive 必须在 Background 段写明，否则 codex 实现时会用默认 chain（含 anthropic）
3. **Worktree 隔离让 dispatch 完全无副作用** — 失败也不污染 checkpoint / main

**Phase 3 进度**：
- 3.0 CC agentic 实现 ✅（待 smoke test + re-baseline 验证）
- 3.1 query 处理 — 未启
- 3.2 引用链 ✅ 已合 checkpoint
- 3.3 hybrid 多 source 融合 — 未启
- 3.4 rerank — 未启
- 3.5 预索引（CC 路径下大部分不需要）— 未启

---

### Stage 8 — Smoke + Re-baseline CC agent against handyman KB

**Open:** 2026-04-28 15:12 (UTC+10) by Claude
**Status:** CLOSED-DONE (with critical findings)
**Layer:** L1（流程纪律 / 测量验证） → 完成 **Phase 1 reset baseline 尝试 + Phase 3.0 验证**
**Timebox:** 35 分钟（实际：~80 分钟，因为 baseline 跑了 64 分钟超时一大半）
**Trigger:** 用户 "全跑"。Phase 3.0 实现完工但没验证；同时 baseline 27.06% 是基于 RAG 链路测的，需要在 CC 链路上重置 baseline 才能后续 PR 引用。

#### 步骤
- 15:12 entry 写入 STAGE_LOG（本条）
- 15:13 merge `feat/qa-benchmark-integration` 到 `feat/kb-cc-agentic`（commit `2576033`）
- 15:18 copy main tree DB + .env 到 cc-agentic worktree（避免重新 sync KB）
- 15:39 启 backend on port 8003
- 15:40 dispatch benchmark runner（PID 46934）：`--judge-mode auto --judge-samples 3 --backend-url http://127.0.0.1:8003`
- 15:40 → 16:47 runner 跑了 64 分钟（vs 预估 17-25 分钟）

#### Close 摘要
**Close:** 2026-04-28 16:50
**结果:** **CC mode mean 17.82 vs 旧 RAG 27.06，整体 -9.24 退步**。但**根因不是 quality**：完成的 14 题质量显著超 RAG（A 完成 mean 47.5 / B 24.4 / C 56.2 / **D 54 max 78**——D-tier 真破局了）。问题是 **20/34 题撞 runner 120s timeout**。

**完整数字**：

| Tier | n | 完成 | 超时 | mean(全部) | mean(完成) | max |
|---|---|---|---|---|---|---|
| A | 10 | 2 | 8 | 9.50 | 47.50 | 55 |
| B | 10 | 5 | 5 | 12.20 | 24.40 | 70 |
| C | 8 | 5 | 3 | 35.12 | 56.20 | 70 |
| D | 6 | 2 | 4 | 18.00 | 54.00 | **78** |

**对比旧 baseline**（RAG, multi-sample N=3）：A=35 / B=11 / C=41 / D=22 / mean=27.06

**慢的具体表现**：
- 每题平均 wall-clock：112.6s
- 所有 timeout 题精确停在 120.0s
- spec 写的 `cc_agent_overall_timeout_s = 30.0`，但每题实际 112s **差 80s 找不到去哪了**

**产出文件:**
- `D:/项目/ops-worktrees/cc-agentic/apps/backend/tests/benchmarks/runs/qa-run-20260428T053956Z.jsonl`（完整 baseline JSONL）

**没做的（Stage 9 跟踪）:**
- 没诊断到底慢在哪一层（agent / synthesizer / CC subprocess 冷启动 / judge）
- 没写新 baseline 报告（数据有但没格式化成 markdown）
- backend on port 8003 仍在跑（留着给 Stage 9 用）

**Lesson:**
1. **测量先于优化** 这条原则今天直接拿到回报：如果不跑 baseline，Phase 3.0 代码合 main 之后才会发现整体退步
2. **CC mode 是真有用** —— 完成的题 D-tier max 78（之前 RAG 撑死 22），完全是范式级别的提升
3. **但操作性是个真问题** —— "代码能跑测试都过" 跟 "在 baseline 时间预算内能跑完" 之间隔着一大段
4. **runner timeout vs agent budget vs subprocess cold start** 这三层时间预算有没对齐 = 关键技术债

---

### Stage 9 — Instrument → diagnose → J+P fix → re-baseline 49.65

**Open:** 2026-04-28 16:52 (UTC+10) by Claude
**Status:** CLOSED-DONE — **Phase 3.0 验证 PASS（mean +22.59）**
**Layer:** L4 (UX / 可观测) → 推进 **Phase 1 baseline lock** + **Phase 3.0 验证**
**Timebox:** 原 30 分钟（实际 ~3 小时，含 71min baseline 跑 + 调试 + 报告）
**Trigger:** 用户回 "go o"。CC mode 退步是 timeout 主导的，不是 quality 差。

#### 步骤
- 16:52 entry 写入 STAGE_LOG（本条）
- 17:00 instrument：解析 OpenTelemetry spans 发现 `knowledge.search` tool 跑 112.61s；agent 守 30s OK，**80 秒在 `_synthesize_or_template` LLM 调用**
- 17:10 进一步 instrument：单 citation snippet 高达 9761 字符（`HandymanVerification.js` 全文件 dump 进 prompt）。`knowledge_synthesis._format_evidence` 已在按 `knowledge_synthesis_max_snippet_chars` 截断（默认 6000），但 4 × 6000 = 24KB prompt 还是太大
- 17:20 写 spec `T-BENCH-RUNNER-TIMEOUT-FLAG`，dispatch codex（low effort）
- 17:25 codex 完工（commit `7a34c37`）：runner 加 `--question-timeout` flag，默认 120s → 240s
- 17:28 set `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=3000` in worktree `.env`（P，纯 .env 覆盖无源码改）
- 17:30 重启 backend on 8003 让吃新 .env
- 17:30 → 19:42 跑 CC baseline 71min
- 19:42 解析新 artifact `qa-run-20260428T093042Z.jsonl`：**mean 49.65 vs 旧 RAG 27.06 = +22.59**
- 19:50 写 baseline 报告 `docs/ai/benchmarks/qa-baseline-2026-04-28.md` + commit `c090419`

#### Close 摘要
**Close:** 2026-04-28 19:55
**结果:** **Phase 1 baseline lock + Phase 3.0 验证 PASS**。CC agentic mode 击败单路 RAG 22.59 分。新 baseline 报告 commit 在 `feat/kb-cc-agentic` (`c090419`)。

**Phase 3.0 真实数据**：

| Tier | 旧 RAG | 第一次 CC | NEW J+P | vs RAG |
|---|---|---|---|---|
| A | 35 | 9.5 | **56.50** | +21.50 |
| B | 11 | 12.2 | **37.60** | +26.60 |
| C | 41 | 35.1 | **70.62** | **+29.62** ★ |
| D | 22 | 18 | **30.33** | +8.33 |
| **mean** | **27.06** | 17.82 | **49.65** | **+22.59** |

**Acceptance vs Stage 9 targets**：

| 指标 | 目标 | 实际 | 结果 |
|---|---|---|---|
| Completion | ≥28/34 | **34/34** | ✅ |
| Mean | ≥35 | **49.65** | ✅ |
| D-tier | ≥40 | 30.33 | ❌ |
| Wall-clock | ≤45min | 71.1min | ❌ |

**产出文件（在 `feat/kb-cc-agentic` 分支）:**
- `docs/ai/tasks/T-BENCH-RUNNER-TIMEOUT-FLAG.md`（新）— J 的 spec
- `apps/backend/scripts/run_qa_benchmark.py`（+19 −3）— `--question-timeout` flag，默认 240s
- `apps/backend/.env`（worktree-local，未 commit）— `OPS_AGENT_KNOWLEDGE_SYNTHESIS_MAX_SNIPPET_CHARS=3000`
- `apps/backend/tests/benchmarks/runs/qa-run-20260428T093042Z.jsonl`（新）— baseline JSONL
- `docs/ai/benchmarks/qa-baseline-2026-04-28.md`（新）— 完整 baseline 报告 + acceptance check + 后续 ticket 提案

**Phase 1 / Phase 3.0 状态**：
- Phase 1 测量地基 ✅ 完工（baseline 49.65 锁定，作为 PR gate forcing function）
- Phase 3.0 CC agentic ✅ 验证 PASS（+22.59 mean，C-tier 翻倍）

**没做的（开新 ticket 跟踪）:**
- `T-KB-EVIDENCE-TIER-CAP` — D-tier cap=6000 / A/B/C cap=3000 让 D 重回 40+
- `T-KB-CLI-POOL` — pre-spawn `claude` 进程，省 5s/call 冷启动 → runtime 71min → ~50min
- `T-KB-HYBRID-RAG-FAST-PATH` — A/B-tier 走 RAG 13-18s；C/D 走 CC → runtime 71min → ~35min
- merge `feat/kb-cc-agentic` 到 checkpoint —— 用户决定（这一波是大块改动，建议至少 review 一下）
- main vs checkpoint 分叉的 meta 整合 —— 还在挂

**Lesson:**
1. **诊断优先于 brute-force tuning** —— 直接 J+K 是猜；instrument 5 分钟才发现 80s 的真正去处是 synthesis 而不是 agent
2. **现成 config 比加新 config 优**：`knowledge_synthesis_max_snippet_chars` 已经存在，只是默认 6000 太松；纯 .env 调 3000 就够，不用动代码
3. **D-tier 的 trade-off** 数据上验证：cap 帮 ABC 但伤 D，因为多跳问题需要长 context。tier-aware cap 这下有数据支持
4. **C-tier +29.62 是最大惊喜**：CC 多轮 grep+read 真的能解决 cross-file 问题，从 41 → 70.62 接近翻倍。这条路对了
5. **runtime 比预估慢**：先期估的 "synthesis 80→40s" 实际只是 80→60s。下次估时按 `cap_ratio × 0.5` 折扣

**Phase 3 进度刷新**：
- 3.0 CC agentic ✅ 实现 + ✅ 验证 PASS（+22.59）
- 3.1 query 处理 — 未启
- 3.2 引用链 ✅
- 3.3 hybrid 多 source 融合 — 未启
- 3.4 rerank — 未启
- 3.5 预索引 — 未启

---

### Stage 10 — Execute T-MERGE-CC-AGENTIC-INTO-MAIN

**Open:** 2026-04-28 ~20:30 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (流程纪律) → Phase AF 成果合并到 checkpoint 主线 ✅
**Timebox:** 30 分钟（实际：~15 分钟）
**Trigger:** 用户回 "继续 / 直接全程"。Phase AF 的 49.65 baseline 还在 worktree，必须合到 checkpoint 否则下个 ticket 拿不到这个起点。

#### 步骤
- 20:30 entry 写入 STAGE_LOG（本条）
- 20:31 创建 4 个 safety tag：`pre-merge/T-MERGE-CC-AGENTIC-2026-04-28-2030-{docs,failure-diagnosis,cc-agentic,checkpoint}`
- 20:33 `git checkout checkpoint/pre-reclassify`
- 20:34 **Step 1**: merge `docs/ops-strategic-specs-2026-04-28` → clean，17 文件 +3685 −2，merge `b6830c5`
- 20:35 **Step 2**: merge `feat/failure-diagnosis` → 1 conflict (SESSION_HANDOFF.md), keep ours, merge `c28b868`
- 20:38 **Step 3**: merge `feat/kb-cc-agentic` → 2 conflicts (SESSION_HANDOFF + T-QA-ACCURACY-BENCHMARK add/add), keep ours, merge `9cffe4b`
- 20:40 verify: `compileall` clean ✅ / **28/28 focused test pass** (cc_agent 7 + cc_agent_loop 12 + failure_diagnosis 9) ✅
- 20:42 cleanup: 删 3 已合并 branch + 1 worktree；保留 `feat/kb-cc-agentic` worktree (含 `.env` override = baseline reproducibility 锚点)

#### Close 摘要
**Close:** 2026-04-28 20:45
**结果:** **3 步 merge 全成功**。`checkpoint/pre-reclassify` HEAD 现在 `9cffe4b`，包含 Phase AF 全部成果（CC agentic + benchmark + diagnosis + STAGE_LOG + Phase AA-AF + 8 ticket specs）。

**Merge 链路（在 checkpoint）:**
```
9cffe4b  merge: feat/kb-cc-agentic — Phase 3.0 CC + qa-benchmark + 49.65 baseline
c28b868  merge: feat/failure-diagnosis — Phase 6 +1
b6830c5  merge: docs/ops-strategic-specs-2026-04-28 — Phase AF + STAGE_LOG + specs
a3f0cf4  Merge branch 'feat/repair-cap-impl' (pre-existing)
```

**Safety net**: 4 个 `pre-merge/...` tag 留着，任何时候 `git reset --hard <tag>` 可回滚。

**没做的:**
- merge `checkpoint/pre-reclassify` → `main` (单独决策)
- 清理另 9 个 stale worktree (Stage 1 deferred)
- backend smoke test 验证 49.65 在 checkpoint 上复现 (可选)

**Lesson:**
1. Cherry-pick + 多次 merge 比想象中干净（evidence schema 模块化好，无耦合）
2. 3 conflicts 全在 SESSION_HANDOFF / 旧版 spec — `git checkout --ours` 一招通关
3. Safety tags 批量创建是好习惯，5 秒成本，但回滚自由度极大
4. Worktree 删除卡时直接 `rm -rf` + `git worktree prune`

**Phase 总览（merge 后）**：
- Phase 1 测量地基 ✅ **on checkpoint**
- Phase 3.0 CC agentic ✅ **on checkpoint**
- Phase 6 治理 UX 🟡 2/6（diagnosis + repair-cap on checkpoint）
- Phase 4 防御矩阵 ✅
- 其它未启

---

### Stage 11 — Dispatch T-KB-EVIDENCE-TIER-CAP (D-tier 重回 40+)

**Open:** 2026-04-28 ~20:50 (UTC+10) by Claude
**Status:** CLOSED-PARTIAL
**Layer:** L2 (架构) → Phase 3.0 修订（部分推进）
**Timebox:** ~100 分钟（实际：~3 小时）
**Trigger:** D-tier 30.33 < 40 target → tier-aware cap 应回血。

#### 步骤摘要
- dispatch codex → commit `9be1ccf` on `feat/kb-evidence-tier-cap`（5 文件 +182 −18，17/17 焦点测试过）
- benchmark `--judge-mode auto`（错误，应该 pin）→ artifact `qa-run-20260428T135735Z.jsonl`

#### Close 摘要（PARTIAL）
**Close:** 2026-04-29 ~01:30
**结果:**

| Tier | PREV(J+P) | NEW(v1) | Δ |
|---|---|---|---|
| A | 56.50 | 53.50 | -3.00 |
| B | 37.60 | 38.80 | +1.20 |
| C | **70.62** | **54.88** | **-15.75 ★** |
| D | 30.33 | **41.00** | **+10.67 ✅** |
| mean | 49.65 | 47.29 | -2.36 |

**D 命中 40 target ✅，但 C 大幅退步，整体 mean 退步 -2.36**。

**根因（多因混合）:**
1. Keyword 分类器漏 C-tier ("which components use X" 类没匹配到 multi-hop 关键词)
2. Judge 不一致（baseline `auto` 实际 claude_code-only；这次 `auto` fallback 到 codex/minimax，judge variance ~5-10 pt/题）
3. 题目级 variance + synthesis evidence selection 噪音

**判定:**
- `9be1ccf` 是 **experimental，不接受为新基线**
- 49.65 仍是 reference baseline（但需重测 with pinned judge 确认）
- 机制本身（tier-aware cap）方向对，但**实现需 invert 默认**：default 6000，narrow 3000 only on strong locate signal
- benchmark 必须 pin judge + pin synthesis provider

**Lesson:**
1. **一次实验只改一个变量**：这次 cap policy + judge policy 同时变，对比无意义
2. **invert default = conservative bias**：不能信任分类器穷尽所有 query 模式
3. **benchmark methodology 必须先文档化再执行**：判 judge auto vs pin、synthesis provider lock、模型版本 stamp 都要预先写死

**没做的（→ Stage 12）:**
- 不 merge `feat/kb-evidence-tier-cap` 到 checkpoint
- 不更新 phase-summary（partial 不够格写 Phase 入口）

---

### Stage 12 — T-KB-EVIDENCE-TIER-CAP v2 (binary + invert + pinned)

**Open:** 2026-04-29 ~01:35 (UTC+10) by Claude
**Status:** OPEN
**Layer:** L2 (架构) → Phase 3.0 完成
**Timebox:** ~150 分钟（spec 15 + dispatch 15 + benchmark 90 + analyze 30；并行 metrics spec 写）
**Trigger:** Stage 11 partial。机制对，policy 错（默认窄）+ judge 不锁。

#### 步骤
- 01:35 entry 写入 STAGE_LOG（本条）
- ~03:00 codex 实施 v2 完成（5 文件：config / schemas/knowledge / services/knowledge / services/knowledge_synthesis / tests/services/test_knowledge_synthesis）。第 6 文件 baseline-doc 被 codex 误改（把 cap=3000 改成 0，会污染 49.65 历史复现），手动 revert。
- ~03:05 sanity：`compileall app` clean，`pytest tests/services/test_knowledge_synthesis.py` 17/17 pass（696s，~12 min）。
- ~03:10 老 8004 backend (PID 29080, v1 commit) kill，从 v2 worktree 重启 backend on 8004 (PID 48209)。`/health` 返回 db_connected=true。
- ~03:15 写 `T-RUN-BENCH-V2.md` 派 codex 跑 benchmark（PID 48221，`--judge-mode claude_code` PINNED，`--judge-samples 3`，`--question-timeout 240`）。预计 ~80-100 分钟。
- 并行：写 `docs/ai/specs/llm-metrics-instrumentation.md`（T-LLM-METRICS，P2，event.payload_json + 现有 LlmUsage，无新表），写 release-roadmap.md Phase 3.3 校准（v1/v2 经验教训：先 binary 再多档；channel weights 必须有 metrics 数据再定；CC 单路已够好，hybrid fast-path 推迟到 3.5）。
- 等 benchmark 出数据 → 决策 commit / merge / 是否更新 baseline。

#### Close 摘要

- **Status:** CLOSED-DROPPED
- **结果:** v2 政策没赢，且实际上没真正运行。
- **bench 灾难 + 抢救:** 第一次 v2 bench 在 codex sandbox 跑（PID 48221），结果 0/34 全 0 分，`task_status=runner_error`。Root cause: `CodexSandboxUsers` Windows 用户对 `%LOCALAPPDATA%\npm-cache` 没写权限 → judge 调 `npx claude` 时 npm EPERM → script `except Exception` 把 judge 故障误标成 task 故障 → answer 被丢、judge 回退 "rule"、rule 对空答案打 0。Backend synthesis 实际上是成功的（DB 里有 34 个真实 answer + citations）。
- **抢救方案:** 派 codex 写 `apps/backend/scripts/rejudge_run.py`（offline rejudge：从 backend `GET /api/tasks/{id}` 拉 `latest_result_json.result.answer` + citations，用 `KeypointJudge(claude_code, samples=3)` 重新打分），从 Tomonkyo shell 跑（绕开 sandbox EPERM）。修了 codex 写的脚本里一处 canonical-citation reuse bug（`task_answer` 之前没用 bench 的 `extract_answer_and_citations` 3-tuple）。耗时 ~9 分钟，34/34 valid。
- **真实 v2 数字（rejudge claude_code, samples=3）:**
  - A=55.00（vs baseline 56.50, **-1.50**, 平）
  - B=44.80（vs 37.60, **+7.20** ✓）
  - C=58.12（vs 70.62, **-12.50** ✗ 距 target 65 短 6.88）
  - D=39.67（vs 30.33, **+9.34** ✓ 距 target 40 短 0.33，基本平）
  - **Mean=50.03（vs 49.65, +0.38 — 统计噪声）**
- **Smoking gun:** 跑 5 分钟 unit test 单独喂 34 题给 `_detect_locate_signal()`，只有 **2/34** match（A-01, A-07）。但 trace 里 34/34 都是 `locate_detected=False, cap_used=6000`。差异原因：planner 给 synthesis 的 `query` 不是用户原 `request_text`，是 planner 改写后的字符串（且 query_rewrite 还会 +12 tokens），detector 的 `len > 60: return False` 把这些都筛掉。
- **结论:**
  1. v2 政策实际上等于"全局 cap=6000"，从未触发 narrow。
  2. 即使修了 upstream-query 让 detector 看到原问题，也只 2/34 命中 narrow，对整体 mean 影响极小。
  3. cap=6000 vs cap=3000 是 per-tier trade-off（B/D 涨 7-9，C 跌 12），mean 持平，没赢家。
  4. **tier-aware via regex/keyword classifier 不是有用的杠杆**，drop 这个 workstream。
- **没做的:**
  - 不 commit v2，不 merge `feat/kb-evidence-tier-cap` 到 checkpoint（branch 留作 history reference）。
  - 不动 `qa-baseline-2026-04-28.md`（49.65 reference 保持）。
- **产出文件（已 copy 到 checkpoint）:**
  - `apps/backend/scripts/rejudge_run.py` — offline rejudge 工具
  - `apps/backend/tests/benchmarks/runs/qa-run-20260429T011959Z.jsonl` — v2 原 bench artifact（judge 灾难版）
  - `apps/backend/tests/benchmarks/runs/qa-run-20260429T011959Z-rejudged.jsonl` — v2 真实 rejudge 数据
  - `apps/backend/tests/benchmarks/runs/qa-run-20260428T135735Z.jsonl` — v1 (Stage 11) 原 bench
  - `docs/ai/tasks/T-REJUDGE-RUN.md` — rejudge 工具 spec
- **Lesson:**
  1. **harness 设计 bug 比策略 bug 危险**：`except Exception` 把 infra 故障翻译成模型故障；harness 必须先持久化 answer 再 judge，且区分 `synthesis_status` / `judge_status` / `score_status`（codex 的洞察）。→ T-BENCH-HARNESS-RESILIENCE 记录待办。
  2. **classifier 的输入要和你测试的输入一致**：v2 unit test 用 raw question，但 runtime detector 看的是 planner 改写后的 query。这俩不一致就别假装是同一个分类器。
  3. **codex sandbox 用户隔离会破 npm/pip cache 等用户级状态**：bench/CLI 编排不要在 codex 沙箱跑，应在 Tomonkyo shell；codex 沙箱专门用于 codegen 和 risky edits（memory rule 待更新）。
  4. **measurement 投资回报递减时及时退出**：v1+v2 两个 stage 投了 ~5 小时换一个"mean +0.38"，还是统计噪声。再迭代第三次去修 1-line classifier bug 不会改变 mean 不动的事实。

---

### Stage 13 — Bench infra triage + offline rejudge + Stage 12 closure

**Open:** 2026-04-29 ~03:30 (UTC+10) by Claude
**Status:** CLOSED-DONE
**Layer:** L1 (调试)
**Timebox:** ~120 分钟实际
**Trigger:** Stage 12 v2 bench 0/34 灾难需要 root-cause + 抢救数据。

#### 步骤
- 03:30 root-cause: `task_status=runner_error` + `npm error EPERM` 全部 34 题；ACL 检查发现 `CodexSandboxUsers` 对 `%LOCALAPPDATA%\npm-cache\_cacache\tmp` 只读。从 Tomonkyo shell `npx --no-install -y @anthropic-ai/claude-code --version` 返 2.1.122 OK，证明问题在 sandbox 用户。
- 03:35 verify: backend 实际 synthesis 成功，curl A-01 task 拿到完整 answer + 4 个 citations + reviewer approved。bench 失败 100% 在 script 端 judge 步骤。
- 03:40 与 codex pivot 讨论（PID 48418）：4 问 → codex 给出 4 答，最关键的 d 点："这是 harness 设计 bug，不是 npm 问题；harness 应在 judge 前持久化 answer，artifact 应区分 3 个 status 字段"。我无实质反驳，采纳。
- 03:45 写 `T-REJUDGE-RUN.md` 派 codex（PID 48455）写 `rejudge_run.py`（low effort）。codex sandbox 跑不了 `python` 所以做不了 dry-run，由 Tomonkyo shell 接手。
- 04:00 dry-run 暴露 1 bug：rejudge 用了自己的 `task_answer` 而没复用 bench 的 `extract_answer_and_citations`，导致 citation 是 display-form 不是 canonical → cp 全 0。手动 patch（3 行 edit），rule dry-run 复跑，cp 正确。
- 04:05 启 claude_code rejudge（PID 48592, samples=3, 真 PINNED 因为 Tomonkyo 用户能用 npm cache），9 分钟跑完 34 题。
- 04:14 数字到位（见 Stage 12 close 摘要）。
- 04:20 与 codex 讨论结果（PID 48604, 4 问 4 答），收敛到 Option A（revert v2，drop tier-cap workstream，pivot FTS5/cards）。
- 04:30 5 分钟 unit test：把 dataset 34 题喂 `_detect_locate_signal`，只有 2 题（A-01/A-07）match。结合 trace 全 False，证实 detector 看到的不是 raw question。
- 04:40 用户拍板 Option B（revert 到 pre-v1 baseline，不留 v1 keyword classifier）。
- 04:45 worktree `git checkout HEAD --` 5 个 v2 文件 → 回到 v1 commit `9be1ccf`。checkpoint/pre-reclassify 本来就没 v1（feat 分支独立），所以源代码已是 baseline 状态。
- 04:50 把 rejudge 工具 + 3 个 measurement 工艺品 + spec 复制到 checkpoint 准备 docs commit。

#### Close 摘要
- **Status:** CLOSED-DONE
- **结果:** Stage 12 灾难抢救成功，v2 政策被诚实评估并 drop。
- **产出:** `rejudge_run.py` 落盘 checkpoint（未来 bench infra 故障可复用）；3 个 measurement artifact 留 history；Stage 12 close 摘要 + 4 条 lesson。
- **没做的:** memory rule 拆分、T-BENCH-HARNESS-RESILIENCE spec、Plan B doc updates（D2/D4/D5/D6）、roadmap Phase 3.3 calibration 收尾——都列入 Stage 14 待办。
- **Lesson:** harness resilience 比 policy 优化优先级高。一个 0/34 的 bench 比 100 个 baseline 偏移更难诊断。














---

### Stage 14 — Bench harness hardening + Plan B from Stage 12 critique

**Open:** 2026-04-29 ~14:00 (UTC+10)
**Status:** CLOSED-DONE
**Commits:** `4783e20` (docs+spec) + `fe879ed` (impl) + `84035a8` (merge)

#### Close 摘要
- **Status:** CLOSED-DONE
- **结果:** 4 件全收。
  - B (memory rule split): codex sandbox vs Tomonkyo bash 写到 auto-memory
  - A (T-BENCH-HARNESS-RESILIENCE): strict pin / preflight / persist-before-judge / 3 status fields, 8 tests
  - C (Plan B docs): D-009 in DECISIONS.md, D2 conditional in roadmap, D4 visible-error pattern in T-LLM-METRICS spec
  - D (archive tag): `archive/kb-evidence-tier-cap-v1` at `9be1ccf`
- **产出:** harness 现在不撒谎；下次 bench 故障 30 min 内可定位（vs Stage 12 用了 3+ hr）。

---

### Stage 15 — T-KB-FTS5-INDEX (lexical retrieval substrate)

**Open:** 2026-04-29 ~15:00
**Status:** CLOSED-DONE (substrate verification, not quality lever)
**Commits:** `2561f13` feat + `1494743` bench + `0316e86` merge

#### Close 摘要
- **结果 (PINNED claude_code, 34/34 valid, 72.5 min):**
  - A=47.50 (-9.00 vs baseline 56.50) ← noisy CSS/build files no longer help
  - B=44.80 (+7.20)
  - C=59.71 (-10.91) ← same dataset coupling as A
  - D=42.78 (+12.45) ← first time D crossed historical 40 target
  - **Mean=48.75 (-0.90 vs 49.65 = noise)**
- **意图:** substrate work, not quality lever. Spec said "expected mean delta small". Verified.
- **诊断:** A/C 损失是 dataset 耦合（baseline 老 sync 留的 .css/build 文件意外帮 synthesis 命中 literal keypoint）；FTS5 候选更干净，synthesis 答案更聚焦反而漏字面 keypoint。Stage 16 cards 应能补回。
- **三个数共存:** 49.65 baseline (reference) / 48.75 FTS5 substrate / pending cards as new best.

---

### Stage 16 — T-KB-RAG-CARDS-OFFLINE (real quality breakthrough)

**Open:** 2026-04-29 ~16:00
**Status:** CLOSED-DONE (NEW BEST QUALITY CHECKPOINT)
**Commits:** `ffec317` feat + `f1b8418` bench + `c7ecfc8` merge

#### Close 摘要
- **结果 (PINNED claude_code, 34/34 valid, 73.6 min):**
  - A=60.00 (+3.50 vs baseline) ← recovered FTS5 dip
  - B=**56.40 (+18.80)** ← first time B-tier broke 50; cards directly addresses "how does X work"
  - C=70.25 (-0.37 = flat)
  - D=**45.67 (+15.34)** ← well past 40 target
  - **Mean=58.82 (+9.17 vs 49.65)** ← biggest single-stage lift since CC agentic +22.59
- **Card metadata** (commit `f1b8418` records for reproducibility):
  - 37 cards generated for hosteddashboard via MiniMax-M2.7
  - card_version: v1-card
  - dashboard repo HEAD at gen: `b60d6d8`
- **三个数共存 (locked into bench commit):**
  - 49.65 = historical reference baseline (Phase 3.0 CC agentic, 2026-04-28) — NOT replaced
  - 48.75 = FTS5 substrate verification (Stage 15)
  - **58.82 = current best quality checkpoint** (Stage 16 cards)
- **战略验证:** "B+D 是真杠杆，A 已近 dataset 上限，cards 是 lever" — 数据匹配预测。
- **Lesson:** structural changes (every-file LLM summary) > runtime tweaks (cap / prompt). 验证投资方向。

---

### Stage 17 — T-LLM-METRICS (observability)

**Open:** 2026-04-29 ~22:00
**Status:** CLOSED-DONE
**Commits:** `d6dabb3` feat + `58eb0d2` merge

#### 步骤
- spec 已存盘（Stage 14 写的），加 cards 作为第 6 个 call site
- codex impl: 6 call sites instrumented (synthesis, semantic_translator, planner, codegen, cc_agent, cards)
- smoke 暴露 2 bug：
  1. `event.task_id` NOT NULL 在老 DB 上 → LLM_CALL writes 失败
  2. record_llm_call 用 caller's session 但 FastAPI `get_db()` yield-then-close 不 commit → events 全丢
- T-LLM-METRICS-FIX-1 spec 写好派 codex 但 codex sandbox 在 worktree 路径上突然 PermissionDenied，0 changes
- 2 bug 我手 patch：(a) db.py 加幂等 SQLite rebuild migration（drop indexes / rename / metadata.create / copy / drop tmp）；(b) llm_telemetry 改 sibling-session pattern（`sessionmaker(bind=db.get_bind())`，自己 commit + close）
- 10/10 telemetry tests + 49/49 regression pass
- Live smoke: 1 search → metrics endpoint shows synthesis (n=1, p50=17s) + cc_agent (n=1, p50=5.8s), telemetry_failure_count=0, fallback_step_distribution{0:2}

#### Close 摘要
- **结果:** 6 call sites instrumented. `/api/metrics/llm-calls` 返回 by_purpose (n, p50_ms, p95_ms, success_rate, cache_hit_pct) + fallback_step_distribution + error_type_distribution + telemetry_failure_count.
- **Visible-error pattern 完整覆盖** (codex 之前 D4 critique 收纳)：counter + WARN log，没有 silent except-pass。
- **Sibling session 设计** 解决了双重问题：telemetry 写不依赖 caller commit + telemetry 失败不 poison caller transaction。
- **Lesson:** FastAPI `get_db()` 不 commit 是个常见 footgun；fire-and-forget telemetry 必须自带 session 或调用方明确 commit。

---

### Stage 18 — T-KB-HYBRID-FAST-PATH (DROPPED — Stage 13 wiring bug + CC infra noise)

**Open:** 2026-04-30 ~12:30
**Status:** CLOSED-DROPPED
**Commits:** 无 (per D-009: 不达验收不 commit)
**Branch:** `feat/kb-hybrid-fast-path` (uncommitted, worktree removed; branch label still at `58eb0d2`)

#### 步骤
- spec 写完（11+ tests, quality-preservation acceptance: mean ≥57, D≥43, runtime <60min）
- codex impl: 16/16 hybrid tests + 59/59 regression all pass
- Pre-bench smoke 3 queries：fast/full 路由都看着对（`disqualified:validated` 也触发了），但 cc_agent 已经 n=7 success_rate=0.5714 (3 errors / 1 timeout) — **infra warning 出现但忽略了**
- Full 34Q bench (PINNED claude_code, samples=3) 跑了 **109.9 min** (+36 vs cards 73.6) → 26/34 valid, 8 task_error/timeout (7 连续 B-tier + 1 A)
- Per-tier (valid only): A=53.33 / B=**34.00** / C=56.79 / D=43.33 / **mean=49.86**
- **未达 acceptance: mean -7, B -18, runtime +50 min**
- 与 codex 复盘 5 问 5 答，收敛到 "A 先短调查 → 大概率 C revert"
- 10 分钟分类发现 root cause: **`routing_query` 不是 user question, 是 planner-rewritten token list**
  - A-01 user: "Which file defines the admin login screen?" → routing_query: "admin login component"
  - B-01 user: "How does the login page validate credentials..." → routing_query: "login credential validation authentication"
  - regex 要 "Which file (defines|...)" 这种自然句，token list 全部 miss
  - **0/34 题 fast-path 实际触发**——hybrid 在 bench 期间根本没生效

#### Close 摘要
- **Status:** CLOSED-DROPPED
- **结果:** Hybrid 实现有 wiring bug（detector 看 rewritten query），bench 期间 0/34 真触发 fast-path。所谓"hybrid 让 bench 变慢"其实是 full-path + CC infra 不稳定的合力。
- **Stage 13 重演:** v2 tier-cap 死于同样 bug——detector 看的不是 user query。spec 里**明文写了** "must see request_text" + "Stage 13 lesson" warning，codex 还是接成了改写后的 query。**这条 lesson 必须升级**——下次 routing/classifier 类 spec 必须强制要求"集成测试断言 routing_query == request_text 字面字符串"。
- **CC infra 不稳:** smoke 已显示 cc_agent 43% 失败率（3 calls 中 2 CCDecisionError + 1 ReadTimeout），bench 把它放大成 8 task_error。这跟 hybrid 无关。
- **没做的:** 不 commit hybrid，不修 wiring 重跑（CC 不稳风险，且预期收益低）。
- **产出归档:**
  - `apps/backend/tests/benchmarks/runs/qa-run-20260430T032048Z-hybrid-DROPPED.jsonl` (失败 bench artifact, history)
  - `docs/ai/tasks/T-KB-HYBRID-FAST-PATH-DROPPED.md` (spec 留作 negative result reference)
  - branch `feat/kb-hybrid-fast-path` 留着（只是 label，没 commit），用 reflog 找路由实现的话能找回
- **Lessons:**
  1. **Routing/classifier 必有"看的是 user query"的集成测试断言**——unit test helper 工作 ≠ wiring 正确，Stage 13 已经验证了一次，今天又验证了一次
  2. **CC infra 失败率早期信号必须刹车**——smoke 时 cc_agent 43% 失败已经是危险信号，不应直接进 bench
  3. **Wall-clock 反向是核心反证**——一个"fast-path"让总 wall-clock 变长，前提已破，后续诊断只是确认而非翻案
  4. **Hybrid is higher-risk class than substrate/quality**——hybrid 改了执行政策（routing decision），cards/FTS5 只改了 retrieval substrate；前者一行 wiring bug 就能让所有 bench 走错路，后者最坏也只是某档掉分
- **下一步:** Stage 19 (扩 benchmark 34→60 验证 cards 泛化) — 与 hybrid 正交，不依赖 CC infra 稳定。
