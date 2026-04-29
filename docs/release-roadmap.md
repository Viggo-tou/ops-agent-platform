# 整合开发计划 —— 质量优先版（Roadmap to v1.0）

**Last updated:** 2026-04-26
**Principle:** 不追求日期。每个任务都做到"如果将来回头看不会后悔"的程度。顺序按依赖关系排，每一步给下一步铺地基。

不在 v1.0：多租户、公开部署、SSO、审计导出。其他能进主线的都进。

## 2026-04-26 Update — 实测中暴露的失败模式

通过前端"现在 firebase 的认证逻辑是啥"实测，拿到 answer_trace，发现四个并发失败：

1. **翻译压瘪原问题**：`现在firebase的认证逻辑是啥` → `firebase auth`，丢掉"逻辑/是啥"的 explain 意图。token_coverage 0.5。→ Phase 3.1 调整方向：query 处理必须**保留原文 + 加扩展**，不允许替换。
2. **chunk 切碎**：Login.js 真正认证逻辑在 L35-82（`handleLogin`），但 retrieval 拿到的是 L1-5（imports）—— 因为按命中切窗，imports 含 `firebase` 字面量得分高，函数体含 `database/ref/get/child` 没 `firebase` 字面量被丢。→ **2026-04-28 修正方向**：原计划做 AST chunking 解决；改为优先 **Phase 3.0 CC agentic 检索**（CC 直接 grep + read 整个函数），AST chunking 降级为 fallback。
3. **router 写死 Android**：`_route_query` 的所有分支都为 Kotlin/Java/XML/Gradle/AndroidManifest 写死，JS 项目走默认分支拿到 `preferred_extensions=(".kt",".java",".xml")` 和 "Kotlin and Java" 的 rationale。→ **T-KB-ROUTE-LANG-AGNOSTIC** 小卡（Phase 2.x，30 行 + 1 测试）。
4. **CSS 文件混入 top-K**：Login.css 占了 1/4 证据预算。→ Phase 3.0 顺手做扩展名加权（CC grep 用 `--include` / `--exclude` 过滤 + 索引层加扩展名黑名单）。

另外 multi-sample N=3 的"诚实税"把 single-sample 30.04 拉到 27.06。→ benchmark 基线必须用 multi-sample 数字，不再引用 single-sample。

---

## 起点（2026-04-23 HEAD @ `fb67194`）

已落地的主干能力：
- QA 链路：MiniMax 合成（T-KB-LLM-SYNTHESIS）+ shortcut 跳 planner/reviewer（T-SKIP-PIPELINE-FOR-QA），端到端 13–19s
- 代码修改链路：T-038/T-039/T-040 的 spec_conformance + jira_approval + 5 道防线已在
- 治理：T-026 RBAC 88/88 smoke 过，后端 policy/approval 模型在
- 沙箱 codegen：T-042-S worktree 机制

欠债（必须在后续阶段清掉，不能带着上线）：
- 22 个红测试挂在 `checkpoint/pre-reclassify`：intent_resolution(5) + request_refinement(7) + semantic_review_order(6) + gate_repair(4)
- 无准确率数字。"shortcut 不掉准"目前靠推理，不靠测量
- 检索还是单路 BM25，多跳/复杂问题答不了（D 档预期低分）
- 没有结构化引用链，审批页看不到 diff 的每一行对应哪条证据
- 没有长期记忆，同一个"闸门失败→怎么修"每次都从零推
- 没有结构化日志/指标/告警

---

## Phase 1 — 建立测量地基

**不做这一步，后面所有"优化"都是拍脑袋。**

### 1.1 T-QA-ACCURACY-BENCHMARK（已建卡，未动）
- 34 条固定问题集（A 简单定位 10 / B 单文件说明 10 / C 跨文件引用 8 / D 多跳分析 6）
- 每题存 `expected_answer_keypoints` + `expected_citations`
- 评分脚本：keypoint 覆盖率 + citation 精度 → 0-100
- 首次基线报告存档，带评委模型版本戳

### 1.2 D 档失败模式归类报告
- 跑完 D 档 6 题后写一份 `qa-complex-failure-analysis.md`
- 每题标注：检索漏 / 合成漏 / planner-would-have-helped / 其他
- 这份报告是 Phase 3 的"需求文档"——如果 D 档主要败在检索，优先做多路召回；败在合成，优先做 rerank；败在推理，优先做 planner 回潮

**出口**：main 上有一组可复现的 A/B/C/D 分数；`docs/ai/benchmarks/` 目录有基线报告。之后每次动检索/合成/模型都重跑这个集拍板。

---

## Phase 2 — 清测试债 + 小 bug 收尾

**22 个红测试是"假信号源"。不清掉，Phase 3/4 跑 pytest 永远分不清哪些是真回归。**

