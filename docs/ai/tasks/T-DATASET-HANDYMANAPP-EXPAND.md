# T-DATASET-HANDYMANAPP-EXPAND — Expand handymanapp QA from 26Q to 50-60Q

<!-- Effort: medium (one-shot LLM-assisted authoring + manual review) -->
<!-- Executor: human-in-loop with codex/MiniMax assistance -->

**Status:** todo (P2 — gates T-STAGE19-REBENCH-N40)
**Priority:** P2 (D-010 acceptance for "real residual gap" requires n≥40 valid)
**Created:** 2026-05-01
**Branch:** `data/handymanapp-expand` based on `checkpoint/pre-reclassify` HEAD `db5ee82`
**Linked verdict:** `docs/ai/specs/stage20-judge-verdict.md` (caveat 3)

## Background

The Stage 20 cross-family rejudge of handymanapp 26Q produced 17 valid records (after the 4 rejudge-rescues, 21). The residual gap of ~8.5 points after switching to MiniMax judge sits on a sample where each tier has very few valid records (A=7, B=6, C=3, D=1). At n=17 the 95% CI on the mean is roughly ±5 points, which is comparable to the gap itself. **You cannot conclude the residual is real signal from n=17.** D-010 explicitly defers Stage 20C (cards-v2) until rebench at n≥40 valid confirms or falsifies the residual.

Given an empirically-observed ~25% bench-attempt-to-valid loss (timeouts + CC failures), we need ~50-60 attempted questions to land n≥40 valid. The current 26Q dataset is the floor we extend, not replace.

## Goal

Expand `apps/backend/tests/benchmarks/qa_benchmark_dataset_handymanapp.jsonl` from 26 questions to **50-60 questions**. Preserve the 26 existing questions verbatim (they have evidence-trail value via the rule + MiniMax artifacts already committed). Add **24-34 new questions** distributed across tiers to balance the existing distribution.

