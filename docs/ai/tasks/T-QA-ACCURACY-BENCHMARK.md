# T-QA-ACCURACY-BENCHMARK — QA 回归基准 + 多跳问题诊断集

**Status:** 已开，未动
**Priority:** P2（基础设施；解锁多项未来决策）
**Created:** 2026-04-23

## 为什么现在开

T-SKIP-PIPELINE-FOR-QA merge 后，"shortcut 不掉准确率"目前只靠推理支撑（简单题 BM25 关键词够用；多跳题走不进 process_question 场景）。**没有数字证据**。

两个直接用途：
1. **短期**：证明 shortcut 没回归（或发现回归）。后续每次动检索 / 合成 / 换模型都能直接跑这个集拍板。
2. **长期**：迟早要攻克交叉/多跳/分析类问题。需要先有测量才知道当前基线有多差、改到哪一步够用。benchmark 结果会直接生成"复杂 QA 应该开成什么 ticket"的需求。

## Scope v1（先把回归跑起来）

建一个固定问题集，覆盖四档复杂度：

| 档位 | 数量 | 例子 | 预期 shortcut 表现 |
|---|---|---|---|
| A. 简单定位 | 10 | "where is Login defined" / "登录页在哪个文件" | 满分（BM25 够） |
| B. 单文件说明 | 10 | "what does AuthProvider.tsx do" | 高分（LLM 合成能答） |
| C. 跨文件引用 | 8 | "which components use AuthProvider" | 中分（BM25 能命中部分，合成勉强） |
| D. 多跳分析 | 6 | "登录模块和订单模块有什么耦合" | **预期低分**，用来量化 gap |

**每题存储**：`question`、`expected_answer_keypoints`（3-5 个关键命中点，不是完整答案）、`expected_citations`（理想应该引用的文件）。

**评分**：
- Keypoint coverage（0-5 命中个数 / 应有个数）—— 可以让 MiniMax 或 GPT-4 当评委
- Citation precision（LLM 引用的文件有几个在 expected 集中）
- 加权综合成 0-100

## Scope v2（为"复杂 QA"ticket 做弹药）

跑完 D 档后，产出一份报告：
- 每道 D 题的失败模式归类（检索漏、合成漏、planner-would-have-helped、其他）
- 如果大头是"planner-would-have-helped"：下一张 ticket 是**给 process_question 加受限 planner**
- 如果大头是"检索漏"：下一张 ticket 是**改进知识检索**（chunking、embedding、reranker）
- 如果大头是"合成漏"：下一张 ticket 是**换更强的合成模型或加多轮 reasoning**

这一步把"我们要攻克复杂问题"从愿望变成可执行。

## 交付物

1. `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` — 34 条带标答问题
2. `apps/backend/scripts/run_qa_benchmark.py` — 跑一遍，输出 0-100 分 + per-question 明细
3. `docs/ai/benchmarks/qa-baseline-YYYY-MM-DD.md` — 首次运行的基线报告
4. `docs/ai/benchmarks/qa-complex-failure-analysis.md` — D 档失败模式归类（决定下一步走向）

## 验收

- 单次运行 < 10 分钟
- 分数可复现（同一 commit 跑两次差 < 3 分）
- shortcut 版本（main @ 5898436）拿到的 A+B+C 档分数 ≥ 85% 满分（没回归）
- D 档分数诚实记录（不做任何"提升 D 档"的改动，只量化）

## 不做

- **不在本 ticket 内改检索 / 合成 / 加 planner**。这是纯测量。任何"为了分数好看"的改动都必须开新 ticket。
- 不做 LLM-as-judge 的元评估（只要评委模型给的分稳定即可，不追求评委本身的准）。
- 不做跨语言（中英混合）测试。全英或全中问题集择一，先英文。

## 依赖

- 无。独立可做。

## 风险

- **问题集太小（34 条）**：基线分数方差可能大。缓解：单题确定性（scenario、model、seed 固定）。
- **评委模型漂移**：用 MiniMax-M2.7 做评委的话，模型版本升级会让分数漂。缓解：评委结果和答案一起存档，每次运行带评委版本戳。