### 2.1 checkpoint/pre-reclassify 分支审计
- 逐个过：intent_resolution / request_refinement / semantic_review 这三块半成品代码
- 决定每块命运：①能合则补齐合入；②设计错则删；③还有价值但未完成则写清楚 xfail 原因 + 下次继续的入口
- **更新 2026-04-26**：P0 review 期间已删掉 11 个孤儿测试文件（intent_resolution/request_refinement/semantic_review/gate_repair 的孤立测试 + 它们引用的不存在方法）。剩余红测试需重审是否还存在。

### 2.2 T-PHASE-Y-ANCHOR-FOLLOWUP
- `fx_neg_nonexistent` regression 修掉（单点，1 天内可清）

### 2.3 T-KB-ROUTE-LANG-AGNOSTIC（实测中发现，新增）
- `_route_query` 不再写死 Kotlin/Java，根据 SourceSpec 实际扩展名生成 rationale + preferred_extensions
- spec：`docs/ai/tasks/T-KB-ROUTE-LANG-AGNOSTIC.md`
- 30 行 + 1 测试，codex 执行

### 2.4 锁住基线
- `pytest apps/backend/tests/` 零红
- 所有 xfail 必须带 reason 字符串 + 关联 ticket ID
- 加一条 CI 规则：新增 xfail 必须在 PR 描述里解释

**出口**：main 上 `pytest` 零红；22 个红测试要么变绿、要么变 xfail-with-reason、要么删除；T-KB-ROUTE 合入；每一条处置有 commit 记录可追。

---

## Phase 3 — RAG 检索与生成质量阶梯

**这是业务主价值。D 档从"预期低分"变"能答对一半"就是这一阶段的收获。**

按依赖顺序，不跳步。每一步跑 Phase 1 benchmark 验证"有没有真的涨"。

### 3.0 CC agentic 检索（NEW priority，2026-04-28 重排）

**为什么放最前**（替换原 AST chunking 优先位）

实测 KB（`D:/项目/HostedDashboard/handyman-admin-dashboard`）规模约几十文件。在这种规模下：

- CC 全库 ripgrep ~100ms；单文件 Read ~200-500ms；3 轮 agent ≈ 6-10s（**比当前单路 RAG 13-18s 还快**）
- 直接让 LLM agent 在真实文件树上 grep + read，**不需要任何离线索引**，不需要 chunk 边界，不需要 embedding
- 解决 baseline 大半失败题：A-05 (`grep -r 'ExportReportButton' --include='*.js'` 一条命令出结果)、D-01 / D-04 (多轮探查)、Login.js handleLogin 定位（直接 Read 整个文件）
- 跟 EvidenceItem schema 的 `cc_glob` / `cc_grep` / `cc_read` 三个 source 完美匹配，schema 已经在等这一刀

**为什么不再优先 AST chunking**
- AST 只解 A/B 档部分（chunker 切碎问题），完全不沾 C/D 档（多跳）
- 小代码库下，AST chunking 的"预先切片 + 索引"成本对比 CC "直接 grep" 没优势
- AST 留作 fallback：CC agent 若不可用 / 速率超限 → 退回 AST + RAG

**做什么**

1. **CC tool wrapper**（新建 `apps/backend/app/services/cc_agent.py`）：
   - `cc_glob(pattern, *, cwd, timeout=10s) -> list[FileMatch]` — 调 `claude` CLI 的 `Glob` 工具
   - `cc_grep(pattern, *, cwd, file_glob=None, timeout=20s) -> list[GrepHit]` — 调 `Grep`
   - `cc_read(path, *, cwd, line_range=None, timeout=15s) -> str` — 调 `Read`
   - 每次调用结果**直接转 EvidenceItem** 塞进证据池

2. **Agent loop**（新建 `apps/backend/app/services/cc_agent_loop.py`）：
   - 输入：query + cwd
   - 第一轮：LLM 看 query → 决定 `cc_glob` 或 `cc_grep` → 拿到 3-10 个候选文件
   - 第二轮：LLM 看候选 + query → 决定 `cc_read` 哪些文件的哪些范围
   - 第三轮（可选）：LLM 看 read 结果 → 决定要不要再 grep / read 别的
   - 预算：`max_rounds=3` / `max_tool_calls=8` / 整体超时 `30s`
   - LLM 输出结构化 ReAct（`thought` + `action` + `observation`），不允许自由文本调用工具

3. **LLM provider chain（agent 自己用的决策模型）**：
   - 顺序：**`claude_code` CLI → `codex` CLI → `minimax`**（按用户 2026-04-28 明示）
   - **不在链里**：`anthropic` API（用户不烧 API 余额）；如果 `_resolve_provider_chain` 默认包含 anthropic，CC agent 这条路要单独配 chain
   - 配置：`cc_agent_provider_chain: list[str] = ["claude_code", "codex", "minimax"]`
   - 任一 provider 失败 → 走下一个；全失败 → fallback 到纯 RAG（保留旧路径）

