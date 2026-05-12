"""Replay harness for the spec-conformance gate.

Loads every JSON fixture under ``docs/ai/fixtures/conformance/`` and runs
``check_spec_conformance`` against the recorded diff + source tree. Fails
the run (non-zero exit) when an observed verdict or rule set drifts from
the fixture's expectation.

Intended use: regression guard. Add a fixture when a real Jira run
exhibits a shape worth locking in (pass or block).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "backend"
FIXTURE_DIR = REPO_ROOT / "docs" / "ai" / "fixtures" / "conformance"

# Make app.services importable without installing the backend.
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.spec_conformance import check_spec_conformance  # noqa: E402


def _materialize_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _run_one(fixture_path: Path) -> tuple[bool, str]:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp)
        _materialize_tree(tree, data.get("source_tree_files") or {})
        report = check_spec_conformance(
            request_text=data.get("request_text"),
            normalized_request=data.get("normalized_request"),
            diff=data.get("diff") or "",
            source_tree=tree,
            must_touch_files=data.get("must_touch_files") or [],
        )
    observed_verdict = report.verdict
    observed_rules = sorted({f.rule for f in report.findings if f.severity == "block"})
    expected_verdict = data["expected_verdict"]
    expected_rules = sorted(data.get("expected_rules") or [])

    if observed_verdict != expected_verdict:
        return False, (
            f"verdict drift: expected {expected_verdict!r}, got {observed_verdict!r}; "
            f"blocking rules={observed_rules}"
        )
    if expected_verdict == "block" and set(expected_rules) - set(observed_rules):
        missing = sorted(set(expected_rules) - set(observed_rules))
        return False, f"expected rules not triggered: {missing} (observed={observed_rules})"
    return True, f"verdict={observed_verdict}, rules={observed_rules}"


def main() -> int:
    fixtures = sorted(FIXTURE_DIR.glob("*.json"))
    if not fixtures:
        print(f"no fixtures under {FIXTURE_DIR}")
        return 1

    failures = 0
    for path in fixtures:
        try:
            ok, detail = _run_one(path)
        except Exception as exc:  # pragma: no cover - harness robustness
            ok, detail = False, f"exception: {exc!r}"
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {path.name}: {detail}")
        if not ok:
            failures += 1

    print()
    print(f"{len(fixtures) - failures}/{len(fixtures)} fixtures passed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
