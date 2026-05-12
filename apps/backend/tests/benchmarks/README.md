# QA Benchmark

- Dataset: `qa_benchmark_dataset.jsonl` with 34 English questions split A/B/C/D = 10/10/8/6.
- Runner: `apps/backend/scripts/run_qa_benchmark.py`.
- Default backend target: `http://127.0.0.1:8002`.
- The runner sends the same `/api/tasks` payload shape as the chat UI plus `X-Actor-Role: employee` and `X-Actor-App-Role: member`.

## Run

1. Start the backend on port `8002`.
2. From the repo root, run:
   `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe apps/backend/scripts/run_qa_benchmark.py --backend-url http://127.0.0.1:8002 --judge-mode auto`
3. For a smoke run, add:
   `--limit 3`

## Outputs

- Artifacts land in `apps/backend/tests/benchmarks/runs/qa-run-<UTC-timestamp>.jsonl`.
- The first JSONL line is a summary header; each later line is one question result.
- `--judge-mode auto` uses MiniMax when `OPS_AGENT_MINIMAX_API_KEY` is configured and falls back to the rule judge otherwise.
- Each record includes score, keypoint hits, citations found, judge mode, task status, duration, and a truncated answer excerpt.
- Exit code is `0` only when at least 90% of A+B+C questions reach a terminal task status and no infrastructure failure is detected.