4. **资源文件过滤**（顺手做，从原 AST 方案搬过来的子任务）：
   - 索引时排除 `.css / .scss / .svg / .png / .lock / .min.*` 等
   - CC grep 的 `file_glob` 也加默认 exclude 列表

**质量门槛**
- 单 query 端到端 ≤ 30s（CC 多轮 + 合成），平均 ≤ 18s
- A 档 35 → 60+（A-05 这种"找文件"题应该接近满分）
- B 档 11 → 35+
- D 档 22 → 40+（多跳能跑通才有用）
- CC 任一调用失败 → 自动 fallback，整体 query 不挂
- agent 跑超 max_rounds → 用已收集的证据走合成，不空手回（"degraded mode"）

**实施依赖**
- T-WS-FS-WORKSPACE schema 已合 ✅（cc_glob / cc_grep / cc_read 三个 source 已定义）
- claude_code CLI 已在 PATH（`npx @anthropic-ai/claude-code`）
- codex CLI 已在 PATH（`codex-cli 0.125.0`）
- minimax API key 在 `.env`

**spec**：待写 `docs/ai/tasks/T-KB-CC-AGENTIC-RETRIEVAL.md`（取代原 AST 优先方案）

### 3.0-fallback AST-aware chunking（降级为可选 fallback，2026-04-28 修订）

**何时做**
- 仅当 3.0 CC agentic 路径不稳定 / 速率限制频发 / 或新接入的大代码库（>5000 文件）让 CC 走全库变慢时，才做这一步
- 对当前几十文件 KB，AST chunking 的 ROI 已经被 CC 模式覆盖

**做什么**（保留原方案，仅作 fallback）
- 引入 TreeSitter（或更轻的 ast 库针对 JS/Py/Java），按"函数/类/顶层声明"切 chunk
- 每个 chunk 包含完整 body 和 docstring，附带 `enclosing_symbol` 元数据
- 文件级粗粒度索引保留作为 fallback（小文件、纯 config）
- 扩展名加权 / 排除：`.css`/`.svg`/`.png` 等纯资源文件不进 retrieval 池或权重 -50%
- 顶层文件级 chunk 跟函数级 chunk 共存

**spec**：原 `docs/ai/tasks/T-KB-AST-CHUNKING.md` 保留但状态改为 P2（fallback 方案）

### 3.1 Query 处理（修订 2026-04-26：从"rewrite 替换"改为"additive 扩展"）

**为什么改方向**
- 实测翻译把 `现在firebase的认证逻辑是啥` 压成 `firebase auth`，丢掉 explain 意图
- 单纯的"rewrite 替换"会让原始问题的 token 信号丢失
- 要的是 **保留原文 + 加扩展**，不是 **替换**

**做什么**
- `apps/backend/app/services/query_rewrite.py` 输出 `QueryVariants(original, expansions, entities)`
  - `original` = 用户原文（不动）
  - `expansions` = 启发式抽出的同义词 / 符号 / 路径片段（先做，便宜）
  - `entities` = 文件名 / 类名 / 函数名候选
  - LLM 兜底只在启发式产出 < 2 个 expansion 时触发
- retrieval 端 `original` 和 `expansions` 同时打分，`expansions` 命中加权 < `original` 命中
- 配置：`query_expansion_enabled`、`query_expansion_max_terms`、`query_expansion_llm_fallback`
- `_text_anchors.py` 提取保留，供 spec_conformance 复用

**质量门槛**
- 原始问题 token 必须出现在 retrieval 评分里（不允许被翻译完全替换）
- LLM 路径超时/失败必须优雅回落到启发式
- benchmark A+B 档分数不降，C 档 +5，D 档有可见改善

### 3.2 结构化引用链（B.4，先做引用链，再做多路召回）
**为什么先做引用**
- 多路召回 + rerank 会让"答案为什么引用这条"更难追。引用链结构化后，后续每一条优化都能在引用粒度上评估。
- 审批页（Phase 6）也吃引用链——早做早省一次返工。

**做什么**
- 合成 prompt 强制要求"每条论断 → 对应 file_path + line_range + 召回通道来源"
- 数据模型：`KnowledgeCitation { claim, file_path, line_range, source_channel, confidence }`
- 前端 chat 区域把纯文本引用换成可点跳转卡片

**质量门槛**
- benchmark 的 citation precision 口径从"文件级"升到"行级"，基线重跑一次存档
- 无引用或引用残缺时 UI 明确提示（不造假）

