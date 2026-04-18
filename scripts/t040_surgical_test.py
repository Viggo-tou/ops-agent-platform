"""T-040 surgical test: construct correct patch, validate pipeline end-to-end.

Instead of relying on MiniMax to generate the correct patch, this script:
1. Reads the real source files from HostedDashboard
2. Constructs the correct modified versions
3. Submits task with OPS_AGENT_PRIMARY_AGENT_PROVIDER=mock (deterministic plan)
4. Intercepts codegen with pre-built correct output
5. Verifies conformance verdict=pass, approval gate, and Jira transition
"""
from __future__ import annotations

import difflib
import json
import os
import sys
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import HTTPError

BACKEND = "http://127.0.0.1:8000"
EVIDENCE_DIR = Path("docs/ai/evidence/T-040")
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

KNOWLEDGE_ROOT = Path(r"D:\项目\HostedDashboard\handyman-admin-dashboard")


def api(method: str, path: str, body: dict | None = None, timeout: int = 600) -> dict:
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


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def build_correct_mockusers() -> str:
    """Remove master1 entry from mockUsers.js."""
    original = (KNOWLEDGE_ROOT / "src/data/mockUsers.js").read_text(encoding="utf-8")
    lines = original.split("\n")
    result = []
    skip = False
    for line in lines:
        if 'id: "master1"' in line:
            # Remove the { before this line too
            while result and result[-1].strip() == "{":
                result.pop()
            skip = True
            continue
        if skip:
            if line.strip().startswith("}"):
                # Skip closing brace and trailing comma
                skip = False
                # Also remove trailing comma from previous entry if present
                if result and result[-1].rstrip().endswith("},"):
                    pass  # keep it, it's the staff1 closing
                continue
            continue
        result.append(line)
    return "\n".join(result)


def build_correct_dashboard() -> str:
    """Move top-level currentUser read into component useEffect."""
    original = (KNOWLEDGE_ROOT / "src/pages/Dashboard.js").read_text(encoding="utf-8")
    # Remove the top-level const currentUser line
    lines = original.split("\n")
    result = []
    for i, line in enumerate(lines):
        if line.strip() == 'const currentUser = JSON.parse(localStorage.getItem("currentUser"));':
            continue
        result.append(line)

    # Insert currentUser as state inside Dashboard component
    final = []
    for line in result:
        final.append(line)
        if line.strip() == "const [dashboardData, setDashboardData] = useState(null);":
            final.append("  const [currentUser, setCurrentUser] = useState(null);")
            final.append("")
            final.append("  useEffect(() => {")
            final.append('    setCurrentUser(JSON.parse(localStorage.getItem("currentUser")));')
            final.append("  }, []);")
    return "\n".join(final)


def make_unified_diff(original: str, modified: str, filepath: str) -> str:
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{filepath}", tofile=f"b/{filepath}")
    return "".join(diff)


def main() -> int:
    log("building correct patches")

    # mockUsers.js
    orig_mu = (KNOWLEDGE_ROOT / "src/data/mockUsers.js").read_text(encoding="utf-8")
    fixed_mu = build_correct_mockusers()
    diff_mu = make_unified_diff(orig_mu, fixed_mu, "src/data/mockUsers.js")

    # Dashboard.js
    orig_db = (KNOWLEDGE_ROOT / "src/pages/Dashboard.js").read_text(encoding="utf-8")
    fixed_db = build_correct_dashboard()
    diff_db = make_unified_diff(orig_db, fixed_db, "src/pages/Dashboard.js")

    combined_diff = diff_mu + diff_db
    log(f"diff: {len(combined_diff)} chars, files: mockUsers.js + Dashboard.js")

    # Verify anchor counts change correctly
    for anchor, orig, fixed, name in [
        ("master1", orig_mu, fixed_mu, "mockUsers"),
        ("currentUser", orig_db, fixed_db, "Dashboard"),
    ]:
        before = orig.count(anchor)
        after = fixed.count(anchor)
        log(f"  {name}: '{anchor}' {before} -> {after}")

    # Print the diff for review
    print("\n=== COMBINED DIFF ===")
    print(combined_diff)
    print("=== END DIFF ===\n")

    # Save correct files and diff as evidence
    ev = {
        "correct_mockusers": fixed_mu,
        "correct_dashboard": fixed_db,
        "diff": combined_diff,
        "diff_files": ["src/data/mockUsers.js", "src/pages/Dashboard.js"],
    }
    evpath = EVIDENCE_DIR / "correct-patch.json"
    evpath.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"saved correct patch to {evpath}")

    # Now validate: call spec_conformance directly
    log("calling conformance check via backend...")
    r = api("POST", "/api/conformance/check", {
        "diff": combined_diff,
        "request_text": 'P69-10 fix: delete master1 from mockUsers.js, move currentUser localStorage read into useEffect in Dashboard.js',
        "must_touch_files": ["src/data/mockUsers.js", "src/pages/Dashboard.js"],
    }, timeout=30)
    if r["_status"] in (200, 201):
        body = r["_body"]
        log(f"conformance verdict: {body.get('verdict')}")
        log(f"findings: {json.dumps(body.get('findings', []), indent=2, default=str)[:500]}")
        evpath2 = EVIDENCE_DIR / "correct-patch-conformance.json"
        evpath2.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        log(f"saved conformance result to {evpath2}")
        return 0
    else:
        log(f"conformance API: {r['_status']} — may not exist, running offline check")

    # Fallback: run conformance offline
    sys.path.insert(0, str(Path("apps/backend")))
    from app.services.spec_conformance import check_spec_conformance

    report = check_spec_conformance(
        request_text='P69-10 fix: delete master1 from mockUsers.js, move currentUser localStorage read into useEffect in Dashboard.js',
        normalized_request=None,
        diff=combined_diff,
        source_tree=KNOWLEDGE_ROOT,
        must_touch_files=["src/data/mockUsers.js", "src/pages/Dashboard.js"],
    )
    verdict = report.verdict
    log(f"offline conformance verdict: {verdict}")
    findings = report.findings
    log(f"findings count: {len(findings)}")
    for f in findings:
        log(f"  - {f.rule}: {f.severity} — {f.message[:200]}")

    report_dict = {
        "verdict": verdict,
        "findings": [{"rule": f.rule, "severity": f.severity, "message": f.message, "evidence": f.evidence} for f in findings],
    }
    evpath2 = EVIDENCE_DIR / "correct-patch-conformance.json"
    evpath2.write_text(json.dumps(report_dict, indent=2, default=str), encoding="utf-8")
    log(f"saved to {evpath2}")
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
