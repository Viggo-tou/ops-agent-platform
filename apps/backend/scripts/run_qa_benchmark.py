from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
QUESTION_TIMEOUT_SECONDS = 240.0
POLL_INTERVAL_SECONDS = 1.0
ANSWER_EXCERPT_MAX_BYTES = 2048
ACTOR_ROLE = "employee"
ACTOR_APP_ROLE = "member"
INFRA_INVALID_SYNTH_STATUSES = {"task_error", "timeout"}
INFRA_BURST_THRESHOLD = 3
INFRA_BURST_BACKOFF_S = [30.0, 60.0]
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


class DatasetValidationError(RuntimeError):
    pass


class SourceFilterError(InfrastructureError):
    pass


@dataclass(frozen=True)
class DatasetRow:
    id: str
    tier: str
    question: str
    expected_answer_keypoints: list[str]
    expected_citations: list[str]
    source_name: str | None = None


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
    payloads: list[dict[str, Any]] = []
    rows: list[DatasetRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise SystemExit(f"Dataset row in {path} is not a JSON object")
            payloads.append(payload)

    distinct_sources = {
        str(payload.get("source_name")).strip()
        for payload in payloads
        if isinstance(payload.get("source_name"), str) and str(payload.get("source_name")).strip()
    }
    missing_source_ids = [
        str(payload.get("id", "<unknown>"))
        for payload in payloads
        if not isinstance(payload.get("source_name"), str) or not str(payload.get("source_name")).strip()
    ]
    if len(distinct_sources) > 1 and missing_source_ids:
        raise DatasetValidationError(
            "Multi-source benchmark dataset requires source_name on every row; "
            f"missing for question id(s): {', '.join(missing_source_ids)}"
        )

    for payload in payloads:
        source_name = payload.get("source_name")
        source_name = source_name.strip() if isinstance(source_name, str) and source_name.strip() else None
        rows.append(
            DatasetRow(
                id=str(payload["id"]),
                tier=str(payload["tier"]),
                question=str(payload["question"]),
                expected_answer_keypoints=[str(item) for item in payload["expected_answer_keypoints"]],
                expected_citations=[str(item) for item in payload["expected_citations"]],
                source_name=source_name,
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


def extract_answer_and_citations(
    task_payload: dict[str, Any],
) -> tuple[str, list[str], list[str], list[dict[str, Any]], dict[str, Any]]:
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
    structured_citations: list[dict[str, Any]] = []
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
                structured_citations.append(dict(item))
    trace = result.get("answer_trace")
    trace = trace if isinstance(trace, dict) else {}
    return answer.strip(), display_citations, canonical_citations, structured_citations, trace


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
    def __init__(self, requested_mode: str, samples: int = 1) -> None:
        self.requested_mode = requested_mode
        self.settings = get_settings()
        self.judge_model = self.settings.knowledge_synthesis_model or "MiniMax-M2.7"
        self.auto_rule_reason: str | None = None
        self._auto_force_rule = False
        # Multi-sample judging: ask the same judge to evaluate the same
        # answer N times and take per-keypoint majority. Dampens the
        # ±3-5 pt single-run noise we observed when comparing close
        # benchmark variants. samples=1 preserves the original behaviour.
        self.samples = max(1, int(samples))

    @property
    def pinned(self) -> bool:
        return self.requested_mode != "auto"

    def _append_auto_reason(self, reason: str) -> None:
        if self.auto_rule_reason and reason in self.auto_rule_reason:
            return
        self.auto_rule_reason = f"{self.auto_rule_reason} {reason}".strip() if self.auto_rule_reason else reason

    def judge(self, *, question: str, answer: str, keypoints: Sequence[str]) -> tuple[list[bool], str]:
        if not answer.strip():
            return [False] * len(keypoints), "rule"

        if self.samples > 1:
            return self._judge_multi_sample(
                question=question, answer=answer, keypoints=keypoints
            )
        return self._judge_one(question=question, answer=answer, keypoints=keypoints)

    def _judge_multi_sample(
        self, *, question: str, answer: str, keypoints: Sequence[str]
    ) -> tuple[list[bool], str]:
        """Run ``self.samples`` independent judge calls and take per-keypoint
        majority vote. If samples disagree on the judge mode actually used
        (e.g. one fell back to rule), the first non-rule mode wins for the
        reported judge_mode field; if all fell back to rule, "rule" wins.
        """
        per_sample_hits: list[list[bool]] = []
        modes_used: list[str] = []
        for _ in range(self.samples):
            hits, mode = self._judge_one(
                question=question, answer=answer, keypoints=keypoints
            )
            per_sample_hits.append(hits)
            modes_used.append(mode)
        # Per-keypoint majority. Ties (1-1 with 2 samples, etc.) resolved
        # in favour of False (more conservative; do not credit unstable hits).
        n = len(keypoints)
        majority: list[bool] = []
        for i in range(n):
            true_count = sum(
                1 for sample in per_sample_hits if i < len(sample) and sample[i]
            )
            majority.append(true_count > self.samples // 2)
        # Pick the most-informative mode label.
        non_rule = next((m for m in modes_used if m != "rule"), None)
        reported_mode = non_rule or modes_used[0]
        return majority, reported_mode

    def _judge_one(self, *, question: str, answer: str, keypoints: Sequence[str]) -> tuple[list[bool], str]:
        """Single judge invocation following the legacy mode-selection chain."""

        if self.requested_mode == "rule":
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        if self.requested_mode == "claude_code":
            return self._judge_with_claude_code(question=question, answer=answer, keypoints=keypoints), "claude_code"

        if self.requested_mode == "codex":
            return self._judge_with_codex(question=question, answer=answer, keypoints=keypoints), "codex"

        if self.requested_mode == "anthropic":
            return self._judge_with_anthropic(question=question, answer=answer, keypoints=keypoints), "anthropic"

        if self.requested_mode == "minimax":
            return self._judge_with_minimax(question=question, answer=answer, keypoints=keypoints), "minimax"

        # auto: prefer CLI judges (cross-family vs MiniMax synthesizer
        # AND no API credit billing) → fall back to Anthropic API →
        # MiniMax → rule.
        if self._auto_force_rule:
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        # Claude Code CLI: cross-family judge using local OAuth — best
        # bias profile, no billing dependency.
        if shutil.which(self.settings.claude_code_command):
            try:
                return self._judge_with_claude_code(question=question, answer=answer, keypoints=keypoints), "claude_code"
            except Exception as exc:  # noqa: BLE001
                self._append_auto_reason(f"Claude Code CLI judge failed: {exc}; trying next.")
        else:
            self._append_auto_reason(f"Claude Code CLI judge not found: {self.settings.claude_code_command}; trying next.")

        # Codex CLI: also cross-family vs MiniMax, uses ChatGPT auth.
        if shutil.which(self.settings.codex_command):
            try:
                return self._judge_with_codex(question=question, answer=answer, keypoints=keypoints), "codex"
            except Exception as exc:  # noqa: BLE001
                self._append_auto_reason(f"Codex CLI judge failed: {exc}; trying next.")
        else:
            self._append_auto_reason(f"Codex CLI judge not found: {self.settings.codex_command}; trying next.")

        if self.settings.anthropic_api_key:
            try:
                return self._judge_with_anthropic(question=question, answer=answer, keypoints=keypoints), "anthropic"
            except (httpx.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
                # Don't force-rule yet — fall through to MiniMax before giving up.
                self._append_auto_reason(f"Anthropic judge failed: {exc}; trying next.")
        else:
            self._append_auto_reason("OPS_AGENT_ANTHROPIC_API_KEY not configured; trying next.")

        if not self.settings.minimax_api_key:
            self._auto_force_rule = True
            self._append_auto_reason("OPS_AGENT_MINIMAX_API_KEY not configured; falling back to rule.")
            return self._judge_with_rule(answer=answer, keypoints=keypoints), "rule"

        try:
            return self._judge_with_minimax(question=question, answer=answer, keypoints=keypoints), "minimax"
        except (httpx.HTTPError, json.JSONDecodeError, RuntimeError) as exc:
            self._auto_force_rule = True
            self._append_auto_reason(f"MiniMax judge unavailable: {exc}; falling back to rule.")
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

    @staticmethod
    def _build_judge_prompt(*, question: str, answer: str, keypoints: Sequence[str]) -> str:
        numbered_keypoints = "\n".join(f"{i + 1}. {k}" for i, k in enumerate(keypoints))
        return (
            "You are a strict benchmark judge for repository-grounded Q&A. "
            "Decide whether each expected keypoint is clearly supported by "
            "the answer text. Treat close paraphrases as hits, but do not "
            "infer facts that are absent.\n\n"
            f"Question:\n{question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Expected keypoints:\n{numbered_keypoints}\n\n"
            f"Return JSON ONLY in this exact shape: "
            f'{{"hits": [{", ".join(["true"] * len(keypoints))}]}} '
            f"with exactly {len(keypoints)} boolean values, one per keypoint, "
            f"in the same order. No prose before or after the JSON."
        )

    def _parse_hits_from_text(self, text: str, *, keypoints_count: int) -> list[bool]:
        """Extract a {"hits": [bool, ...]} object from arbitrary CLI text output."""
        cleaned = (text or "").strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, flags=re.MULTILINE)
        # Find first JSON object containing a "hits" key.
        for match in re.finditer(r"\{[^{}]*?\"hits\"\s*:\s*\[[^\]]*\][^{}]*\}", cleaned, flags=re.DOTALL):
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            raw = parsed.get("hits") if isinstance(parsed, dict) else None
            if isinstance(raw, list) and len(raw) == keypoints_count and all(isinstance(x, bool) for x in raw):
                return list(raw)
        # Fallback: any top-level JSON
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if m is not None:
            try:
                parsed = json.loads(m.group(0))
                raw = parsed.get("hits") if isinstance(parsed, dict) else None
                if isinstance(raw, list) and len(raw) == keypoints_count and all(isinstance(x, bool) for x in raw):
                    return list(raw)
            except json.JSONDecodeError:
                pass
        raise RuntimeError(f"could not parse hits array from CLI output: {cleaned[:300]!r}")

    def _judge_with_claude_code(self, *, question: str, answer: str, keypoints: Sequence[str]) -> list[bool]:
        """Use the Claude Code CLI as a cross-family judge.

        Avoids API billing (CLI uses local OAuth) and avoids the
        self-evaluation bias of MiniMax-judges-MiniMax. Slower than an
        API call (~30-60s per question) but no per-call cost.
        """
        claude_cmd = shutil.which(self.settings.claude_code_command)
        if not claude_cmd:
            raise RuntimeError(f"Claude Code CLI not found: {self.settings.claude_code_command}")

        prompt = self._build_judge_prompt(question=question, answer=answer, keypoints=keypoints)

        env = {**os.environ}
        # The CLI uses its own OAuth; remove any API key that might switch
        # the CLI into API-billing mode.
        env.pop("ANTHROPIC_API_KEY", None)
        if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
            for candidate in [
                "D:\\Git\\bin\\bash.exe",
                "C:\\Program Files\\Git\\bin\\bash.exe",
                "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
            ]:
                if os.path.isfile(candidate):
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                    break

        claude_args = self.settings.claude_code_args.split()
        if "-p" not in claude_args and "--print" not in claude_args:
            claude_args.append("--print")
        if "--dangerously-skip-permissions" not in claude_args:
            claude_args.append("--dangerously-skip-permissions")
        if "--output-format" not in " ".join(claude_args):
            claude_args.extend(["--output-format", "json"])

        cmd = [claude_cmd, *claude_args, "-"]

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as prompt_file:
            prompt_file.write(prompt)
            prompt_file_path = prompt_file.name

        try:
            with open(prompt_file_path, "r", encoding="utf-8") as stdin_f:
                proc = subprocess.run(
                    cmd,
                    stdin=stdin_f,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=int(self.settings.claude_code_timeout_seconds),
                )
        finally:
            try:
                os.unlink(prompt_file_path)
            except OSError:
                pass

        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code CLI returned rc={proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
            )

        # CLI in --output-format json mode wraps the assistant response in
        # a JSON envelope: {"type":"result","subtype":"success","result":"<text>"}.
        try:
            envelope = json.loads(proc.stdout)
            inner = envelope.get("result") if isinstance(envelope, dict) else None
        except json.JSONDecodeError:
            inner = proc.stdout
        if not isinstance(inner, str):
            inner = proc.stdout

        return self._parse_hits_from_text(inner, keypoints_count=len(keypoints))

    def _judge_with_codex(self, *, question: str, answer: str, keypoints: Sequence[str]) -> list[bool]:
        """Use the Codex CLI (`codex exec`) as judge. Auth via ChatGPT
        subscription, no per-call API billing.
        """
        codex_cmd = shutil.which(self.settings.codex_command)
        if not codex_cmd:
            raise RuntimeError(f"Codex CLI not found: {self.settings.codex_command}")

        prompt = self._build_judge_prompt(question=question, answer=answer, keypoints=keypoints)

        env = {**os.environ}
        if self.settings.openai_api_key:
            env["OPENAI_API_KEY"] = self.settings.openai_api_key

        # codex exec reads prompt from stdin via "-"; --full-auto avoids
        # interactive confirmations.
        cmd = [codex_cmd, "exec", "--full-auto", "-"]

        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            env=env,
            timeout=int(self.settings.codex_timeout_seconds),
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"Codex CLI returned rc={proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
            )

        return self._parse_hits_from_text(proc.stdout, keypoints_count=len(keypoints))


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
        if row.source_name:
            payload["source_name"] = row.source_name
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


def _matches_citation(returned: str, expected: str) -> bool:
    """Match expected citation paths, including legacy source-name aliases."""
    aliases = {
        "handyman-admin-dashboard": "hosteddashboard",
        "hosteddashboard": "hosteddashboard",
    }
    returned_normalized = returned.strip().replace("\\", "/").lower()
    expected_normalized = expected.strip().replace("\\", "/").lower()
    if "/" not in expected_normalized:
        return normalize_citation_path(returned_normalized) == normalize_citation_path(expected_normalized)
    expected_source, expected_rel = expected_normalized.split("/", 1)
    expected_source = aliases.get(expected_source, expected_source)
    known_sources = {"hosteddashboard", "handymanapp", *aliases.values()}
    if expected_source not in known_sources:
        return normalize_citation_path(returned_normalized) == normalize_citation_path(expected_normalized)
    if "/" not in returned_normalized:
        return normalize_citation_path(returned_normalized) == normalize_citation_path(expected_rel)
    returned_source, returned_rel = returned_normalized.split("/", 1)
    return returned_source == expected_source and normalize_citation_path(returned_rel) == normalize_citation_path(expected_rel)


def _citation_identity(citation: dict[str, Any]) -> str:
    source_name = str(citation.get("source_name") or "").strip()
    relative_path = str(citation.get("relative_path") or "").strip()
    return f"{source_name}/{relative_path}" if source_name else relative_path


def _any_kp_substring(keypoints: Sequence[str], card_text: str) -> bool:
    normalized_card = card_text.lower()
    for keypoint in keypoints:
        normalized_keypoint = normalize_text(str(keypoint))
        if len(normalized_keypoint) >= 4 and normalized_keypoint in normalized_card:
            return True
        for token in re.findall(r"[@a-z0-9_./-]{4,}", str(keypoint).lower()):
            if token in normalized_card:
                return True
    return False


def compute_retrieval_diagnostics(
    row: DatasetRow,
    trace: dict[str, Any],
    citations: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    expected_first_rank: int | None = None
    for index, citation in enumerate(citations, start=1):
        returned = _citation_identity(citation)
        if any(_matches_citation(returned, expected) for expected in row.expected_citations):
            expected_first_rank = index
            break

    source_distribution = Counter(
        str(citation.get("source_name") or "").strip() or "<missing>"
        for citation in citations
    )
    requested = row.source_name
    wrong_source = (
        sum(count for source, count in source_distribution.items() if source != requested)
        if requested
        else 0
    )

    expected_fact_in_card: bool | None = None
    for citation in citations:
        returned = _citation_identity(citation)
        if any(_matches_citation(returned, expected) for expected in row.expected_citations):
            card_text = str(citation.get("card_text") or "").strip().lower()
            if card_text:
                expected_fact_in_card = _any_kp_substring(row.expected_answer_keypoints, card_text)
            break

    return {
        "expected_citation_top_rank": expected_first_rank,
        "top_k_source_distribution": dict(source_distribution),
        "wrong_source_in_top_k": wrong_source,
        "expected_fact_in_card": expected_fact_in_card,
        "selected_sources": trace.get("selected_sources") if isinstance(trace.get("selected_sources"), list) else [],
    }


def assign_stage19_buckets(record: dict[str, Any], diagnostics: dict[str, Any]) -> list[str]:
    buckets: list[str] = []
    expected_rank = diagnostics.get("expected_citation_top_rank")
    wrong_source_count = int(diagnostics.get("wrong_source_in_top_k") or 0)
    source_distribution = diagnostics.get("top_k_source_distribution") or {}
    requested_source = record.get("source_name")
    requested_source_hits = (
        int(source_distribution.get(requested_source, 0)) if requested_source else 0
    )
    if wrong_source_count > 0:
        buckets.append("retrieval_wrong_source")
    elif expected_rank is None and requested_source_hits == 0:
        buckets.append("retrieval_empty_no_source")
    elif expected_rank is None:
        buckets.append("retrieval_right_source_wrong_file")
    if expected_rank is not None and int(expected_rank) > 4:
        buckets.append("retrieval_right_source_wrong_file")
    if diagnostics.get("expected_fact_in_card") is False:
        buckets.append("card_missing_keypoint_facts")

    keypoint_coverage = float(record.get("keypoint_coverage") or 0.0)
    if (
        record.get("tier") in {"C", "D"}
        and keypoint_coverage < 0.3
        and expected_rank is not None
        and not any(bucket.startswith("retrieval_") for bucket in buckets)
    ):
        buckets.append("cross_file_reasoning_miss")

    android_terms = {
        "Composable",
        "Fragment",
        "ViewModel",
        "Firebase",
        "Navigation",
        "Activity",
        "RecyclerView",
        "Adapter",
        "navController",
        "LazyColumn",
        "@composable",
        "compose",
        "androidx",
    }
    answer = str(record.get("answer_excerpt") or "").lower()
    keypoint_text = " ".join(str(item) for item in record.get("expected_answer_keypoints") or []).lower()
    has_android = any(term.lower() in keypoint_text for term in android_terms)
    if has_android and not any(term.lower() in answer for term in android_terms):
        buckets.append("domain_jargon_miss")
    return buckets


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def tier_summary(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for tier in ("A", "B", "C", "D"):
        tier_records = [record for record in records if record["tier"] == tier]
        valid_records = [
            record for record in tier_records if str(record.get("score_status", "valid")) == "valid"
        ]
        scores = [float(record["score"]) for record in valid_records]
        summary[tier] = {
            "count": len(tier_records),
            "valid_score_count": len(valid_records),
            "invalid_score_count": len(tier_records) - len(valid_records),
            "completed": sum(1 for record in tier_records if record["completed"]),
            "timed_out": sum(1 for record in tier_records if record["timed_out"]),
            "mean_score": round(mean(scores), 2) if scores else 0.0,
            "min_score": round(min(scores), 2) if scores else 0.0,
            "max_score": round(max(scores), 2) if scores else 0.0,
        }
    return summary


def source_summary(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    sources = sorted(
        {
            str(record.get("source_name") or "").strip()
            for record in records
            if str(record.get("source_name") or "").strip()
        }
    )
    for source_name in sources:
        source_records = [record for record in records if record.get("source_name") == source_name]
        valid_records = [
            record for record in source_records if str(record.get("score_status", "valid")) == "valid"
        ]
        scores = [float(record["score"]) for record in valid_records]
        summary[source_name] = {
            "count": len(source_records),
            "valid_score_count": len(valid_records),
            "invalid_score_count": len(source_records) - len(valid_records),
            "mean_score": round(mean(scores), 2) if scores else 0.0,
        }
    return summary


def parse_infra_burst_backoff(value: str) -> list[float]:
    backoffs: list[float] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        backoff = float(part)
        if backoff < 0:
            raise argparse.ArgumentTypeError("--infra-burst-backoff values must be non-negative")
        backoffs.append(backoff)
    return backoffs


def classify_failure_bucket(record: dict[str, Any]) -> str | None:
    """Return a failure bucket name for invalid records, or None for valid ones."""
    if record.get("score_status") == "valid":
        return None
    synthesis_status = record.get("synthesis_status")
    judge_status = record.get("judge_status")
    duration_s = float(record.get("duration_s") or 0)
    error = " ".join(
        str(record.get(field) or "").lower()
        for field in ("error", "judge_error", "answer_excerpt")
    )

    if synthesis_status == "timeout":
        return "infra_timeout"
    if synthesis_status == "task_error":
        if 25 <= duration_s <= 35:
            return "cc_failure"
        if "cc_agent" in error or "claude_code" in error or "cc decision" in error:
            return "cc_failure"
        return "infra_task_error"
    if synthesis_status == "empty":
        return "synthesis_empty"
    if synthesis_status == "pass" and judge_status == "fail":
        return "judge_failure"
    if "cc_agent" in error or "claude_code" in error or "cc decision" in error:
        return "cc_failure"
    return "other"


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
    question_timeout_seconds: float,
    preflight_judge_status: str,
    preflight_judge_error: str | None,
    infra_burst_count: int = 0,
    abort_reason: str | None = None,
) -> dict[str, Any]:
    abc_records = [record for record in records if record["tier"] in {"A", "B", "C"}]
    abc_completed = sum(1 for record in abc_records if record["completed"])
    abc_total = len(abc_records)
    completion_ratio = abc_completed / abc_total if abc_total else 0.0
    judge_modes_used: list[str] = []
    for record in records:
        mode = str(record.get("judge_mode") or "").strip()
        if mode and mode != "skipped" and mode not in judge_modes_used:
            judge_modes_used.append(mode)
    synthesis_counts = Counter(str(record.get("synthesis_status", "unknown")) for record in records)
    judge_counts = Counter(str(record.get("judge_status", "unknown")) for record in records)
    score_counts = Counter(str(record.get("score_status", "unknown")) for record in records)
    failure_buckets = Counter()
    stage19_buckets = Counter()
    for record in records:
        bucket = classify_failure_bucket(record)
        if bucket:
            failure_buckets[bucket] += 1
        stage19_buckets.update(record.get("stage19_buckets") or [])
    retrieval_rank_distribution = Counter(
        "absent"
        if record.get("expected_citation_top_rank") is None
        else f"top{min(int(record['expected_citation_top_rank']), 8)}"
        for record in records
    )
    fact_records = [
        record for record in records if record.get("expected_fact_in_card") is not None
    ]
    pinned_judge_failure_count = (
        sum(1 for record in records if record.get("judge_status") == "fail")
        if requested_judge_mode != "auto"
        else 0
    )
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
        "question_timeout_s": question_timeout_seconds,
        "requested_judge_mode": requested_judge_mode,
        "judge_model": judge.judge_model,
        "judge_modes_used": judge_modes_used,
        "judge_auto_fallback_reason": judge.auto_rule_reason,
        "preflight_judge_status": preflight_judge_status,
        "preflight_judge_error": preflight_judge_error,
        "pinned_judge_failure_count": pinned_judge_failure_count,
        "pinned_judge_run_intact": (
            abort_reason is None
            and (requested_judge_mode == "auto" or pinned_judge_failure_count == 0)
        ),
        "score_averaging_note": (
            "Records with score_status='invalid' are infrastructure/judge/synthesis failures "
            "and must not be averaged as model-quality scores."
        ),
        "backend_commit_sha": try_git_head(),
        "total_questions": len(records),
        "completed_questions": sum(1 for record in records if record["completed"]),
        "timed_out_questions": sum(1 for record in records if record["timed_out"]),
        "infrastructure_failed": infrastructure_failed,
        "infra_burst_count": infra_burst_count,
        "abort_reason": abort_reason,
        "synthesis_status_counts": dict(sorted(synthesis_counts.items())),
        "judge_status_counts": dict(sorted(judge_counts.items())),
        "score_status_counts": dict(sorted(score_counts.items())),
        "failure_bucket_counts": dict(sorted(failure_buckets.items())),
        "retrieval_top_rank_distribution": dict(sorted(retrieval_rank_distribution.items())),
        "wrong_source_record_count": sum(
            1 for record in records if int(record.get("wrong_source_in_top_k") or 0) > 0
        ),
        "expected_fact_in_card_rate": (
            sum(1 for record in fact_records if record.get("expected_fact_in_card") is True)
            / max(len(fact_records), 1)
        ),
        "stage19_bucket_counts": dict(sorted(stage19_buckets.items())),
        "abc_completed": abc_completed,
        "abc_total": abc_total,
        "abc_completion_ratio": round(completion_ratio, 4),
        "tier_summary": tier_summary(records),
        "source_summary": source_summary(records),
    }
    return summary


def write_artifact(out_path: Path, summary: dict[str, Any], records: Sequence[dict[str, Any]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def preflight_judge(judge: KeypointJudge) -> tuple[str, str | None]:
    try:
        _hits, mode = judge.judge(question="ping", answer="ping", keypoints=["ping"])
        if judge.requested_mode == "auto" and mode == "rule" and not judge.auto_rule_reason:
            judge.auto_rule_reason = "Auto judge preflight fell back to rule judge"
        return "pass", None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        if judge.requested_mode == "auto":
            judge.auto_rule_reason = (
                (judge.auto_rule_reason + " ") if judge.auto_rule_reason else ""
            ) + f"Auto judge preflight failed: {error}; continuing with per-question fallback."
            print(f"WARNING: Preflight judge call failed: {error}; continuing in auto mode.", file=sys.stderr)
            return "fail", error
        raise RuntimeError(error) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Q&A accuracy benchmark against the local backend.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET_PATH), help="Path to the benchmark dataset JSONL file.")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8002", help="Backend base URL, without the /api suffix.")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Directory where JSONL run artifacts are written.")
    parser.add_argument("--actor-name", default="qa-benchmark", help="Actor name sent to the backend task API.")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N dataset rows.")
    parser.add_argument(
        "--question-timeout",
        type=float,
        default=QUESTION_TIMEOUT_SECONDS,
        help=(
            "Per-question backend polling deadline in seconds. Each question that "
            "stays running past this is marked timed_out. Default 240s; raise if "
            "synthesis is slow under heavy load."
        ),
    )
    parser.add_argument(
        "--judge-samples",
        type=int,
        default=1,
        help=(
            "Number of independent judge calls per question; per-keypoint "
            "majority vote dampens single-run noise. 3 is a good baseline. "
            "Cost: roughly N× the judge time (judge is the cheap step "
            "compared to the pipeline run, so 3× judge ≈ 1.4× total)."
        ),
    )
    parser.add_argument(
        "--judge-mode",
        choices=("auto", "claude_code", "codex", "anthropic", "minimax", "rule"),
        default="auto",
        help=(
            "Scoring judge mode. 'auto' prefers CLI judges (claude_code, then "
            "codex) — they're cross-family vs the MiniMax synthesizer AND don't "
            "consume API credits — then falls back to Anthropic API, MiniMax, "
            "rule. Explicit modes pin the choice."
        ),
    )
    parser.add_argument(
        "--infra-burst-threshold",
        type=int,
        default=INFRA_BURST_THRESHOLD,
        help="Pause when N consecutive infra-invalid records appear.",
    )
    parser.add_argument(
        "--infra-burst-backoff",
        type=str,
        default=",".join(str(int(item)) for item in INFRA_BURST_BACKOFF_S),
        help="Comma-separated backoff seconds; bursts beyond list count abort the bench.",
    )
    parser.add_argument(
        "--no-pause-on-burst",
        action="store_true",
        help="Disable burst pause; emit warnings only.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_path = resolve_cli_path(args.dataset)
    out_dir = resolve_cli_path(args.out_dir)
    try:
        rows = load_dataset(dataset_path, args.limit)
    except DatasetValidationError as exc:
        print(f"Dataset validation failed: {exc}", file=sys.stderr)
        return 2

    if not rows:
        raise SystemExit("Dataset is empty.")

    started_at = utc_now()
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"qa-run-{timestamp}.jsonl"

    judge = KeypointJudge(args.judge_mode, samples=args.judge_samples)
    client = BenchmarkClient(backend_url=args.backend_url, actor_name=args.actor_name)
    records: list[dict[str, Any]] = []
    infrastructure_failed = False
    consecutive_infra_invalid = 0
    empty_source_streak = 0
    empty_source_count = 0
    smoke_attempt_count = 0
    SMOKE_EMPTY_STREAK_LIMIT = 3
    SMOKE_EARLY_WINDOW = 5
    SMOKE_EARLY_EMPTY_FRACTION = 0.5
    infra_burst_count = 0
    abort_reason: str | None = None
    preflight_judge_status = "not_run"
    preflight_judge_error: str | None = None
    infra_burst_backoff = parse_infra_burst_backoff(args.infra_burst_backoff)
    infra_burst_threshold = max(1, int(args.infra_burst_threshold))

    try:
        preflight_judge_status, preflight_judge_error = preflight_judge(judge)
    except RuntimeError as exc:
        preflight_judge_status = "fail"
        preflight_judge_error = str(exc)
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
            question_timeout_seconds=args.question_timeout,
            preflight_judge_status=preflight_judge_status,
            preflight_judge_error=preflight_judge_error,
            infra_burst_count=infra_burst_count,
            abort_reason=abort_reason,
        )
        write_artifact(out_path, summary, records)
        print(
            f"Preflight judge call failed: {preflight_judge_error}; "
            "aborting before consuming synthesis budget",
            file=sys.stderr,
        )
        client.close()
        return 2

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
            question_timeout_seconds=args.question_timeout,
            preflight_judge_status=preflight_judge_status,
            preflight_judge_error=preflight_judge_error,
            infra_burst_count=infra_burst_count,
            abort_reason=abort_reason,
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
            synthesis_status = "task_error"
            judge_status = "skipped"
            score_status = "invalid"
            answer = ""
            citations_found_display: list[str] = []
            citations_found_canonical: list[str] = []
            structured_citations: list[dict[str, Any]] = []
            answer_trace: dict[str, Any] = {}
            answer_excerpt = ""
            error_text: str | None = None
            judge_error: str | None = None
            keypoint_hits = [False] * len(row.expected_answer_keypoints)
            judge_mode_used = "skipped"
            citation_precision = 0.0
            keypoint_coverage = 0.0
            score = 0.0

            try:
                created_task = client.submit_question(row)
                task_id = str(created_task.get("id") or "").strip() or None
                if not task_id:
                    raise InfrastructureError(f"Task creation for {row.id} returned no task id")
                final_task, duration_s, timed_out = client.poll_task(
                    task_id, timeout_seconds=args.question_timeout
                )
                if timed_out or final_task is None:
                    task_status = "timed_out"
                    synthesis_status = "timeout"
                else:
                    task_status = str(final_task.get("status") or "").strip().lower()
                    completed = task_status in TERMINAL_STATUSES
                    (
                        answer,
                        citations_found_display,
                        citations_found_canonical,
                        structured_citations,
                        answer_trace,
                    ) = extract_answer_and_citations(final_task)
                    if row.source_name:
                        smoke_attempt_count += 1
                        selected_sources_raw = answer_trace.get("selected_sources")
                        selected_set = set(
                            selected_sources_raw if isinstance(selected_sources_raw, list) else []
                        )
                        cross_source_set = selected_set - {row.source_name}
                        if cross_source_set:
                            raise SourceFilterError(
                                f"Source filter smoke check failed for {row.id}: "
                                f"expected selected_sources subset of {{{row.source_name!r}}}, "
                                f"got cross-source contamination {sorted(cross_source_set)!r} "
                                f"(full set: {selected_sources_raw!r})"
                            )
                        if not selected_set:
                            empty_source_streak += 1
                            empty_source_count += 1
                            if empty_source_streak >= SMOKE_EMPTY_STREAK_LIMIT:
                                raise SourceFilterError(
                                    f"Systematic empty retrieval for {row.id}: "
                                    f"{empty_source_streak} consecutive Qs returned empty "
                                    f"selected_sources for requested {row.source_name!r}"
                                )
                            if (
                                smoke_attempt_count <= SMOKE_EARLY_WINDOW
                                and empty_source_count
                                >= max(2, int(smoke_attempt_count * SMOKE_EARLY_EMPTY_FRACTION) + 1)
                            ):
                                raise SourceFilterError(
                                    f"Systematic empty retrieval for {row.id}: "
                                    f"{empty_source_count}/{smoke_attempt_count} early Qs returned empty "
                                    f"selected_sources for requested {row.source_name!r}"
                                )
                        else:
                            empty_source_streak = 0
                    answer_excerpt = truncate_utf8(answer, ANSWER_EXCERPT_MAX_BYTES)
                    citation_precision = compute_citation_precision(row.expected_citations, citations_found_canonical)
                    if answer.strip():
                        synthesis_status = "pass"
                        try:
                            keypoint_hits, judge_mode_used = judge.judge(
                                question=row.question,
                                answer=answer,
                                keypoints=row.expected_answer_keypoints,
                            )
                            judge_status = "pass"
                        except Exception as exc:  # noqa: BLE001
                            judge_status = "fail"
                            judge_mode_used = args.judge_mode
                            judge_error = f"{type(exc).__name__}: {exc}"
                    else:
                        synthesis_status = "empty" if task_status == "completed" else "task_error"
                        judge_status = "skipped"
            except InfrastructureError as exc:
                infrastructure_failed = True
                duration_s = time.monotonic() - question_started
                task_status = "infrastructure_error"
                synthesis_status = "task_error"
                error_text = str(exc)
                if isinstance(exc, SourceFilterError):
                    abort_reason = (
                        "systematic_empty_retrieval"
                        if "Systematic empty retrieval" in str(exc)
                        else "source_filter_broken"
                    )
            except Exception as exc:  # pragma: no cover - defensive failure capture
                infrastructure_failed = True
                duration_s = time.monotonic() - question_started
                task_status = "runner_error"
                synthesis_status = "task_error"
                error_text = f"{type(exc).__name__}: {exc}"

            if task_status in TERMINAL_STATUSES:
                completed = True
            if synthesis_status == "pass" and judge_status == "pass":
                keypoint_coverage = sum(1 for hit in keypoint_hits if hit) / max(len(keypoint_hits), 1)
                score_status = "valid"
                score = (keypoint_coverage * 60.0) + (citation_precision * 40.0)
            else:
                score_status = "invalid"

            record = {
                "type": "question",
                "question_id": row.id,
                "tier": row.tier,
                "task_id": task_id,
                "task_status": task_status,
                "synthesis_status": synthesis_status,
                "judge_status": judge_status,
                "score_status": score_status,
                "completed": completed,
                "timed_out": timed_out,
                "score": round(score, 2),
                "keypoint_coverage": round(keypoint_coverage, 4),
                "citation_precision": round(citation_precision, 4),
                "keypoint_hits": [
                    {"keypoint": keypoint, "hit": hit}
                    for keypoint, hit in zip(row.expected_answer_keypoints, keypoint_hits)
                ],
                "expected_answer_keypoints": row.expected_answer_keypoints,
                "expected_citations": row.expected_citations,
                "source_name": row.source_name,
                "citations_found": citations_found_display,
                "judge_mode": judge_mode_used,
                "duration_s": round(duration_s, 3),
                "answer_excerpt": answer_excerpt,
                "error": error_text,
                "judge_error": judge_error,
            }
            diagnostics = compute_retrieval_diagnostics(row, answer_trace, structured_citations)
            record.update(
                {
                    "expected_citation_top_rank": diagnostics["expected_citation_top_rank"],
                    "top_k_source_distribution": diagnostics["top_k_source_distribution"],
                    "wrong_source_in_top_k": diagnostics["wrong_source_in_top_k"],
                    "expected_fact_in_card": diagnostics["expected_fact_in_card"],
                }
            )
            record["stage19_buckets"] = assign_stage19_buckets(record, diagnostics)
            records.append(record)

            if score_status == "invalid" and synthesis_status in INFRA_INVALID_SYNTH_STATUSES:
                consecutive_infra_invalid += 1
                if consecutive_infra_invalid >= infra_burst_threshold:
                    infra_burst_count += 1
                    if args.no_pause_on_burst:
                        print(
                            f"BENCH WARNING: {consecutive_infra_invalid} consecutive "
                            "infra-invalid records; pause-on-burst disabled",
                            file=sys.stderr,
                        )
                        consecutive_infra_invalid = 0
                    elif infra_burst_count > len(infra_burst_backoff):
                        abort_reason = "infra_burst_exceeded"
                        print(
                            f"BENCH ABORT: {infra_burst_count} infra bursts; aborting "
                            "bench to avoid contaminated signal",
                            file=sys.stderr,
                        )
                    else:
                        backoff = infra_burst_backoff[infra_burst_count - 1]
                        print(
                            f"BENCH PAUSE: {consecutive_infra_invalid} consecutive "
                            f"infra-invalid records; sleeping {backoff:g}s, then "
                            "probing backend health",
                            file=sys.stderr,
                        )
                        time.sleep(backoff)
                        try:
                            client.ensure_backend_reachable()
                        except Exception as exc:  # noqa: BLE001
                            abort_reason = "backend_unreachable_after_pause"
                            infrastructure_failed = True
                            print(
                                f"BENCH ABORT: backend unreachable after pause: {exc}",
                                file=sys.stderr,
                            )
                        consecutive_infra_invalid = 0
            else:
                consecutive_infra_invalid = 0

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
                question_timeout_seconds=args.question_timeout,
                preflight_judge_status=preflight_judge_status,
                preflight_judge_error=preflight_judge_error,
                infra_burst_count=infra_burst_count,
                abort_reason=abort_reason,
            )
            write_artifact(out_path, running_summary, records)
            print(
                f"{row.id} status={task_status} score={record['score']:.2f} "
                f"judge={judge_mode_used} duration={record['duration_s']:.3f}s"
            )
            if abort_reason:
                break
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
        question_timeout_seconds=args.question_timeout,
        preflight_judge_status=preflight_judge_status,
        preflight_judge_error=preflight_judge_error,
        infra_burst_count=infra_burst_count,
        abort_reason=abort_reason,
    )
    write_artifact(out_path, summary, records)

    abc_records = [record for record in records if record["tier"] in {"A", "B", "C"}]
    abc_completed = sum(1 for record in abc_records if record["completed"])
    abc_ratio = abc_completed / max(len(abc_records), 1)
    print(
        f"artifact={out_path} completed={summary['completed_questions']}/{summary['total_questions']} "
        f"abc_completion_ratio={abc_ratio:.4f} infrastructure_failed={infrastructure_failed}"
    )

    invalid_score_count = summary["score_status_counts"].get("invalid", 0)
    if abort_reason or infrastructure_failed or invalid_score_count or abc_ratio < 0.9:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
