from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

SCRIPT_PATH = Path(__file__).resolve()
BACKEND_ROOT = SCRIPT_PATH.parents[1]
REPO_ROOT = SCRIPT_PATH.parents[3]

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Load backend .env regardless of the caller's CWD so judge credentials
# (OPS_AGENT_ANTHROPIC_API_KEY, OPS_AGENT_MINIMAX_API_KEY) are picked up
# when the runner is invoked from the repo root / a worktree root.
_BACKEND_ENV = BACKEND_ROOT / ".env"
if _BACKEND_ENV.is_file():
    import os
    for _line in _BACKEND_ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _k = _k.strip()
        _v = _v.strip().strip('"').strip("'")
        if _k and _k not in os.environ:
            os.environ[_k] = _v

from app.core.config import get_settings

DEFAULT_DATASET_PATH = REPO_ROOT / "apps" / "backend" / "tests" / "benchmarks" / "qa_benchmark_dataset.jsonl"
DEFAULT_OUT_DIR = REPO_ROOT / "apps" / "backend" / "tests" / "benchmarks" / "runs"
TERMINAL_STATUSES = {"completed", "failed", "rolled_back"}
QUESTION_TIMEOUT_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 1.0
ANSWER_EXCERPT_MAX_BYTES = 2048
ACTOR_ROLE = "employee"
ACTOR_APP_ROLE = "member"
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "up",
    "uses",
    "with",
}


class InfrastructureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DatasetRow:
    id: str
    tier: str
    question: str
    expected_answer_keypoints: list[str]
    expected_citations: list[str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_cli_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (Path.cwd() / candidate).resolve()


def load_dataset(path: Path, limit: int | None) -> list[DatasetRow]:
    rows: list[DatasetRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            rows.append(
                DatasetRow(
                    id=str(payload["id"]),
                    tier=str(payload["tier"]),
                    question=str(payload["question"]),
                    expected_answer_keypoints=[str(item) for item in payload["expected_answer_keypoints"]],
                    expected_citations=[str(item) for item in payload["expected_citations"]],
                )
            )
    if limit is not None:
        return rows[:limit]
    return rows


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[: max_bytes - 3]
    while True:
        try:
            return clipped.decode("utf-8") + "..."
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def strip_json_fence(text: str) -> str:
    normalized = text.strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
        normalized = re.sub(r"\s*```$", "", normalized)
    return normalized.strip()


def normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = lowered.replace("\\", "/")
    lowered = re.sub(r"[^a-z0-9/._-]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9/._-]*", text.lower())
    cleaned: list[str] = []
    for token in tokens:
        if token in STOPWORDS:
            continue
        if len(token) >= 3 or any(char.isdigit() for char in token) or "/" in token or "_" in token or "." in token:
            cleaned.append(token)
    return cleaned


def token_matches(expected_token: str, answer_tokens: Sequence[str]) -> bool:
    for candidate in answer_tokens:
        if candidate == expected_token:
            return True
        if len(expected_token) >= 4 and candidate.startswith(expected_token):
            return True
        if len(candidate) >= 4 and expected_token.startswith(candidate):
            return True
    return False


def normalize_citation_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/").lower()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.replace(":", "/")
    src_index = normalized.find("/src/")
    if src_index != -1:
        return normalized[src_index + 1 :]
    if normalized.startswith("src/"):
        return normalized
    return normalized


def extract_answer_and_citations(task_payload: dict[str, Any]) -> tuple[str, list[str], list[str]]:
    latest_result = task_payload.get("latest_result_json")
    latest_result = latest_result if isinstance(latest_result, dict) else {}
    result = latest_result.get("result")
    result = result if isinstance(result, dict) else {}

    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        answer = latest_result.get("message")
    if not isinstance(answer, str):
        answer = ""

    display_citations: list[str] = []
    canonical_citations: list[str] = []
    raw_citations = result.get("citations")
    if isinstance(raw_citations, list):
        for item in raw_citations:
            if not isinstance(item, dict):
                continue
            relative_path = item.get("relative_path")
            source_name = item.get("source_name")
            if isinstance(relative_path, str) and relative_path.strip():
                citation_text = relative_path.strip()
                if isinstance(source_name, str) and source_name.strip():
                    citation_text = f"{source_name.strip()}/{citation_text}"
                display_citations.append(citation_text)
                canonical_citations.append(normalize_citation_path(relative_path))
    return answer.strip(), display_citations, canonical_citations


def extract_minimax_content(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list):
        raise RuntimeError("MiniMax response did not include choices")
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    raise RuntimeError("MiniMax response did not include assistant content")


class KeypointJudge:
    def __init__(self, requested_mode: str) -> None:
        self.requested_mode = requested_mode
        self.settings = get_settings()
        self.judge_model = self.settings.knowledge_synthesis_model or "MiniMax-M2.7"
        self.auto_rule_reason: str | None = None
        self._auto_force_rule = False

    def judge(self, *, question: str, answer: str, keypoints: Sequence[str]) -> tuple[list[bool], str]:
        if not answer.strip():
            return [False] * len(keypoints), "rule"

        if self.requested_mode == "rule":
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        if self.requested_mode == "anthropic":
            return self._judge_with_anthropic(question=question, answer=answer, keypoints=keypoints), "anthropic"

        if self.requested_mode == "minimax":
            return self._judge_with_minimax(question=question, answer=answer, keypoints=keypoints), "minimax"

        # auto: prefer Anthropic (cross-family vs MiniMax synthesizer →
        # avoids self-evaluation bias), fall back to MiniMax, then rule.
        if self._auto_force_rule:
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        if self.settings.anthropic_api_key:
            try:
                return self._judge_with_anthropic(question=question, answer=answer, keypoints=keypoints), "anthropic"
            except (httpx.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
                # Don't force-rule yet — fall through to MiniMax before giving up.
                self.auto_rule_reason = f"Anthropic judge failed, falling back to MiniMax: {exc}"

        if not self.settings.minimax_api_key:
            self._auto_force_rule = True
            self.auto_rule_reason = (
                self.auto_rule_reason
                or "Neither OPS_AGENT_ANTHROPIC_API_KEY nor OPS_AGENT_MINIMAX_API_KEY configured"
            )
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        try:
            return self._judge_with_minimax(question=question, answer=answer, keypoints=keypoints), "minimax"
        except (httpx.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
            self._auto_force_rule = True
            self.auto_rule_reason = f"MiniMax judge unavailable: {exc}"
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

    def _judge_with_rule(self, *, answer: str, keypoints: Sequence[str]) -> list[bool]:
        answer_normalized = normalize_text(answer)
        answer_tokens = list(dict.fromkeys(tokenize(answer)))
        hits: list[bool] = []
        for keypoint in keypoints:
            keypoint_normalized = normalize_text(keypoint)
            if keypoint_normalized and keypoint_normalized in answer_normalized:
                hits.append(True)
                continue

            keypoint_tokens = list(dict.fromkeys(tokenize(keypoint)))
            if not keypoint_tokens:
                hits.append(False)
                continue

            overlap_count = sum(1 for token in keypoint_tokens if token_matches(token, answer_tokens))
            overlap_ratio = overlap_count / len(keypoint_tokens)
            if len(keypoint_tokens) <= 2:
                hits.append(overlap_ratio == 1.0)
            else:
                hits.append(overlap_ratio >= 0.75)
        return hits

    def _judge_with_minimax(self, *, question: str, answer: str, keypoints: Sequence[str]) -> list[bool]:
        if not self.settings.minimax_api_key:
            raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY not configured")

        system_prompt = (
            "You are a strict benchmark judge for repository-grounded Q&A. "
            "Decide whether each expected keypoint is clearly supported by the answer text. "
            "Treat close paraphrases as hits, but do not infer facts that are absent. "
            "Return JSON only with this shape: {\"hits\": [true, false]}."
        )
        numbered_keypoints = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(keypoints))
        user_prompt = (
            f"Question:\n{question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Expected keypoints:\n{numbered_keypoints}\n\n"
            f"Return JSON only. The `hits` array must have exactly {len(keypoints)} booleans."
        )

        payload = {
            "model": self.judge_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "name": "QA Benchmark Judge",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "name": "benchmark",
                    "content": user_prompt,
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.settings.knowledge_synthesis_timeout_seconds) as client:
            response = client.post(
                f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()

        content = extract_minimax_content(response_payload)
        parsed = json.loads(strip_json_fence(content))
        if not isinstance(parsed, dict):
            raise RuntimeError("MiniMax judge did not return a JSON object")
        raw_hits = parsed.get("hits")
        if not isinstance(raw_hits, list) or len(raw_hits) != len(keypoints):
            raise RuntimeError("MiniMax judge returned an invalid hits array")
        hits: list[bool] = []
        for item in raw_hits:
            if not isinstance(item, bool):
                raise RuntimeError("MiniMax judge returned a non-boolean hit value")
            hits.append(item)
        return hits

    def _judge_with_anthropic(self, *, question: str, answer: str, keypoints: Sequence[str]) -> list[bool]:
        """Cross-family judge using Anthropic Claude. Used when the synthesis
        side is MiniMax — Anthropic judges MiniMax's output without the
        self-evaluation bias that 'MiniMax judges MiniMax' has.
        """
        if not self.settings.anthropic_api_key:
            raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY not configured")

        system_prompt = (
            "You are a strict benchmark judge for repository-grounded Q&A. "
            "Decide whether each expected keypoint is clearly supported by the answer text. "
            "Treat close paraphrases as hits, but do not infer facts that are absent. "
            "Return JSON only with this shape: {\"hits\": [true, false]}."
        )
        numbered_keypoints = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(keypoints))
        user_prompt = (
            f"Question:\n{question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Expected keypoints:\n{numbered_keypoints}\n\n"
            f"Return JSON only. The `hits` array must have exactly {len(keypoints)} booleans."
        )

        payload = {
            "model": self.settings.anthropic_model,
            "max_tokens": 512,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        with httpx.Client(timeout=self.settings.knowledge_synthesis_timeout_seconds) as client:
            response = client.post(
                f"{self.settings.anthropic_base_url.rstrip('/')}/v1/messages",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()

        # Anthropic messages API: response.content is a list of content blocks.
        content_blocks = response_payload.get("content") or []
        text_parts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
        content = "".join(text_parts)
        if not content.strip():
            raise RuntimeError("Anthropic judge returned empty content")

        parsed = json.loads(strip_json_fence(content))
        if not isinstance(parsed, dict):
            raise RuntimeError("Anthropic judge did not return a JSON object")
        raw_hits = parsed.get("hits")
        if not isinstance(raw_hits, list) or len(raw_hits) != len(keypoints):
            raise RuntimeError("Anthropic judge returned an invalid hits array")
        hits: list[bool] = []
        for item in raw_hits:
            if not isinstance(item, bool):
                raise RuntimeError("Anthropic judge returned a non-boolean hit value")
            hits.append(item)
        return hits


class BenchmarkClient:
    def __init__(self, backend_url: str, actor_name: str) -> None:
        self.backend_url = backend_url.rstrip("/")
        self.actor_name = actor_name
        self.client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=30.0),
            headers={
                "Content-Type": "application/json",
                "X-Actor-Role": ACTOR_ROLE,
                "X-Actor-App-Role": ACTOR_APP_ROLE,
            },
        )

    def close(self) -> None:
        self.client.close()

    def ensure_backend_reachable(self) -> None:
        try:
            response = self.client.get(f"{self.backend_url}/health")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise InfrastructureError(f"Backend not reachable at {self.backend_url}: {exc}") from exc

    def submit_question(self, row: DatasetRow) -> dict[str, Any]:
        payload = {
            "title": f"QA benchmark {row.id}",
            "request": row.question,
            "actor_name": self.actor_name,
            "actor_role": ACTOR_ROLE,
        }
        try:
            response = self.client.post(f"{self.backend_url}/api/tasks", json=payload)
            response.raise_for_status()
            task_payload = response.json()
        except httpx.HTTPError as exc:
            raise InfrastructureError(f"Failed to create task for {row.id}: {exc}") from exc
        if not isinstance(task_payload, dict):
            raise InfrastructureError(f"Task creation for {row.id} did not return an object payload")
        return task_payload

    def poll_task(self, task_id: str, timeout_seconds: float = QUESTION_TIMEOUT_SECONDS) -> tuple[dict[str, Any] | None, float, bool]:
        started = time.monotonic()
        while True:
            elapsed = time.monotonic() - started
            if elapsed > timeout_seconds:
                return None, elapsed, True
            try:
                response = self.client.get(f"{self.backend_url}/api/tasks/{task_id}")
                response.raise_for_status()
                task_payload = response.json()
            except httpx.HTTPError as exc:
                raise InfrastructureError(f"Failed to poll task {task_id}: {exc}") from exc
            if not isinstance(task_payload, dict):
                raise InfrastructureError(f"Task poll for {task_id} did not return an object payload")
            status = str(task_payload.get("status") or "").strip().lower()
            if status in TERMINAL_STATUSES:
                return task_payload, elapsed, False
            time.sleep(POLL_INTERVAL_SECONDS)


def compute_citation_precision(expected_paths: Sequence[str], found_paths: Sequence[str]) -> float:
    expected = {normalize_citation_path(item) for item in expected_paths if item.strip()}
    found = {normalize_citation_path(item) for item in found_paths if item.strip()}
    if not found:
        return 0.0
    return len(expected & found) / max(len(found), 1)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def tier_summary(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for tier in ("A", "B", "C", "D"):
        tier_records = [record for record in records if record["tier"] == tier]
        scores = [float(record["score"]) for record in tier_records]
        summary[tier] = {
            "count": len(tier_records),
            "completed": sum(1 for record in tier_records if record["completed"]),
            "timed_out": sum(1 for record in tier_records if record["timed_out"]),
            "mean_score": round(mean(scores), 2) if scores else 0.0,
            "min_score": round(min(scores), 2) if scores else 0.0,
            "max_score": round(max(scores), 2) if scores else 0.0,
        }
    return summary


def try_git_head() -> str | None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={REPO_ROOT.as_posix()}",
                "rev-parse",
                "HEAD",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    head = result.stdout.strip()
    return head or None


def build_summary(
    *,
    dataset_path: Path,
    out_path: Path,
    backend_url: str,
    actor_name: str,
    requested_judge_mode: str,
    judge: KeypointJudge,
    started_at: datetime,
    finished_at: datetime | None,
    records: Sequence[dict[str, Any]],
    infrastructure_failed: bool,
    running: bool,
) -> dict[str, Any]:
    abc_records = [record for record in records if record["tier"] in {"A", "B", "C"}]
    abc_completed = sum(1 for record in abc_records if record["completed"])
    abc_total = len(abc_records)
    completion_ratio = abc_completed / abc_total if abc_total else 0.0
    judge_modes_used = sorted({str(record["judge_mode"]) for record in records if record.get("judge_mode")})
    summary = {
        "type": "summary",
        "status": "running" if running else "completed",
        "benchmark": "qa_accuracy_benchmark",
        "started_at_utc": format_utc(started_at),
        "finished_at_utc": format_utc(finished_at) if finished_at else None,
        "dataset_path": str(dataset_path),
        "artifact_path": str(out_path),
        "backend_url": backend_url,
        "actor_name": actor_name,
        "question_timeout_s": QUESTION_TIMEOUT_SECONDS,
        "requested_judge_mode": requested_judge_mode,
        "judge_model": judge.judge_model,
        "judge_modes_used": judge_modes_used,
        "judge_auto_fallback_reason": judge.auto_rule_reason,
        "backend_commit_sha": try_git_head(),
        "total_questions": len(records),
        "completed_questions": sum(1 for record in records if record["completed"]),
        "timed_out_questions": sum(1 for record in records if record["timed_out"]),
        "infrastructure_failed": infrastructure_failed,
        "abc_completed": abc_completed,
        "abc_total": abc_total,
        "abc_completion_ratio": round(completion_ratio, 4),
        "tier_summary": tier_summary(records),
    }
    return summary


def write_artifact(out_path: Path, summary: dict[str, Any], records: Sequence[dict[str, Any]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Q&A accuracy benchmark against the local backend.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH), help="Path to the benchmark dataset JSONL file.")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8002", help="Backend base URL, without the /api suffix.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Directory where JSONL run artifacts are written.")
    parser.add_argument("--actor-name", default="qa-benchmark", help="Actor name sent to the backend task API.")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N dataset rows.")
    parser.add_argument(
        "--judge-mode",
        choices=("auto", "anthropic", "minimax", "rule"),
        default="auto",
        help=(
            "Scoring judge mode. 'auto' prefers Anthropic (cross-family, avoids "
            "self-evaluation bias vs MiniMax synthesizer), falls back to MiniMax, "
            "then rule judge."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_path = resolve_cli_path(args.dataset)
    out_dir = resolve_cli_path(args.out_dir)
    rows = load_dataset(dataset_path, args.limit)

    if not rows:
        raise SystemExit("Dataset is empty.")

    started_at = utc_now()
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"qa-run-{timestamp}.jsonl"

    judge = KeypointJudge(args.judge_mode)
    client = BenchmarkClient(backend_url=args.backend_url, actor_name=args.actor_name)
    records: list[dict[str, Any]] = []
    infrastructure_failed = False

    try:
        client.ensure_backend_reachable()
    except InfrastructureError as exc:
        summary = build_summary(
            dataset_path=dataset_path,
            out_path=out_path,
            backend_url=args.backend_url,
            actor_name=args.actor_name,
            requested_judge_mode=args.judge_mode,
            judge=judge,
            started_at=started_at,
            finished_at=utc_now(),
            records=records,
            infrastructure_failed=True,
            running=False,
        )
        write_artifact(out_path, summary, records)
        print(str(exc), file=sys.stderr)
        return 2

    try:
        for row in rows:
            question_started = time.monotonic()
            task_id: str | None = None
            timed_out = False
            completed = False
            task_status = "unknown"
            answer = ""
            citations_found_display: list[str] = []
            citations_found_canonical: list[str] = []
            error_text: str | None = None

            try:
                created_task = client.submit_question(row)
                task_id = str(created_task.get("id") or "").strip() or None
                if not task_id:
                    raise InfrastructureError(f"Task creation for {row.id} returned no task id")
                final_task, duration_s, timed_out = client.poll_task(task_id)
                if timed_out or final_task is None:
                    task_status = "timed_out"
                    keypoint_hits = [False] * len(row.expected_answer_keypoints)
                    judge_mode_used = "rule"
                    citation_precision = 0.0
                    keypoint_coverage = 0.0
                    score = 0.0
                    answer_excerpt = ""
                else:
                    task_status = str(final_task.get("status") or "").strip().lower()
                    completed = task_status in TERMINAL_STATUSES
                    answer, citations_found_display, citations_found_canonical = extract_answer_and_citations(final_task)
                    keypoint_hits, judge_mode_used = judge.judge(
                        question=row.question,
                        answer=answer,
                        keypoints=row.expected_answer_keypoints,
                    )
                    keypoint_coverage = sum(1 for hit in keypoint_hits if hit) / max(len(keypoint_hits), 1)
                    citation_precision = compute_citation_precision(row.expected_citations, citations_found_canonical)
                    score = (keypoint_coverage * 60.0) + (citation_precision * 40.0)
                    answer_excerpt = truncate_utf8(answer, ANSWER_EXCERPT_MAX_BYTES)
            except InfrastructureError as exc:
                infrastructure_failed = True
                duration_s = time.monotonic() - question_started
                task_status = "infrastructure_error"
                error_text = str(exc)
                keypoint_hits = [False] * len(row.expected_answer_keypoints)
                judge_mode_used = "rule"
                citation_precision = 0.0
                keypoint_coverage = 0.0
                score = 0.0
                answer_excerpt = ""
            except Exception as exc:  # pragma: no cover - defensive failure capture
                infrastructure_failed = True
                duration_s = time.monotonic() - question_started
                task_status = "runner_error"
                error_text = f"{type(exc).__name__}: {exc}"
                keypoint_hits = [False] * len(row.expected_answer_keypoints)
                judge_mode_used = "rule"
                citation_precision = 0.0
                keypoint_coverage = 0.0
                score = 0.0
                answer_excerpt = ""

            if task_status in TERMINAL_STATUSES:
                completed = True

            record = {
                "type": "question",
                "question_id": row.id,
                "tier": row.tier,
                "task_id": task_id,
                "task_status": task_status,
                "completed": completed,
                "timed_out": timed_out,
                "score": round(score, 2),
                "keypoint_coverage": round(keypoint_coverage, 4),
                "citation_precision": round(citation_precision, 4),
                "keypoint_hits": [
                    {"keypoint": keypoint, "hit": hit}
                    for keypoint, hit in zip(row.expected_answer_keypoints, keypoint_hits)
                ],
                "expected_citations": row.expected_citations,
                "citations_found": citations_found_display,
                "judge_mode": judge_mode_used,
                "duration_s": round(duration_s, 3),
                "answer_excerpt": answer_excerpt,
                "error": error_text,
            }
            records.append(record)

            running_summary = build_summary(
                dataset_path=dataset_path,
                out_path=out_path,
                backend_url=args.backend_url,
                actor_name=args.actor_name,
                requested_judge_mode=args.judge_mode,
                judge=judge,
                started_at=started_at,
                finished_at=None,
                records=records,
                infrastructure_failed=infrastructure_failed,
                running=True,
            )
            write_artifact(out_path, running_summary, records)
            print(
                f"{row.id} status={task_status} score={record['score']:.2f} "
                f"judge={judge_mode_used} duration={record['duration_s']:.3f}s"
            )
    finally:
        client.close()

    finished_at = utc_now()
    summary = build_summary(
        dataset_path=dataset_path,
        out_path=out_path,
        backend_url=args.backend_url,
        actor_name=args.actor_name,
        requested_judge_mode=args.judge_mode,
        judge=judge,
        started_at=started_at,
        finished_at=finished_at,
        records=records,
        infrastructure_failed=infrastructure_failed,
        running=False,
    )
    write_artifact(out_path, summary, records)

    abc_records = [record for record in records if record["tier"] in {"A", "B", "C"}]
    abc_completed = sum(1 for record in abc_records if record["completed"])
    abc_ratio = abc_completed / max(len(abc_records), 1)
    print(
        f"artifact={out_path} completed={summary['completed_questions']}/{summary['total_questions']} "
        f"abc_completion_ratio={abc_ratio:.4f} infrastructure_failed={infrastructure_failed}"
    )

    if infrastructure_failed or abc_ratio < 0.9:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