### 3.3 混合证据召回（升级自原 "多路召回"，2026-04-28 update + 2026-04-29 校准）

**2026-04-29 校准（Stage 11/12/13 复盘 final）**

Stage 11 v1（关键字分类器 → tier-aware cap）部分失败：D +10.67 ✅，但 C -15.75 ❌。Stage 12 v2 inverted-default 政策跑了 rejudge：A=55(-1.5)、B=44.8(+7.2)、C=58.1(-12.5)、D=39.7(-0.3)、**mean=50.03（vs 49.65, +0.38 — 统计噪声）**。

诊断：trace 显示 34/34 题都 `locate_detected=False, cap_used=6000`，policy 实际等于"全局 cap=6000"。后做 5 分钟 unit test，把 raw dataset 34 题喂 `_detect_locate_signal`，只 **2/34** match。差异原因：planner 给 synthesis 的 `query` 不是用户原 `request_text`（query_rewrite +12 tokens），detector 的 `len > 60: return False` 把这些都筛掉。

**结论：tier-aware via regex/keyword classifier 不是有用的杠杆。** Drop 这个 workstream，v2 不 commit，回 baseline cap=3000 (.env override)。

落到 Phase 3.3 的具体校准：

落到 Phase 3.3 的具体校准：

1. **任何"按 query shape / channel 选权重"的逻辑，先做 binary（开/关 / 宽/窄），不要做 3+ 档**。v1 想 single/multi 双档已经吃了苦头，跳到 single/medium/wide 三档只会失败更多次。证明二元不够再加。
2. **channel_weights 不能拍脑袋**。所有 RRF 权重必须先有 T-LLM-METRICS 的数据（每个通道的命中率 / p95 延迟 / cost），再用一份 baseline 数据集做小范围 sweep 决定。**不允许在没数据的情况下硬编码 0.4 / 0.3 / 0.2**。前置票：`docs/ai/specs/llm-metrics-instrumentation.md`。
3. **接受标准 "C 档 +10 / D 档 +8" 的基线对照对象更新**：原来对 Phase 3.0 出口（49.65 + D 30.33），v2 落地后改用 v2 PINNED judge baseline 作为新基线（数字待 Stage 12 出）。比较时 judge / synthesis provider 必须 pin 死。
4. **CC 工具混入 EvidenceItem 池的优先级降低**：v2 已经验证 CC 单路就能拿 49.65 mean。3.3 的 "agentic 通道" 重要性从 P0 降为 P1 —— 先把 fts5 + card 这两个**离线索引**通道做掉（CC 慢的根源是 runtime 调用，离线索引不抢 30s 预算），benchmark 涨多少看，再决定要不要把 cc_glob/cc_grep 也并进 RRF。
5. **T-KB-HYBRID-RAG-FAST-PATH 重新定位**（codex 反对推迟到 3.5）：176s/Q 不是"latency-only"，是"产品根本不能交互"。Hybrid fast-path 留 **P0**，但 scope 窄：simple locate/definition/factual repo 问题走 fast-path（RAG 13-18s），ambiguity / 低置信才升 agentic。这是 codex 在 Stage 12 critique D5 修正我的判断后的版本。
6. **classifier 的输入要和测试输入一致**（Stage 13 lesson）：v2 unit test 用 raw question，runtime detector 看的是 planner 改写的 query。任何未来 query-shape 分类（fast-path 路由、cap 选择、re-rank trigger）必须明确到底用哪个 query 字符串，并在 trace 里两个都记。

**为什么改名 + 改方向**

原 3.3 写的是 "4 条 RAG 通道（BM25 + embedding + 符号图 + git-recency）+ RRF 融合"。方向对，但范围窄了：T-WS-FS-WORKSPACE 已合并（commit `015d256` + `f646cb6`），里面的 `EvidenceItem` 统一 schema 把可融合的证据来源扩到了 **8 种**，包含 Claude Code agentic 工具结果和文件级 markdown 卡片。RAG-only 多路只覆盖其中 5 种 —— 剩下 3 种（CC 工具 + 卡片）也是这一阶段要落地的产物，不该拖到 Phase 5。

**EvidenceItem.source 全集**（schema 已在代码里）

| Source | 性质 | 数据来源 | 当前状态 |
|---|---|---|---|
| `rag_lexical` | 被动 | 现 `knowledge.py` keyword retriever | ✅ 已用 |
| `rag_fts5` | 被动 | SQLite FTS5 BM25 全文索引 | 这一阶段做 |
| `rag_card` | 被动（粗粒度） | 离线 LLM 给每个文件写 markdown 卡片 | 这一阶段做 |
| embedding | 被动 | 现单路 embedding 检索 | ✅ 已用（待并入融合）|
| symbol graph | 被动 | `knowledge_source_path/index.json` 解析符号引用 | 这一阶段做 |
| git recency | 被动 | git blame 加权最近 N 天改动 | 这一阶段做 |
| `cc_glob` / `cc_grep` / `cc_read` | 主动 agentic | Claude Code CLI 工具调用结果 | 这一阶段做 |
| `user_provided` | 主动 | 用户在 chat 里粘的代码 | ✅ 已用 |
| `spec_anchor` | 被动 | spec_conformance must_touch 抽出 | ✅ 已用 |

