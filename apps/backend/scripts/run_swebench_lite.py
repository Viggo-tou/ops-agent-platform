"""SWE-bench-Lite adapter for the Ops Agent Platform develop pipeline.

What this script does
---------------------
For each task in `apps/backend/tests/benchmarks/swebench_lite_subset_50.jsonl`:
  1. Materialize the upstream repo at ``base_commit`` under
     ``--clones-dir`` (default ``/tmp/swebench``). Uses a tarball download
     (fast) when possible, falls back to ``git clone --filter=blob:none``.
  2. Register that working tree as a managed knowledge source via the
     in-process repository_registry module so the orchestrator can find
     it by ``source_name``.
  3. POST a task to ``--backend-url`` with the SWE-bench problem statement
     prepended by a fake Jira reference so the request is classified as
     ``jira_issue_develop``.
  4. Poll ``GET /api/tasks/{id}`` until the task reaches a terminal state
     (``completed`` / ``awaiting_approval`` / ``failed`` / ``stale_failed``)
     or the per-task timeout fires.
  5. Pull the produced unified diff out of the task's events / result and
     append a SWE-bench prediction record to the run's predictions.jsonl.

The output predictions file is the input to the official
``swebench.harness.run_evaluation`` Docker-based grader. We don't run
the grader here — that's a separate step the user kicks off after the
harness completes.

CLI
---
    python apps/backend/scripts/run_swebench_lite.py \
        --backend-url http://127.0.0.1:8000 \
        --limit 1                                    # smoke
    python apps/backend/scripts/run_swebench_lite.py \
        --backend-url http://127.0.0.1:8000          # full 50

Outputs
-------
- ``apps/backend/tests/benchmarks/runs/swebench-lite-<timestamp>/``
    - ``predictions.jsonl`` — one row per task, swebench-evaluator-ready
    - ``run-meta.jsonl``    — first row = run metadata, rest = per-task
                              detail (status, duration, token est, etc.)

The script is idempotent on resume: pass ``--resume <run-dir>`` to
re-attempt only the tasks that previously errored or timed out.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[3]

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Load backend .env so MCP / provider / model config picked up when the
# runner is invoked from the repo root.
_BACKEND_ENV = BACKEND_ROOT / ".env"
if _BACKEND_ENV.is_file():
    for _line in _BACKEND_ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k and _k not in os.environ:
            os.environ[_k] = _v

from app.services import repository_registry  # noqa: E402

DEFAULT_SUBSET = REPO_ROOT / "apps" / "backend" / "tests" / "benchmarks" / "swebench_lite_subset_50.jsonl"
DEFAULT_CLONES_DIR = Path(tempfile.gettempdir()) / "swebench"
DEFAULT_OUT_DIR = REPO_ROOT / "apps" / "backend" / "tests" / "benchmarks" / "runs"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_TASK_TIMEOUT_SECONDS = 30 * 60  # 30 min per task
POLL_INTERVAL_SECONDS = 5.0
TERMINAL_STATUSES = {"completed", "failed", "stale_failed", "awaiting_approval", "rolled_back"}
ACTOR_HEADERS = {"X-Actor-Name": "swebench", "X-Actor-Role": "admin"}
PROBLEM_PREFIX = ""  # scenario_override forces develop; no fake Jira ref needed.


# --------------------------------------------------------------------------
# 1) repo materialization
# --------------------------------------------------------------------------


def materialize_repo(repo: str, base_commit: str, dest: Path) -> bool:
    """Materialize ``repo`` at ``base_commit`` under ``dest``.

    Uses a shallow git clone with ``core.longpaths=true`` so Windows
    doesn't choke on django's deeply-nested doc theme paths (PNGs in
    docs/_theme/djangodocs-epub/static exceed the 260-char limit). The
    tarball-via-codeload fastpath we tried first failed on those same
    files because subprocess tar doesn't honor the long-path API.

    Idempotent: a ``swebench.ready`` marker inside ``dest`` lets repeat
    invocations skip the clone entirely. On any failure we wipe ``dest``
    so a retry isn't tripped up by a half-cloned tree.
    """
    if (dest / "swebench.ready").exists():
        print(f"  cached: {dest}", flush=True)
        return True

    # Wipe leftovers from any prior failed attempt so the clone target is
    # clean. ignore_errors=True because dest may not exist on first run.
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)

    print(f"  clone --filter=blob:none {repo}", flush=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # core.longpaths=true is the Windows fix; harmless on POSIX.
    base_args = ["git", "-c", "core.longpaths=true"]

    try:
        subprocess.run(
            base_args
            + [
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                f"https://github.com/{repo}.git",
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        subprocess.run(
            base_args + ["fetch", "origin", base_commit, "--depth=1"],
            check=True,
            capture_output=True,
            cwd=dest,
            timeout=300,
        )
        subprocess.run(
            base_args + ["checkout", base_commit],
            check=True,
            capture_output=True,
            cwd=dest,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print(f"    git clone timed out for {repo}@{base_commit[:8]}", flush=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[:300]
        print(f"    git clone failed: {stderr}", flush=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        return False

    (dest / "swebench.ready").touch()
    return True


# --------------------------------------------------------------------------
# 2) repository registry registration
# --------------------------------------------------------------------------


def register_source(name: str, path: Path, repo: str) -> None:
    """Add the materialized clone as a managed source.

    Idempotent — overwrites the prior registry entry with the same name.
    """
    record = repository_registry.SourceRecord(
        name=name,
        path=str(path),
        origin="clone",
        description=f"swebench-lite source for {repo}",
        git_url=f"https://github.com/{repo}.git",
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    # Drop existing entry with the same name first so add_managed_source
    # doesn't reject a duplicate.
    try:
        repository_registry.remove_managed_source(name)
    except Exception:  # noqa: BLE001
        pass
    repository_registry.add_managed_source(record)


def sync_kb(backend_url: str, source_name: str) -> tuple[int, str | None]:
    """Trigger FTS indexing for the freshly-registered source.

    Without this the orchestrator's KB retrieval falls back to LLM
    routing / glob scans against an empty FTS table and codegen gets
    little-to-no context. Returns (indexed_doc_count, error_or_None).
    """
    try:
        resp = httpx.post(
            f"{backend_url}/api/knowledge/sync",
            params={"source_name": source_name},
            headers=ACTOR_HEADERS,
            timeout=600.0,
        )
        if resp.status_code >= 400:
            return 0, f"{resp.status_code}: {resp.text[:300]}"
        body = resp.json()
        return int(body.get("indexed_documents") or 0), None
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# 3) task submission + polling
# --------------------------------------------------------------------------


def submit_task(
    backend_url: str, problem_statement: str, source_name: str, task_index: int
) -> str:
    request_text = (
        PROBLEM_PREFIX.format(n=task_index)
        + problem_statement.strip()[:3500]
    )
    payload = {
        "request": request_text,
        "actor_name": "swebench",
        "actor_role": "employee",
        "source_name": source_name,
        # Force develop scenario — long SWE-bench problem statements
        # naturally contain "complete" / "done" / "fix" which trip the
        # writeback classifier without this override.
        "scenario_override": "jira_issue_develop",
        # SWE-bench tasks are not real Jira tickets; the develop pipeline
        # would otherwise 404 on jira.get_issue and fail before any code
        # got generated.
        "skip_jira_prefetch": True,
    }
    # Bumped from 60 → 180 because under parallel=4 the backend serialises
    # task-create writes against SQLite's single writer + retry-on-locked
    # backoff and a single submit can wait several seconds for its slot.
    resp = httpx.post(
        f"{backend_url}/api/tasks",
        json=payload,
        headers=ACTOR_HEADERS,
        timeout=180.0,
    )
    if resp.status_code != 201 and resp.status_code != 200:
        raise RuntimeError(f"create task {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    return data["id"]


def poll_until_terminal(
    backend_url: str, task_id: str, timeout_seconds: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{backend_url}/api/tasks/{task_id}",
                headers=ACTOR_HEADERS,
                # Bumped 30s → 60s for v10: full-DeepSeek reviewer makes
                # /api/tasks/{id} occasionally slower than 30s when
                # gates run synchronously. ReadTimeout was seen on v10
                # task 2 (django-11283) under DeepSeek reviewer load.
                timeout=60.0,
            )
            resp.raise_for_status()
            last = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"    poll err: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        status = last.get("status")
        if status in TERMINAL_STATUSES:
            return last
        time.sleep(POLL_INTERVAL_SECONDS)
    return last or {"status": "timeout"}


# --------------------------------------------------------------------------
# 4) diff extraction
# --------------------------------------------------------------------------


def extract_diff(backend_url: str, task: dict[str, Any]) -> str:
    """Pull the produced unified diff out of the task's events.

    The develop pipeline emits the diff in `apply_patch_succeeded` /
    `codegen_completed` events with `payload_json.diff`. The most
    reliable place to read it is the events list rather than
    latest_result_json (which sometimes only has a summary).
    """
    candidates: list[str] = []
    # Try latest_result_json first.
    result = task.get("latest_result_json") or {}
    if isinstance(result, dict):
        for key in ("diff", "patch", "produced_diff", "merged_diff"):
            value = result.get(key)
            if isinstance(value, str) and value.startswith("diff --git"):
                candidates.append(value)

    task_id = task.get("id")
    if task_id:
        try:
            resp = httpx.get(
                f"{backend_url}/api/tasks/{task_id}/events",
                headers=ACTOR_HEADERS,
                timeout=60.0,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"    diff fetch (events) err: {exc}")
            events = []
        # Walk events newest-first, prefer ones that look like a final diff.
        for ev in reversed(events):
            payload = ev.get("payload_json") or ev.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            for key in ("diff", "patch", "merged_diff", "produced_diff", "final_diff"):
                value = payload.get(key)
                if isinstance(value, str) and value.startswith("diff --git"):
                    candidates.append(value)

    # Pick the longest as a heuristic for "most-complete" diff.
    return max(candidates, key=len, default="")


# --------------------------------------------------------------------------
# top-level orchestration
# --------------------------------------------------------------------------


_FILE_LOCK = threading.Lock()
# Backend write paths don't all retry on "database is locked": KB sync
# (1000-row insert) and POST /api/tasks (Task row + initial event)
# both hit the issue under parallel=4. Holding this lock around both
# steps serializes the DB-heavy bootstrap; the pipeline itself still
# runs in parallel inside the backend's worker pool, so we keep most
# of the 4x speedup. Clone / poll / extract are unlocked.
_BACKEND_WRITE_LOCK = threading.Lock()


def _process_one(
    *,
    idx: int,
    total: int,
    task: dict[str, Any],
    backend_url: str,
    clones_dir: Path,
    task_timeout_seconds: float,
    predictions_path: Path,
    meta_path: Path,
) -> None:
    """Run one SWE-bench task end-to-end.

    Safe to invoke from multiple threads concurrently — file writes are
    serialized via _FILE_LOCK; backend operations rely on the orchestrator
    + repository_registry's own internal locking.
    """
    instance_id = task["instance_id"]
    repo = task["repo"]
    base_commit = task["base_commit"]
    problem_statement = task["problem_statement"]

    print(f"[{idx}/{total}] start {instance_id} ({repo}@{base_commit[:8]})", flush=True)

    slug = instance_id.replace("/", "_").replace("__", "-")
    clone_path = clones_dir / instance_id.replace("/", "_")
    source_name = f"swebench-{slug}"[:64]

    record: dict[str, Any] = {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    t0 = time.monotonic()

    try:
        ok = materialize_repo(repo, base_commit, clone_path)
        if not ok:
            raise RuntimeError("materialize_repo failed")

        register_source(source_name, clone_path, repo)

        with _BACKEND_WRITE_LOCK:
            indexed, sync_err = sync_kb(backend_url, source_name)
            if sync_err:
                raise RuntimeError(f"KB sync failed: {sync_err}")
            record["indexed_documents"] = indexed
            task_id = submit_task(backend_url, problem_statement, source_name, idx)
        record["task_id"] = task_id
        print(f"[{idx}/{total}] {instance_id} task_id={task_id} indexed={indexed}", flush=True)

        terminal = poll_until_terminal(backend_url, task_id, task_timeout_seconds)
        duration = time.monotonic() - t0
        record["status"] = terminal.get("status", "unknown")
        record["duration_seconds"] = round(duration, 1)
        record["workflow_stage"] = terminal.get("workflow_stage")
        record["pending_approval"] = terminal.get("pending_approval")
        record["review_verdict"] = terminal.get("review_verdict")
        record["review_summary"] = (
            (terminal.get("review_summary") or "")[:500] if terminal else None
        )

        diff = extract_diff(backend_url, terminal) if terminal else ""
        print(
            f"[{idx}/{total}] {instance_id} status={record['status']} "
            f"duration={duration:.0f}s diff_len={len(diff)}",
            flush=True,
        )

        prediction = {
            "instance_id": instance_id,
            "model_patch": diff,
            "model_name_or_path": "ops-agent-platform/dev-pipeline (deepseek-codegen + claude_code-planner)",
        }
        with _FILE_LOCK:
            with predictions_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(prediction, ensure_ascii=False) + "\n")

    except Exception as exc:  # noqa: BLE001
        duration = time.monotonic() - t0
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        record["duration_seconds"] = round(duration, 1)
        print(f"[{idx}/{total}] {instance_id} ERROR {record['error']}", flush=True)

    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    with _FILE_LOCK:
        with meta_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(
    *,
    subset_path: Path,
    backend_url: str,
    clones_dir: Path,
    out_dir: Path,
    limit: int | None,
    start: int,
    task_timeout_seconds: float,
    resume_run: Path | None,
    parallel: int,
) -> None:
    tasks: list[dict[str, Any]] = []
    with subset_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            tasks.append(json.loads(line))

    if start:
        tasks = tasks[start:]
    if limit is not None:
        tasks = tasks[:limit]
    if not tasks:
        print("no tasks to run; exiting")
        return

    if resume_run is not None:
        run_dir = resume_run
        run_dir.mkdir(parents=True, exist_ok=True)
        # Skip tasks whose predictions already exist with status != error/timeout.
        completed_ids = set()
        meta_path = run_dir / "run-meta.jsonl"
        if meta_path.exists():
            with meta_path.open("r", encoding="utf-8") as fp:
                for line in fp:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("instance_id") and row.get("status") not in {
                        None,
                        "error",
                        "timeout",
                    }:
                        completed_ids.add(row["instance_id"])
        before = len(tasks)
        tasks = [t for t in tasks if t["instance_id"] not in completed_ids]
        print(f"resume: {before - len(tasks)} tasks already completed, {len(tasks)} remaining")
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = out_dir / f"swebench-lite-{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

    predictions_path = run_dir / "predictions.jsonl"
    meta_path = run_dir / "run-meta.jsonl"

    if not meta_path.exists():
        with meta_path.open("w", encoding="utf-8") as fp:
            fp.write(
                json.dumps(
                    {
                        "kind": "run_header",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "subset_path": str(subset_path.relative_to(REPO_ROOT)),
                        "backend_url": backend_url,
                        "task_count": len(tasks),
                        "task_timeout_seconds": task_timeout_seconds,
                    }
                )
                + "\n"
            )

    print(f"\n=== run dir: {run_dir.relative_to(REPO_ROOT)} ===")
    print(f"=== tasks: {len(tasks)}, parallel: {parallel}, backend: {backend_url} ===\n")

    if parallel <= 1:
        for idx, task in enumerate(tasks, start=1):
            _process_one(
                idx=idx,
                total=len(tasks),
                task=task,
                backend_url=backend_url,
                clones_dir=clones_dir,
                task_timeout_seconds=task_timeout_seconds,
                predictions_path=predictions_path,
                meta_path=meta_path,
            )
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="swebench") as pool:
            futures = {
                pool.submit(
                    _process_one,
                    idx=idx,
                    total=len(tasks),
                    task=task,
                    backend_url=backend_url,
                    clones_dir=clones_dir,
                    task_timeout_seconds=task_timeout_seconds,
                    predictions_path=predictions_path,
                    meta_path=meta_path,
                ): idx
                for idx, task in enumerate(tasks, start=1)
            }
            for fut in as_completed(futures):
                # Surface unexpected exceptions; per-task errors are
                # already logged inside _process_one.
                exc = fut.exception()
                if exc is not None:
                    print(f"worker exception: {type(exc).__name__}: {exc}", flush=True)

    print(f"\n=== complete ===")
    print(f"predictions: {predictions_path.relative_to(REPO_ROOT)}")
    print(f"meta:        {meta_path.relative_to(REPO_ROOT)}")
    print()
    print("Next: run the SWE-bench evaluator on predictions.jsonl, e.g.")
    print(
        f"  python -m swebench.harness.run_evaluation \\\n"
        f"    --dataset_name princeton-nlp/SWE-bench_Lite \\\n"
        f"    --predictions_path {predictions_path} \\\n"
        f"    --max_workers 4 \\\n"
        f"    --run_id ops-agent-{run_dir.name}"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--subset", type=Path, default=DEFAULT_SUBSET)
    p.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    p.add_argument("--clones-dir", type=Path, default=DEFAULT_CLONES_DIR)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--start", type=int, default=0, help="0-indexed start within subset")
    p.add_argument(
        "--task-timeout-seconds", type=float, default=DEFAULT_TASK_TIMEOUT_SECONDS
    )
    p.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="resume into an existing run dir; skips already-finished tasks",
    )
    p.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="run N tasks concurrently. Capped by backend pipeline_max_workers (6).",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        subset_path=args.subset,
        backend_url=args.backend_url,
        clones_dir=args.clones_dir,
        out_dir=args.out_dir,
        limit=args.limit,
        start=args.start,
        task_timeout_seconds=args.task_timeout_seconds,
        resume_run=args.resume,
        parallel=max(1, int(args.parallel)),
    )
