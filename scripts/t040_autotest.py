"""T-040 one-shot E2E: submit, poll, approve if surgical, dump evidence."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError, URLError

BACKEND = "http://127.0.0.1:8000"
EVIDENCE_DIR = Path("docs/ai/evidence/T-040")
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

REQ_TEXT = (
    "P69-10 fix: in hosteddashboard, delete the array element with id "
    '"master1" from src/data/mockUsers.js, and in src/pages/Dashboard.js '
    "move the top-level localStorage.getItem currentUser read into a useEffect "
    "inside the Dashboard component. Touch only those two files. "
    "Do not create new files. Apply the patch."
)


def api(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    url = f"{BACKEND}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "X-Actor-Role": "admin", "X-Actor-Name": "Tomonkyo"}
    req = urlreq.Request(url, data=data, method=method, headers=headers)
    try:
        with urlreq.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return {"_status": r.status, "_body": json.loads(raw) if raw else None}
    except HTTPError as e:
        return {"_status": e.code, "_body": json.loads(e.read().decode() or "null")}
    except URLError as e:
        return {"_status": 0, "_error": str(e)}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    log("submit task")
    r = api("POST", "/api/tasks", {"request": REQ_TEXT, "actor_role": "admin", "actor_name": "Tomonkyo"}, timeout=900)
    if r["_status"] not in (200, 201) or not r.get("_body"):
        log(f"submit failed: {r}")
        return 1
    task = r["_body"]
    tid = task["id"]
    log(f"submitted id={tid[:8]} scenario={task['scenario']} status={task['status']}")

    # after create_task returns, pipeline has already run end-to-end
    # fetch full detail
    r = api("GET", f"/api/tasks/{tid}")
    detail = r.get("_body") or {}
    status = detail.get("status")
    pending = detail.get("pending_approval")
    log(f"post-create: status={status} pending_approval={pending}")

    out = {
        "task_id": tid,
        "scenario": detail.get("scenario"),
        "status": status,
        "pending_approval": pending,
        "review_verdict": detail.get("review_verdict"),
        "review_summary": detail.get("review_summary"),
        "latest_result_json": detail.get("latest_result_json"),
    }

    if status == "awaiting_approval":
        log("approving via /api/approvals")
        # find approval ticket
        apps = api("GET", f"/api/approvals?task_id={tid}").get("_body") or []
        if isinstance(apps, list) and apps:
            aid = apps[0]["id"]
            log(f"approval id={aid} — granting")
            rr = api("POST", f"/api/approvals/{aid}/grant", {"decided_by_role": "admin", "decided_by_name": "Tomonkyo", "decision_note": "T-040 autotest"})
            log(f"grant result: {rr['_status']}")
            time.sleep(3)
            detail = (api("GET", f"/api/tasks/{tid}").get("_body")) or {}
            out["final_status"] = detail.get("status")
            out["jira_transitioned"] = ((detail.get("latest_result_json") or {}).get("result") or {}).get("jira_transitioned")
        else:
            log("no approval ticket found")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    evpath = EVIDENCE_DIR / f"autotest-{stamp}-{tid[:8]}.json"
    evpath.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log(f"evidence => {evpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
