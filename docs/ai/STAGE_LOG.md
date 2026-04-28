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
**Status:** OPEN
**Layer:** L1 (流程纪律)
**Timebox:** 5 分钟
**Trigger:** 把今天加的 stage discipline + phase 更新落到 git，避免下次 session 拿不到。继续在已有 `docs/ops-strategic-specs-2026-04-28` 分支上加 commit。

#### 步骤
- 13:30 entry 写入 STAGE_LOG（本条）