**做什么**

1. **被动检索通道（offline-indexed）**
   - **rag_fts5**：把现 `rank_bm25` 切到 SQLite FTS5。在 >10k 文件仓库快 5-10x，支持 phrase / NEAR / column boosting
   - **rag_card**：离线对每个文件让 LLM 写一段 markdown 卡片（"这文件干啥 / 关键 symbol / 跟谁相关"），存进 SQLite + FTS5 索引。Agent 第一轮**先扫卡片粗筛**，再 deep-read 命中文件
   - embedding / symbol graph / git recency 接入融合（数据已有，只是没 RRF 合）

2. **主动 agentic 通道（runtime）**
   - 把 Claude Code CLI 的 `Glob` / `Grep` / `Read` 工具调用结果**塞进 EvidenceItem 池**，跟 RAG 召回结果并列融合
   - 触发流程：第一轮 RAG（rag_fts5 + rag_card 粗筛）→ LLM 看证据决定要不要 cc_glob/cc_grep/cc_read 深查 → 第二轮证据再融合
   - 跟 Phase 5.4 KnowledgeAgent 是同一套机制；EvidenceItem schema 把"被动召回"和"主动 agentic"统一在一个池子，避免两套打分逻辑

3. **融合**
   - RRF（Reciprocal Rank Fusion）合并所有有 rank 的通道
   - CC 工具结果用 hit-count + 文件相关性打分（不是 rank-based），混入 RRF 时用 normalized score
   - entity 命中（query 里抽出的 file_path / class / function name）+15% boost
   - 配置：`knowledge_rrf_k`、`knowledge_entity_boost`、`channel_weights.{rag_fts5, rag_card, embedding, symbol, recency, cc_glob, cc_grep, cc_read}`、各通道独立开关

**质量门槛**
- 任意单一通道关掉都能降级跑（隔离故障）
- benchmark：C 档 +10 分、D 档 +8 分（vs Phase 3.0 出口基线）
- card 粗筛后第一轮被深读的文件数 ≤ 5（卡片必须真起作用）
- CC 工具触发的额外延迟 ≤ +6s（agent 多走一轮的成本上限）
- 单仓库扫描总耗时 ≤ 当前 12-18s 的 1.5x（混合比纯 RAG 慢，但要有顶）

**实施依赖**
- Phase 3.0 CC agentic 检索完成（agent 已在跑，cards 是优化层）；如降级用 AST，则 AST chunking 完成（card 摘要要按函数/类粒度生成）
- Phase 3.5 PreIndex 同步升级，把 FTS5 表 + 卡片表都建出来（预索引方案见 3.5 修订）
- T-WS-FS-WORKSPACE schema 已合并 ✅

**spec**：未写，建议三个 ticket（按依赖顺序）
- `T-KB-FTS5-INDEX` — FTS5 表设计 + 查询接口 + benchmark vs rank_bm25
- `T-KB-FILE-CARDS` — 离线卡片生成（LLM 一次性 pass）+ 卡片召回 channel + 粗筛逻辑
- `T-KB-CC-EVIDENCE` — Claude Code Glob/Grep/Read 接入 EvidenceItem 池 + RRF 融合扩展

### 3.4 Rerank 跨编码器精排（B.3）
**做什么**
- 候选 Top-K（比如 30）走 cross-encoder 精排到 Top-N（比如 8）
- 本地小模型优先（bge-reranker-base 之类），避免再加一个外部 LLM 依赖
- 配置：`rerank_enabled`、`rerank_top_k`、`rerank_top_n`

**质量门槛**
- D 档分数从 3.3 基础上再 +5 分
- 延迟增加不超过 1.5s（rerank 不能失控）

### 3.5 知识库预索引与缓存（同步 3.3 升级 2026-04-28）
**为什么放在这**
- 前面 3.1–3.4 会把检索路径越撑越重。不预索引，多仓库接入就炸。
- 3.3 升级后多了两类新索引（FTS5 + 文件卡片），尤其卡片要 LLM 离线生成（每文件一次），不预跑就每次查询都点钱。