Tier distribution target after expansion (rough — tune to your judgment of what's worth asking):

| Tier | Existing | New (target) | After expansion |
|---|---|---|---|
| A (single-file factual) | 8 | +4-6 | 12-14 |
| B (multi-file lookup) | 8 | +6-8 | 14-16 |
| C (multi-file synthesis) | 6 | +8-10 | 14-16 |
| D (deep trace flow) | 4 | +6-10 | 10-14 |
| **Total** | **26** | **+24-34** | **50-60** |

**C and D get the heaviest expansion** because that's where the residual gap concentrates and where we have the thinnest current sample. A/B already had n=7/6 which is enough to confirm the judge-artifact recovery; widening them is lower marginal value.

## Sourcing approach

Use file-pinned LLM-assisted generation followed by manual review. **Do NOT auto-commit LLM-generated questions** — every new entry must be human-verified for keypoint accuracy and citation correctness.

### Step 1: Pick target files

Inspect `D:/项目/HandymanApp-fresh/` (the handymanapp source repo) and identify files that:

- Have substantive logic (not just stub Activities or DTOs)
- Cover Android stack breadth: Compose UI / Fragment lifecycle / Firebase calls / nav_graph / Gradle dependencies / data classes / image upload / state management
- Aren't already exhaustively covered in the existing 26Q

Aim for **20-25 distinct target files**. Spread questions across them so no single file accounts for more than 3 questions.

### Step 2: Generate file-pinned candidates

For each target file, prompt an LLM (MiniMax for cost, Claude/Anthropic if credit available) with the full file content and the existing question schema. Example prompt shape:

```
Given the file content below, generate K Android-engineer questions whose
expected answers are grounded in this exact file (and possibly 1-2 related files
for B/C/D tier). For each question:

- Pick a tier (A/B/C/D) based on whether answering needs only this file (A),
  this file plus 1-2 referenced files (B/C), or trace through 3+ files (D)
- Write a natural-language question an Android dev would actually ask
- Write 3-5 expected_answer_keypoints that name specific Android symbols
  (class names, function names, Firebase APIs, Compose annotations, XML
  resource IDs, navigation actions) appearing in the file
- List expected_citations as canonical paths matching the existing dataset's
  conventions

File: <relative_path>
File content:
<full file>

Schema example (existing dataset row):
<paste existing row>
```

### Step 3: Manual review checklist

For every LLM-generated candidate before adding to the dataset:

- [ ] **Keypoint grounding**: every expected_answer_keypoint has a literal token (class name, function, Firebase path, etc.) that appears in at least one of the cited files. (Critical: if the keypoint references a symbol that doesn't exist in the file, the bench will systematically score 0 on it forever.)
- [ ] **Citation accuracy**: paths match `D:/项目/HandymanApp-fresh/...` files exactly. Run `git ls-files` in the source repo and grep paths.
- [ ] **Tier honesty**: A-tier questions must be answerable from one file. C-tier questions must legitimately need 2+ files. D-tier questions must have a trace flow (data flowing through 3+ files / fragments / Firebase nodes).
- [ ] **Question naturalness**: an Android dev should plausibly ask this. Avoid contrived synthetic-test phrasing ("according to the file...").
- [ ] **Distinct value**: not a duplicate of an existing question on the same file with slight rewording.
- [ ] **Source name**: every row has `"source_name": "handymanapp"`.

### Step 4: Optional sanity bench

After adding the new rows, run `--limit 5` smoke against the new questions to verify nothing trivially breaks (citations resolve, retrieval returns at least the expected file at top-K, judge can score). Don't run the full new bench yet — that is `T-STAGE19-REBENCH-N40`.

## Files to edit

1. `apps/backend/tests/benchmarks/qa_benchmark_dataset_handymanapp.jsonl` — append 24-34 new rows. Preserve the 26 existing rows in their current order. New rows can use IDs like `A-19, A-20, ...` continuing the existing `A-11..A-18` numbering, `B-19, B-20, ...` continuing `B-11..B-18`, etc.

   Per-tier numbering (proposed):
   - A-tier: existing A-11..A-18 (8) → add A-19..A-24 (6) → total 14
   - B-tier: existing B-11..B-18 (8) → add B-19..B-26 (8) → total 16
   - C-tier: existing C-09..C-14 (6) → add C-15..C-24 (10) → total 16
   - D-tier: existing D-07..D-10 (4) → add D-11..D-20 (10) → total 14

2. `apps/backend/tests/benchmarks/qa_benchmark_dataset.jsonl` — same 24-34 new rows appended to the multi-source dataset (they need to coexist with hosteddashboard rows).

   This duplicate maintenance is annoying. If the dataset infrastructure ever changes to be derived (split files generated from the master), refactor; for now the two files must stay in sync.

3. (optional) `apps/backend/tests/benchmarks/qa_benchmark_dataset_hosteddashboard.jsonl` — leave unchanged. Dashboard expansion is separate (`T-DATASET-HOSTEDDASHBOARD-EXPAND`, lower priority).

## Tests

- No new code tests required.
- Pre-existing dataset validators in `run_qa_benchmark.py::load_dataset` will reject malformed rows — running the bench's `--help` or the harness pytest suite both exercise this path.

## Acceptance

- Both `qa_benchmark_dataset_handymanapp.jsonl` and `qa_benchmark_dataset.jsonl` have the new rows appended in identical order.
- Total handymanapp rows: 50-60 (≥50 required, ≤60 to keep wallclock manageable).
- Per-tier counts: A ≥12, B ≥14, C ≥14, D ≥10.
- Smoke run (`--limit 5` against the first 5 new rows) completes without any hard validator failures.
- Manual review checklist applied to every new row; reviewer noted in commit message.

## Out of scope

- Dashboard dataset expansion (separate ticket).
- Cross-checking the new questions with cards-v2 (cards-v2 isn't built yet; this is the dataset for the n≥40 rebench that decides whether cards-v2 is justified).
- Auto-generating questions from card_text (closed-loop trap — never use cards as both answer-source and question-source for the same bench).
- Synthesizing D-tier questions for stacks beyond handymanapp.

## Workflow

This ticket is **human-in-loop**, not codex-autonomous. Reasonable execution path:

1. Spend 30-60 min picking target files (read through `HandymanApp-fresh/app/src/main/`).
2. Use MiniMax (cheap) or codex (sandbox-write into a scratch JSONL) to draft K candidates per file.
3. Manually review each candidate against the checklist above; reject ~30-50% (LLM-generated questions tend to over-include shallow A-tier and weak D-tier).
4. Append the surviving rows to both `.jsonl` files in identical order.
5. Commit with `data(bench): handymanapp dataset 26Q -> 50Q` (or whatever final count).

Estimate total wall-time: 3-5 hours human-in-loop.

## Why P2 not P1

T-JUDGE-HYBRID-V1 (P1) is the hard prerequisite — without it, the rebench at n≥40 is uninterpretable for the same reason the original Stage 19 was. Dataset expansion is mechanical; judge fix is conceptual.
