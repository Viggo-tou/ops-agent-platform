# QA Complex Failure Analysis

Artifact analyzed: `apps/backend/tests/benchmarks/runs/qa-run-20260424T001902Z.jsonl`

- `D-01` (`10.00`) - `retrieval-miss`: the answer talked about `Login.js` plus unrelated user pages, but missed `UserContext.js`, `Sidebar.js`, and `App.js`, which are the key files for the session-centralization question.
- `D-02` (`0.00`) - `other`: the request failed with a reviewer/policy rejection before any grounded answer was produced, so this miss is not a normal retrieval-or-synthesis failure.
- `D-03` (`10.00`) - `planner-would-help`: the run only surfaced a thin slice of `HandymanVerification.js` and never joined it with `VerificationModal.js` / `ConfirmModal.js`; a decomposition step would likely have broken the flow into document approval, manual override, and phone-confirm sub-questions.
- `D-04` (`20.00`) - `retrieval-miss`: the answer partially used `JobManagement.js` and `JobCategoryStats.js`, but missed `Dashboard.js`, `ServiceAnalytics.js`, and `jobCategories.js`, which are the files needed to explain the reporting inconsistency across job data paths.
- `D-05` (`0.00`) - `planner-would-help`: the anchor gate said the support-reply pipeline terms were absent even though `SupportFeedback.js` contains the actual flow; a planner could have translated the question into repo-native anchors like `SupportFeedback`, `support_requests`, and `emailjs.send`.
- `D-06` (`20.00`) - `synthesis-miss`: the run cited the two right page files, but the answer stayed at a shallow UI comparison and did not synthesize the data-branch and derived-metric differences the question asked for.

Recommended next ticket title: `Phase 3.X - source-only retrieval + planner-assisted multi-hop QA`
