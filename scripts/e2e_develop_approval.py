"""End-to-end driver for T-039: develop task → Jira transition approval gate.

Exercises the full frontend-equivalent flow via HTTP:

    1. POST /api/tasks  with a develop-scenario request
    2. Poll GET /api/tasks/{id} until status == AWAITING_APPROVAL
    3. GET /api/approvals (list), find the matching jira.transition_issue row
    4. POST /api/approvals/{id}/grant  (or /reject if --reject)
    5. Poll GET /api/tasks/{id} until status is a terminal state
    6. Print evidence JSON (task.status, approval decisions, jira_transitioned)

Usage:
    python scripts/e2e_develop_approval.py --request "remove Minij from src/a.py"
    python scripts/e2e_develop_approval.py --reject

The script requires a running backend at BACKEND_URL (default
http://127.0.0.1:8000). It uses ``X-Actor-Role: admin`` so the default
permission map accepts both task:create and approval:decide.

Exit codes:
    0  = flow completed, terminal state reached
    1  = backend unreachable or HTTP error
    2  = task never parked at AWAITING_APPROVAL within TIMEOUT_SECONDS
    3  = task did not reach a terminal state within TIMEOUT_SECONDS
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib import error as urlerr
from urllib import request as urlreq

BACKEND_URL = "http://127.0.0.1:8000"
HEADERS = {
    "Content-Type": "application/json",
    "X-Actor-Role": "admin",
}
TIMEOUT_SECONDS = 300
POLL_INTERVAL = 2.0

TERMINAL_STATUSES = {"completed", "failed", "rolled_back"}


def _http(method: str, path: str, body: dict | None = None, timeout: int = 30) -> tuple[int, Any]:
    url = f"{BACKEND_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urlreq.Request(url, data=data, headers=HEADERS, method=method)
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urlerr.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw
        return exc.code, payload
    except urlerr.URLError as exc:
        print(f"[FATAL] Cannot reach backend at {BACKEND_URL}: {exc}", file=sys.stderr)
        sys.exit(1)


def _wait_for_status(task_id: str, predicate, label: str) -> dict:
    deadline = time.time() + TIMEOUT_SECONDS
    last_status = None
    while time.time() < deadline:
        code, task = _http("GET", f"/api/tasks/{task_id}")
        if code != 200:
            print(f"[ERROR] GET /api/tasks/{task_id} → {code}: {task}")
            sys.exit(1)
        status = task.get("status")
        if status != last_status:
            print(f"  [poll] status={status}")
            last_status = status
        if predicate(task):
            return task
        time.sleep(POLL_INTERVAL)
    print(f"[TIMEOUT] task never reached {label} within {TIMEOUT_SECONDS}s")
    sys.exit(2 if label == "AWAITING_APPROVAL" else 3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--request",
        default='implement TEST-1: remove "Minij" from src/a.py',
        help="Request text to submit as a develop task.",
    )
    parser.add_argument(
        "--reject",
        action="store_true",
        help="Reject the approval instead of granting it.",
    )
    parser.add_argument(
        "--notes",
        default="E2E driver decision",
        help="Reviewer notes attached to the approval decision.",
    )
    args = parser.parse_args()

    print(f"[1/5] POST /api/tasks  request={args.request!r}")
    # Task creation is synchronous and drives the full develop pipeline to
    # the approval gate before returning. That can take several minutes.
    code, task = _http(
        "POST",
        "/api/tasks",
        {"request": args.request, "actor_name": "e2e-driver", "actor_role": "employee"},
        timeout=600,
    )
    if code != 201:
        print(f"[ERROR] Task creation failed: {code} {task}")
        return 1
    task_id = task["id"]
    print(f"       created task_id={task_id}  initial_status={task.get('status')}")

    print("[2/5] polling for AWAITING_APPROVAL ...")
    parked = _wait_for_status(
        task_id, lambda t: t.get("status") == "awaiting_approval", "AWAITING_APPROVAL"
    )
    latest = parked.get("latest_result_json") or {}
    approval_id = latest.get("approval_id")
    print(f"       parked. approval_id={approval_id}")
    print(f"       preview diff first 120 chars: {(latest.get('result') or {}).get('diff', '')[:120]!r}")

    print("[3/5] GET /api/approvals → confirm jira.transition_issue row exists")
    code, approvals = _http("GET", "/api/approvals?status=pending")
    if code != 200:
        print(f"[ERROR] approval list failed: {code} {approvals}")
        return 1
    matching = [a for a in (approvals or []) if a.get("id") == approval_id]
    if not matching:
        print(f"[ERROR] approval {approval_id} not found in pending list")
        return 1
    approval = matching[0]
    print(f"       action_name={approval.get('action_name')} approver_role={approval.get('approver_role')}")

    decision = "reject" if args.reject else "grant"
    print(f"[4/5] POST /api/approvals/{approval_id}/{decision}")
    code, decided = _http(
        "POST",
        f"/api/approvals/{approval_id}/{decision}",
        {"actor_name": "e2e-driver", "actor_role": "team_lead", "notes": args.notes},
    )
    if code != 200:
        print(f"[ERROR] decision failed: {code} {decided}")
        return 1
    print(f"       decision={decided.get('status')}  decided_by={decided.get('decided_by_actor_name')}")

    print("[5/5] polling for terminal status ...")
    final = _wait_for_status(
        task_id, lambda t: (t.get("status") or "") in TERMINAL_STATUSES, "TERMINAL"
    )
    final_status = final.get("status")
    result = (final.get("latest_result_json") or {}).get("result") or {}
    print("\n=== EVIDENCE ===")
    print(f"task_id:                 {task_id}")
    print(f"final status:            {final_status}")
    print(f"jira_transitioned:       {result.get('jira_transitioned')}")
    print(f"jira_transition_rejected:{result.get('jira_transition_rejected')}")
    print(f"files_changed:           {result.get('files_changed')}")
    print(f"approval decision:       {decided.get('status')}")
    print(f"expected outcome:        "
          f"{'completed+jira_transitioned=false' if args.reject else 'completed+jira_transitioned=true'}")

    # Gate expectations:
    # - reject:  status=completed, jira_transitioned=False, jira_transition_rejected=True
    # - grant:   status=completed, jira_transitioned=True (if Jira config present)
    if args.reject:
        if final_status == "completed" and result.get("jira_transitioned") is False:
            print("\n[PASS] reject path preserved code + skipped Jira transition")
            return 0
        print("\n[FAIL] reject path did not match expected shape")
        return 1
    else:
        if final_status == "completed":
            print("\n[PASS] grant path reached completed")
            return 0
        print("\n[FAIL] grant path did not reach completed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
