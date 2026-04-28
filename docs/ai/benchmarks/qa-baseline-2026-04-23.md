# QA Baseline 2026-04-23

Run executed on 2026-04-24 from `2026-04-24T00:19:02Z` to `2026-04-24T00:26:09Z` (427s) against `http://127.0.0.1:8002`.

- Artifact: `apps/backend/tests/benchmarks/runs/qa-run-20260424T001902Z.jsonl`
- Backend commit SHA: `adeb67256f1a51f6d4c8f3706fc6f4d9b669db66`
- Judge request: `auto`
- Judge version/model: `MiniMax-M2.7`
- Judge mode actually used: `rule` for all 34 questions
- Judge fallback reason: `OPS_AGENT_MINIMAX_API_KEY not configured`
- Completion: 34/34 questions reached terminal status, 0 timed out

## Per-Tier Scores

| Tier | Mean | Min | Max |
| --- | ---: | ---: | ---: |
| A | 5.50 | 0.00 | 15.00 |
| B | 8.00 | 0.00 | 10.00 |
| C | 29.62 | 10.00 | 55.00 |
| D | 10.00 | 0.00 | 20.00 |

A+B+C aggregate mean: `13.29 / 100` across 28 questions.

Baseline currently fails acceptance - likely cause: source retrieval is polluted by build artifacts and irrelevant files, and some Q&A prompts still leak into Jira/policy/anchor-rejection paths instead of staying inside the repository-grounded shortcut flow.

## Top-3 Lowest-Scoring Questions

1. `A-08` (`0.00`, failed): the support-page locate question escaped the Q&A path and tried to create a Jira issue, ending in an Atlassian 400 response instead of a repository answer. Diagnosis: scenario routing is still brittle for support/ticket wording.
2. `B-03` (`0.00`, failed): the job-management explain question was rejected with `anchors not found` even though `JobManagement.js` exists in the benchmark repo. Diagnosis: the anchor gate is over-literal and can reject natural-language QA prompts before retrieval ever reaches the right file.
3. `A-05` (`0.00`, completed): the reusable export-control locate question pulled license files and bundled chunk output instead of `src/components/ExportReportButton.js`, then answered as if the question was incomplete. Diagnosis: retrieval ranking is not source-only and lets build artifacts dominate easy locate questions.