**做什么**
- `apps/backend/scripts/build_knowledge_index.py`：扫描 → AST 分块（Phase 3.0-fallback；CC 路径不需要预 chunk）→ 多种索引并行建 → 持久化到 `data/kb_index/`
- 索引产物：
  - **embedding** 表（Phase 3.3 之前已有）
  - **FTS5 BM25** 表（替换原 rank_bm25 倒排，对应 `rag_fts5`）
  - **文件卡片** 表（LLM 一次性 pass 给每文件写 markdown 摘要 + 关键 symbol，对应 `rag_card`，FTS5 索引化）
  - **符号图**（import / call / def，对应 symbol channel）
  - **git recency** 元数据（每文件最后改动时间）
- 查询路径只读索引 + 按 mtime 增量刷新（卡片支持 git diff 增量重建，不全量重跑 LLM）
- 重建策略：
  - 文件变动触发：mtime 变 → 仅重建该文件相关条目（embedding / 卡片）
  - 定期全量：按周或按 commit 数兜底
  - 卡片 LLM 调用要带 budget cap（默认 100 文件/全量任务），超限走降级（用文件名 + top-N 函数签名拼凑伪卡片）

**质量门槛**
- 查询侧端到端从 13-18s 降到 < 3s（合成之外的部分）
- 卡片首次全量构建：单仓库 < 10 分钟（包括 LLM 调用，假设 100-500 文件）
- 增量重建：单文件改动 → 索引刷新 < 5s
- 首次构建有进度日志 + 失败可恢复（断点续跑）

---

## Phase 4 — 反幻觉矩阵完整版（T-041 八个闸门）

**背景：T-038/T-039/T-040 已把"拒绝可疑产出"的骨架搭好，但 T-041 把"为什么拒绝"从黑盒变白盒。Phase 3 做完后，引用链结构化了，闸门才有料可校验。**

### 4.1 T-041-01 Evidence bundle（P0）
- codegen 开工前必须提交一个"证据包"：意图表、引用链（复用 3.2 的结构）、已知失败测试、假设列表
- 闸门：证据包任何一块缺失 → 直接拒绝

### 4.2 T-041-02 Intent-vs-diff shape checker（P0）
- 对比"意图表声明要改什么"和"diff 实际改了什么"，形状不一致就拒绝
- 检查维度：新增文件数、删除行数、跨模块跳跃

### 4.3 T-041-03 Existing-file-first policy（P0）
- 新文件比例硬门槛（比如新文件 / 总改动文件 > 50% 就必须审批加证据）
- 防止 LLM 不读现有代码、上来就新建并行实现

### 4.4 T-041-04 Approval 证据链闭合（P0）
- 审批入口强制校验：证据包 + diff + goal attestation 三者必须全在
- 缺任意一项 → 审批按钮灰掉，前端明示原因

### 4.5 T-041-05 Symbol gate（P1，纳入主线）
- 校验 diff 引用/修改的符号在知识库符号图里真的存在（不能凭空 import 不存在的模块）

### 4.6 T-041-06 Failing test first（P1，纳入主线）
- 修 bug 必须先看到一个能复现的红测试，再允许改业务代码

### 4.7 T-041-07 Runtime path validation（P1，纳入主线）
- 改动落地后跑一次 runtime 路径校验（例如启动进程、打一次 health、跑一条冒烟）
- 已有 runtime validation self-repair loop（commit `3fb5432`），这一步是把它接入 T-041 评分

### 4.8 T-041-08 Goal-by-goal attestation（P1，纳入主线）
- 每个 goal 单独有签字栏：该 goal 对应哪些 diff hunk、引用哪些证据、跑了哪些测试
- 任一 goal 缺签字 → 整个审批不通过

**Phase 4 出口**：一个典型 Jira develop 任务完整 E2E 一次，每一步闸门的拒绝/通过原因都能在日志里追到单行。

---

## Phase 5 — 记忆系统 + Agentic 检索（agentic-rag-memory Part A + B）

**为什么放在 Phase 4 之后**
- Part A 核心是"闸门失败 → 修复方案"的结构化沉淀。Phase 4 把八个闸门都立起来之后，失败模式才稳定，记忆条目才不会在"这个闸门下周被改掉"中白写。
- Part B（agentic 多步检索）建在 Phase 3 的 chunking + 多路召回基础上，单步 retrieval 已经稳定，agent 才有意义"再发一轮"。

### 5.1 数据模型
- 新表 `agent_memories { id, scope, key, observation, resolution, created_at, last_used_at, usage_count }`
- scope 示例：`gate:spec_conformance`、`gate:diff_reviewer`、`repo:HostedDashboard`
- key 用内容 hash（同一类失败同一条记忆）

### 5.2 写入路径
- 闸门拒绝 + 后续修复完成 → 自动提取 (observation, resolution) 对写入
- 人工修复也能手工 flag "这是值得记的"

