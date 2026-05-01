# Decisions

Last updated: 2026-05-01

## D-001 Single Runtime Remains The Default

Keep the backend on the current single-runtime orchestrator until governance, approval, audit, and the main workbench UI are stable.

Reason:

- The product still needs correctness and clarity in the existing path.
- Splitting into async workers or multiple services now would make debugging the current answer-chain issue harder.

## D-002 Recovery Files Are Authoritative For Session Resume

Use these files as the first recovery layer:

- `AGENTS.md`
- `PROJECT_CONTEXT.md`
- `CURRENT_STATE.md`
- `TASK_QUEUE.md`
- `DECISIONS.md`
- `SESSION_HANDOFF.md`

Reason:

- The local machine restarted and the active development context was lost.
- These files give future agents a stable, repo-local recovery entry.

## D-003 UI Must Follow Local References Strictly

The next UI pass should treat screenshots in `references/` as the visual source of truth, not just broad inspiration.

Constraints:

- pale fixed left sidebar around 240-270 px
- centered readable main content
- black user bubble and white assistant reply card
- black primary buttons
- light borders and minimal cards
- no gradient, no colorful dashboards, no technical status panels in the product surface

## D-004 Planner Output Is Not A Chat Answer

Do not expose planner objectives and step lists as the normal assistant answer in chat.

Reason:

- Users asked a repository question and received a plan/debug response.
- The assistant surface must show a final answer, a grounded no-evidence explanation, or a calm failure message.

Required follow-up:

- Fix backend `process_question` final output and frontend `MessageList` fallback behavior.

## D-005 Frontend LocalStorage Is Temporary Scaffolding

The current frontend uses localStorage for some workbench scaffolding such as login role, conversation title overrides, memory entries, and model choice.

Reason:

- T-025 focused on UI/product structure before backend persistence existed.

Constraint:

- Do not store raw provider API keys in frontend localStorage.
- T-026 must replace temporary local behavior with backend-backed APIs where sensitive or persistent.

## D-006 Browser Path Import Must Stay Compliant

The browser frontend can offer file, folder, and zip import affordances through user-granted file selection.

It must not claim it can read arbitrary local paths unless a backend, desktop shell, or explicit file access mechanism exists.

## D-007 Follow-up Turns Remain Auditable Tasks For Now

Same-chat follow-ups reuse `session_id` and render as one product conversation in the UI, but each turn is still persisted as a separate backend task.

Reason:

- Existing task/event/tool execution persistence gives an audit trail without adding a new conversation-message table yet.
- This is a pragmatic bridge until T-026 or a later persistence task introduces first-class conversation messages.

Constraint:

- Follow-up classification must use the marker-delimited user intent, not the whole context block.
- The UI must hide the context block and show only the user's actual follow-up text.

## D-008 Knowledge Delete is Hard Delete (T-026-B)

`DELETE /api/knowledge/documents/{id}` and `DELETE /api/knowledge/sources/{name}` physically remove DB rows and, for upload-owned sources, files on disk. No soft-delete / disable column exists.

Reason:

- Knowledge documents are derived artifacts: files can be re-uploaded, repos can be re-synced. A soft-delete tombstone buys no recovery value the source of truth does not already offer.
- Soft-delete adds query filters, index cost, and UI ambiguity ("is this hidden or gone?"). Not worth the complexity at the current product stage.
- Governance-level undo is still available via the task rollback path for patches that introduced the knowledge change.

Constraint:

- Delete must remain gated by the `knowledge:delete` permission (admin only in current `PERMISSION_MAP`).
- If compliance or legal later require retention/recall, switch to soft-delete by adding a `deleted_at` column and filtering in `KnowledgeService.list_documents` / `list_sources` — don't retrofit half the codebase.

## D-009 Scored Tickets Land In Two Commits (feat + bench evidence)

Any ticket whose acceptance criteria reference benchmark scores must land in two commits, not one:

1. `feat(<area>): <change description>` — the implementation only. No measurement artifacts. No bench numbers in the message.
2. `bench(<area>): record <ticket-id> results and decision` — the artifact files (`apps/backend/tests/benchmarks/runs/qa-run-*.jsonl`) plus a stage-log entry recording per-tier deltas and the accept/revert decision.

Reason:

- The implementation and the empirical evaluation have different revert semantics. If the bench fails to meet acceptance, only commit 2 needs to revert; commit 1 may still be useful as an experimental checkpoint or starting point for v2.
- Bundling them means a "didn't meet bar" outcome forces a two-thing revert, more churn, and pollutes the implementation's commit message with numbers that may be re-measured later.
- Codex's Stage 12 critique D6 surfaced this — bundled commits are bad history.

Constraint:

- Do not commit either half until the bench has been run with a strict-pinned judge (per T-BENCH-HARNESS-RESILIENCE) and `score_status="valid"` for all 34 questions in the artifact.
- The stage-log entry in commit 2 must explicitly record the accept/revert decision. If revert: commit 2 is `revert(<area>): drop <ticket-id> per bench` and commit 1's hash is recorded in the stage-log close summary.

## D-010 Stage 20A (Hybrid Judge) Promoted To PRIMARY

Cross-family rejudge of Stage 19 artifacts (handymanapp + dashboard via MiniMax) collapsed the dashboard→handymanapp gap from rule-judge **+25.12** to semantic-judge **+8.46**. Rule judge has a structural Android/Kotlin paraphrasing bias that systematically under-scores answers on stacks whose keypoint vocabulary diverges from natural-English connectives.

Stage 20 priority is therefore:

- **20A (hybrid judge) = PRIMARY**, not infrastructure. Rule-only is structurally fragile cross-stack and blocks every future cross-stack benchmark.
- **20B (answer prompt rewrite) = DEPRIORITIZED**. Answers are already comprehensive when judge is fair (handymanapp MiniMax 51.78 ≈ dashboard MiniMax 60.24).
- **20C (cards-v2) = NARROW + CONDITIONAL**. Scope only to C/D multi-file synthesis; commit only after re-bench at n≥40 valid confirms residual gap is real, not sample variance.

Reason:

- Without 20A, every Stage 20+ benchmark on a non-React stack will be misread the same way.
- A-tier handymanapp records like A-17 (rule 13.3 → MiniMax 73.3, with manual verification of all 3 keypoints literally present in answer) are unambiguous evidence the bias is in the judge, not the answer.
- MiniMax is not blanket-lenient: dashboard total delta is +0.92 (essentially zero), dashboard C-tier delta is **−4.12** (stricter than rule), and confirmed misses (B-12, C-11, D-09, D-10) stayed at 0.

Constraint:

- Stage 20A spec must add a second LLM judge family (restore Anthropic credit or add OpenAI) before tightening claims further. Single-LLM-judge view is suggestive but not definitive.
- Hybrid judge does NOT replace rule judge. Rule = strict lexical conformance; LLM = semantic coverage; both kept and reported. See `docs/ai/specs/stage20-judge-verdict.md` for the hybrid-judge sketch and full caveats.
- D-tier conclusions are out of scope of this decision (n=1 vs n=3, insufficient sample).
- Independent Stage 19 follow-up: bench `question_timeout_seconds=240s` is too short for handymanapp (rejudge rescued 4 timeout records where backend actually completed). Raise to 360-480s in next handymanapp run.

### D-010 Amendment (2026-05-01 same day): V1 ships as MM-only, hybrid deferred to V2

Hybrid judge implementation landed in `feat/judge-hybrid-v1` and `T-JUDGE-HYBRID-V1-FIX` and was smoked. Manual inspection of the 10 rule-vs-MiniMax disagreement cases (where rule fired but MM did not) found:

- TP (rule legitimately catches MM false negative): 2
- FP (rule fires but answer doesn't cover keypoint): 3
- Ambiguous: 5

Net signal `TP − FP = −1`. Even the most generous reading of ambiguous cases (all → TP) gives 7/10 with a 30% false-positive rate on rule's "lift over MM" — too noisy to trust as primary judge. **V1 is therefore retired-as-default and replaced by `T-JUDGE-DEFAULT-MINIMAX-V1`**: the default `--judge-mode` becomes `minimax`, `auto` is removed (silent fallback to rule was a benchmarking footgun), and `hybrid` stays in the codebase as an experimental flag.

The original D-010 framing ("Stage 20A = hybrid judge primary") is **superseded** by:

- **Stage 20A V1 = MM-only semantic judge** (this amendment).
- **Stage 20A V2 = cross-family hybrid** — deferred to `T-JUDGE-HYBRID-V2.md`, gated on (a) second LLM family available (Anthropic credit or OpenAI key) AND (b) `T-JUDGE-AMBIG-CALIBRATION` calibration dataset complete.

V1 ships with explicit single-family caveat in artifact metadata (`judge_family_count`, `cross_family_validated`, `judge_caveats`) so downstream readers cannot mistake an MM-only run for a cross-family-validated one.

Constraint amendments:

- Stage 20C (cards-v2) decision still gated on n≥40 rebench under V1 (MM-only). The cross-stack gap to interpret is `+8.46` (rule rejudge to MM rejudge), not the hybrid `+4.84` (which we now know is partly artifact).
- The hybrid scaffolding in `feat/judge-hybrid-v1` should be merged into checkpoint as experimental code (not deleted), so V2 has a base to build on.
- Auto judge mode removal is mandatory; smoke runs must pin a mode.

### D-010 Second amendment (2026-05-01 same day): V2-CLI lands as diagnostic, not promoted to official

T-JUDGE-HYBRID-V2-CLI implementation landed (`feat/judge-hybrid-v2-cli` merged at commit `5387a21`). It uses Codex CLI (subscription, no API budget needed) as the second LLM judge family, AND-gated against MiniMax. The original V2 deferral assumption ("Anthropic credit OR OpenAI key required") was wrong — Codex CLI satisfies cross-family without API budget.

V2 promotion criteria check (from spec): **2/7 thresholds failed**. V2 does NOT auto-promote to official default.

Quantitative results (apples-to-apples, valid in rule + MM + V2):

| Dataset | Rule | MM (V1) | V2 | V2 − MM |
|---|---|---|---|---|
| Dashboard (n=25) | 59.32 | 60.24 | **52.72** | -7.52 |
| Handymanapp (n=17) | 34.20 | 51.78 | **47.08** | -4.71 |
| Cross-stack gap | +25.12 | +8.46 | **+5.64** | (narrowest) |

Cross-family agreement: dashboard 92%, handymanapp 95%. Codex CLI failure rate: 0% (post UTF-8 stdin fix).

The V2 mean drops below V1 MM-only by 4-8 points. **This is by design** — AND-gate forces `hit_score = 1.0` ONLY when both LLMs agree, so MM-yes-Codex-no kps that V1 credited 1.0 now score 0.0. The drop is not a quality regression; it's the conservative invariant the V2 spec required (`V2_mean ≤ V1_MM_only_mean` per dataset). The promotion criterion was set at ±3 points which is too tight given the math; relaxing to ±10 would have passed, but that defeats the "guard against silent over-promotion" intent.

V2 verdict: **stays as `--judge-mode hybrid_v2` for cross-family diagnostic**. V1 (`--judge-mode minimax`) remains official default. V2's value is the disagreement taxonomy, not the score.

Most actionable diagnostic from V2 (highest-leverage Stage 20C input):

- `both_no_evidence_yes` count = **50 keypoints** (dashboard 34 + handymanapp 16) — retrieval found the expected file with the keypoint substring, but neither MM nor Codex saw the answer articulating it.
- `both_no_rule_no_evidence_no` count = **62 keypoints** — true misses (cards/retrieval/synth all failed).
- `mm_yes_codex_no` count = **14 keypoints** — MM credit Codex refuses (Codex tilts slightly stricter than MM).

Stage 20C reframe (data-locked, not opinion-locked):

- The 50 `both_no_evidence_yes` count is suggestive of a synthesis articulation gap. **Not yet confirmed** — needs sample inspection to distinguish real synth misses from evidence-rung false-positive hits (e.g., card_text containing keypoint substring incidentally).
- If sample inspection confirms ≥70% real synth gap → synthesizer A/B experiment becomes Stage 20C P0, cards-v2 becomes parallel P1 (62 true-miss kps).
- If sample inspection shows the signal is weak → cards-v2 stays P0.

Constraint amendments:

- V2 mode `hybrid_v2` ships in code but is NOT the default. CLI `--judge-mode minimax` (V1) remains default.
- The Codex CLI judge subprocess MUST use UTF-8 encoding for stdin (commit `fb8afa7` fix). Reverting this would cause silent failures on non-ASCII prompts.
- Future cross-family validations (`hybrid_v2`) need Codex CLI or Claude Code CLI installed and authenticated — both work via subscription, no marginal cost.
- The `T-JUDGE-HYBRID-V2.md` placeholder is superseded by `T-JUDGE-HYBRID-V2-CLI.md` (kept for audit trail).
- The `T-JUDGE-AMBIG-CALIBRATION.md` calibration dataset is no longer required for V2 — V2 ships as diagnostic without it. Calibration is queued only if V2 ever needs to be promoted to official.
