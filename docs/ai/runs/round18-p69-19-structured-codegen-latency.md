# Round18 P69-19 Structured Codegen Latency

Date: 2026-05-14

## Summary

Round18 reached `awaiting_approval` with the new Kotlin structured JSON edit path enabled.

- Task id: `8bf4c02d-525f-46f5-aa7a-cbc00152ee44`
- Outcome: `awaiting_approval`
- Total wall time: about 36m34s, from 11:50:06Z to 12:26:41Z.
- Contract coverage: passed, 6/6 required contracts implemented.
- Acceptance check: passed, 6 tests.
- Compile: passed after compile repair.
- SymbolGraph ref validity: passed, 0 violations.
- Runtime validation: passed.
- Spec conformance and goal attestation: passed.

## Codegen Timing

| Item | Timing | Notes |
| --- | ---: | --- |
| Planner | about 296s | 11:50:16Z to 11:55:12Z |
| Batch 1 | about 110s | `CustomerKYCAddressForm.kt`, `deepseek:structural_kotlin`, +2/-1 |
| Batch 2 | about 791s | `CustomerSignup.kt`, late result after timeout |
| Batch 3 | about 584s | `HandymanKYCAddressForm.kt`, fallback raw diff |
| Batch 4 | about 1074s | `HandymanSignup.kt`, late result after timeout |
| Compile command | about 34s | `:app:compileDebugKotlin` passed |

## Pattern Evidence

Final diff pattern scan:

```json
{
  "MapView": true,
  "MapEventsReceiver": true,
  "GeocoderGetFromLocation": true,
  "GeocoderLocale": true,
  "FirebaseWrite": true,
  "OSMDroid": true,
  "NoGoogleMaps": true,
  "FilesChanged": 4,
  "Added": 362,
  "Removed": 14
}
```

## Important Correctness Finding

Round18 exposed a platform artifact bug after reaching approval:

- Compile repair fixed the sandbox working tree.
- The compile gate passed against that repaired tree.
- The approval diff artifact could still be stale because compile repair applied patches with `commit=False` and the pipeline did not regenerate `pipeline_state["diff"]`, `codegen_result["diff"]`, or `attempts/001/diff.patch` from the final sandbox tree.

Fix added after the run:

- Regenerate final diff from `pre_codegen_snapshot_id` after compile repair passes.
- Update `pipeline_state`, `codegen_result`, `files_changed`, and workspace `diff.patch`.
- Add a regression test proving uncommitted repair edits appear in the approval diff.

## Interpretation

The architecture direction is now clearer:

- Structured JSON edits help, but only where the harness owns final diff generation.
- The main runtime bottleneck is still long-tail initial codegen, not deterministic repair.
- Approval is reachable, but the platform must treat final-tree diff refresh as mandatory before trusting downstream gates and approval artifacts.