### 5.3 读取路径
- codegen 开工前，按当前 scope 拉近 N 条相关记忆拼进 prompt（不替代 spec，是补充）
- 命中即自增 usage_count，冷记忆定期归档

### 5.4 Agentic 多步检索（Part B，新合并 2026-04-26）

**为什么不在 Phase 3 做**
- Phase 3 的 chunking + multi-path + rerank 解决 ~85% 的"chunk 选错了"
- 剩下 15% 是"一次检索说不清"——典型 D 档 trace 题（"Trace from X to Y to Z"）
- 这种问题需要 agent 一边查一边推理，不是一次召回能给完的

**做什么**
- `KnowledgeAgent` 类：拿到 query → 第一轮 retrieval → LLM 看证据 → 决定是否 re-query
- 工具集：`expand_chunk(file, line)`、`fetch_function(file, symbol)`、`follow_import(file, symbol)`、`fetch_file(path)`
- 预算：`max_rounds=3`、`max_tool_calls=8`、单轮超时 30s、整体超时 90s
- LLM 输出结构化 ReAct（thought + action + observation），不允许自由文本调用工具
- 命中预算上限或 LLM 说 done → 进入正常合成阶段

**质量门槛**
- D 档分数（Phase 3 结束基线之上）+8 分以上才合入
- agentic 路径平均延迟不超过 30s（vs 单轮 13-18s）
- 路径失败必须降级为单轮 retrieval（不能整个 query 失败）
- 配置：`agentic_retrieval_enabled`（默认 OFF）、按 query 复杂度自动启用（D 档默认 ON、A 档默认 OFF）

### 5.5 观测
- 面板：记忆总数、近 7 天命中次数、Top-10 被复用的记忆
- agentic：平均 round 数、超预算率、降级率
- 决定是否需要更积极的摘要/合并

**出口**：
- 同一类闸门失败，第二次遇到平均修复时间降 30% 以上（benchmark 在 Phase 4 E2E 上跑）
- D 档 trace 类问题分数 ≥ A 档分数的 80%（在 Phase 3 出口基础上）

---

## Phase 6 — 治理 UX 收尾

> **紧急插入（2026-04-23）**：Playwright 实测 P69-8 发现 `awaiting_approval` 的 chat UI 与"处理中"视觉无区分，且 codegen.repair 无上限（实测 9 轮 19 分钟）。下列 6.0a/6.0b 前置。

### 6.0a T-CHAT-APPROVAL-UX（P0，插入项）
- `awaiting_approval` 不再用 ThinkingIndicator，改成独立状态块 + 自动滚到审批按钮
- ticket: `docs/ai/tasks/T-CHAT-APPROVAL-UX.md`

### 6.0b T-PIPELINE-REPAIR-CAP（P1，插入项）
- codegen.repair 硬上限 N 轮（默认 3）+ 单轮超时（默认 180s）
- 超限不失败、转审批；审批页列出每轮尝试摘要
- ticket: `docs/ai/tasks/T-PIPELINE-REPAIR-CAP.md`

### 6.1 T-030 审批队列 UI
- 列表 + 详情 + grant/reject 流
- 详情页展示：原始意图、goal 列表（勾选状态）、证据包、diff（下一步 6.2）、记忆命中（Phase 5）

### 6.2 T-O1 Diff viewer with goal attestation
- 真实 diff 高亮 + 每个 hunk 对应的 goal 标记
- 引用链点击跳 KB 文件

### 6.3 RBAC 前端错误路径
- 被 deny 时 UI 不崩，展示明确消息 + "要谁审批"指引
- 401/403 与 500 视觉区分

### 6.4 审计事件列表
- 按 task 聚合的事件流视图（translation → plan → gate → approval → diff → test → done）
- 每个事件可展开看原始 payload

**出口**：非 admin 发起 `notion.update` → 前端看到"需审批" → admin 在审批队列看到完整证据 → grant → 任务继续。全程零手动 curl。

---

## Phase 7 — 运维可观测性

### 7.1 T-I1 structlog 结构化日志
- 所有关键路径加 `request_id`、`task_id`、`session_id`、`stage`
- JSON 输出，按日滚动

### 7.2 T-K1 指标
- 最小集：任务吞吐、失败率、LLM 调用成本、闸门通过率（按闸门名分 label）
- Prometheus 暴露点 `/metrics`

### 7.3 T-L1 告警 + 深度健康检查
- `/health` 检查到具体子系统（DB、MiniMax、KB 索引、政策引擎）
- 失败率 > 阈值、成本异常 → 告警（先邮件，后接 IM）

### 7.4 T-J1 OpenTelemetry（纳入主线，因为质量优先）
- 分布式 trace 串起 orchestrator → tool_gateway → knowledge → MiniMax

**出口**：一次失败任务从告警 → 日志 → trace_id → 出错代码行，< 5 分钟定位。

---

## Phase 8 — 体验打磨

### 8.1 T-STREAMING-SSE（B.5）
- spec 已在 `docs/ai/tasks/T-STREAMING-SSE.md`，等主链路稳定（现在就算稳定）
- 后端：`knowledge_synthesis` 支持 `stream=True`，新增 `GET /api/tasks/{id}/stream`
- 前端：`EventSource` 累加渲染，引用区和答案分 slot
- 结构事件入 DB，token 流不入

**出口**：首字 < 2s，断连回落到一次 GET 轮询。

### 8.2 长期会话记忆（B.6）
- 同一 session 跨回合的上下文在 orchestrator 层维护摘要
- 超过 N 轮触发滚动摘要，避免 prompt 无限增长

### 8.3 语言/语气打磨
- 中/英问题检出，合成端语言对齐
- 回答首段给"我做了什么检索"的一句话可选披露（不是默认）

### 8.4 T-P1 MiniMax fallback
- 合成/翻译/评审分离到独立 provider 抽象
- Anthropic 作为 fallback 至少接起来（不强制用，但断网演练必须过）

---

## Phase 9 — 发布闸

### 9.1 README 端到端重写
- 从 clone 到"跑完一个真实任务"的手把手演示
- 用一张真的 Jira ticket 截图贯穿

### 9.2 全量回归
- Phase 1 benchmark 重跑
- 全套 pytest 零红
- E2E Jira develop（Playwright）
- E2E 审批流（Playwright）
- 运维告警演练：人为触发失败 → 确认告警到 → trace 追到源

### 9.3 DECISIONS.md 增补
- Phase 1–8 期间每次重大取舍单独一条（比如"为什么先做引用链再做多路召回"）

### 9.4 打 tag
- `v1.0` 当且仅当 9.1–9.3 全绿
- 发版说明按阶段回顾，不按 commit 流水账

---

## 依赖关系（简图，更新 2026-04-26）

```
Phase 1 (测量 + multi-run baseline)
   └── Phase 2 (清债 + T-KB-ROUTE)
          └── Phase 3.0 CC agentic 检索 ★ 硬前置（2026-04-28 替换原 AST 优先）
                 │   (AST chunking 降级为 fallback，仅大代码库或 CC 不可用时启用)
                 └── Phase 3.1 Query 处理（additive）
                        └── Phase 3.2 CitationChain
                               ├── Phase 3.3 MultiPath
                               │      └── Phase 3.4 Rerank
                               │             └── Phase 3.5 PreIndex
                               └── Phase 4 (T-041 八闸门)
                                      └── Phase 5 (记忆 Part A + Agentic Part B)
                                             └── Phase 6 (治理 UX)
                                                    └── Phase 7 (可观测)
                                                           └── Phase 8 (体验)
                                                                  └── Phase 9 (发布)
```

关键串行点：**Phase 1 → 2 → 3.0 → 3.1 → 3.2** 是硬依赖，不能并行。
**Phase 3.0 AST chunking 改了 chunk 边界 → 必须立新 baseline 再继续**。
**Phase 3.3/3.4/3.5** 可以合成一个大 sprint 做。
**Phase 6/7** 彼此独立，可并行。
**Phase 8.1 SSE / 8.2 长期记忆 / 8.3 语言 / 8.4 fallback** 彼此独立，按心情排。

---

## 风险与悬而未决

1. **MiniMax 单点**：3.1–3.4 每一步都更依赖它。8.4 的 fallback 要前置，最迟在 Phase 4 之前接好（否则一次 MiniMax 故障把整条新链路打哑）。
2. **评委漂移**：Phase 1 评委模型版本升级会让后续 benchmark 分数不可比。基线报告必须带评委版本戳，版本变更时同 commit 重跑一次对齐。
3. **索引爆炸**：3.5 预索引上线后，多仓库接入前要先压测。设上限（比如 "单仓库 > 50MB 代码触发警告"）。
4. **记忆噪音**：Phase 5 如果不加冷却/归档，一年后记忆表会变"万恶之源"。5.4 面板必须上线第一天就开始看。
5. **E2E Playwright 脚本债**：Phase 6/9 的 E2E 必须 Playwright（记忆规则）。这套脚本现在只有几条，后续每个 Phase 至少补 1 条，不能到 Phase 9 才集中写。

---

## Post-v1.0 列表（不阻塞发版）

- 多租户 / 异步执行（触发条件见 ADR 0002）
- 公开部署 / SSO
- 多模型并行合成（快速回答 vs 深度回答双路）
- 接入更多工具（Slack threads、Notion blocks、GitHub PR review）
- 审计导出
