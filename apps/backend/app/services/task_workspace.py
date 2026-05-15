from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tarfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from app.agents.schemas import GeneratedPlanPayload
from app.core.config import Settings, get_settings
from app.schemas.evidence import EvidenceItem, EvidenceSource


_UNSAFE_PATH_CHARS = re.compile(r"[\x00-\x1f`$;&|<>'\"*?!]")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_GLOBAL_MEMORY_DIRS = (
    "_global/memory/codebase_facts",
    "_global/memory/gate_failures",
    "_global/memory/prompt_lessons",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    return str(value or "")


def _is_terminal_status(value: object) -> bool:
    return _status_value(value) in {"completed", "failed", "rolled_back"}


def _remove_tree(path: Path) -> None:
    def _rm_readonly(func, target, _exc_info):  # noqa: ANN001
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except Exception:  # noqa: BLE001
            pass

    shutil.rmtree(path, onerror=_rm_readonly)


def _safe_relative_path(value: str, *, field_name: str = "path") -> str:
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if _UNSAFE_PATH_CHARS.search(normalized):
        raise ValueError(f"{field_name} contains unsafe characters")
    if PurePosixPath(normalized).is_absolute() or PureWindowsPath(normalized).is_absolute():
        raise ValueError(f"{field_name} must be relative")
    parts = PurePosixPath(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must not contain dot segments")
    return normalized


def _safe_identifier(value: str, *, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(f"{field_name} contains unsafe characters")
    return normalized


class TaskWorkspace:
    """File-system scratchpad for one task."""

    def __init__(self, task_id: str, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.task_id = _safe_identifier(task_id, field_name="task_id")
        self._workspace_base = Path(self.settings.agent_workspace_root).resolve()
        self._task_root = (self._workspace_base / self.task_id).resolve()
        if not self._is_under(self._task_root, self._workspace_base):
            raise ValueError(f"Path escapes workspace: {self._task_root}")

    @classmethod
    def for_task(cls, task_id: str, settings: Settings | None = None) -> "TaskWorkspace":
        return cls(task_id=task_id, settings=settings)

    @property
    def root(self) -> Path:
        return self._task_root

    @classmethod
    def ensure_global_memory_dirs(cls, settings: Settings | None = None) -> None:
        resolved_settings = settings or get_settings()
        base = Path(resolved_settings.agent_workspace_root).resolve()
        for relative in _GLOBAL_MEMORY_DIRS:
            path = (base / relative).resolve()
            if not cls._is_under(path, base):
                raise ValueError(f"Path escapes workspace: {path}")
            path.mkdir(parents=True, exist_ok=True)

    def write_intent(
        self,
        *,
        intent_text: str,
        language: str | None,
        must_touch_files: list[str],
        scenario: str,
        request_text: str | None = None,
        jira_issue_body: str | None = None,
        jira_issue_key: str | None = None,
    ) -> None:
        self._ensure_task_layout()
        safe_must_touch = [
            _safe_relative_path(path, field_name="must_touch_files")
            for path in must_touch_files
        ]
        normalized_request = (request_text if request_text is not None else intent_text).strip()
        normalized_jira_body = (jira_issue_body or "").strip()
        payload = {
            "task_id": self.task_id,
            "intent_text": intent_text,
            "request_text": normalized_request,
            "jira_issue_body": normalized_jira_body,
            "jira_issue_key": (jira_issue_key or "").strip(),
            "language": language,
            "must_touch_files": safe_must_touch,
            "scenario": scenario,
            "written_at": _utcnow_iso(),
        }
        lines = [
            "---",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            "---",
            "# Intent",
            "",
            f"Scenario: {scenario}",
            f"Language: {language or 'unknown'}",
            "",
            "Must touch files:",
        ]
        if safe_must_touch:
            lines.extend(f"- {path}" for path in safe_must_touch)
        else:
            lines.append("- none")
        lines.extend(["", "## Request", "", intent_text.rstrip(), ""])
        if normalized_jira_body:
            lines.extend(["## Jira Issue Body", "", normalized_jira_body, ""])
        self._atomic_write_text(self.root / "intent.md", "\n".join(lines))

    def read_intent(self) -> dict[str, Any] | None:
        path = self._validate_path(self.root / "intent.md")
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0] == "---":
            try:
                end = lines.index("---", 1)
            except ValueError:
                end = -1
            if end > 1:
                try:
                    payload = json.loads("\n".join(lines[1:end]))
                except json.JSONDecodeError:
                    return None
                return payload if isinstance(payload, dict) else None
        return None

    def add_evidence(self, items: list[EvidenceItem]) -> None:
        if not items:
            return
        self._ensure_task_layout()
        manifest_path = self._validate_path(self.root / "evidence" / "manifest.json")
        raw_existing = self._read_json_file(manifest_path, default=[])
        existing = [
            entry
            for entry in raw_existing
            if isinstance(entry, dict)
        ] if isinstance(raw_existing, list) else []
        threshold = int(getattr(self.settings, "agent_workspace_snippet_inline_threshold", 4000))
        for item in items:
            item_id = _safe_identifier(item.id, field_name="evidence id")
            _safe_relative_path(item.file_path, field_name="file_path")
            metadata = dict(item.metadata or {})
            snippet = item.snippet or ""
            if len(snippet) > threshold:
                snippet_path = self.root / "evidence" / "snippets" / f"{item_id}.txt"
                self._atomic_write_text(snippet_path, snippet)
                metadata["snippet_file"] = f"evidence/snippets/{item_id}.txt"
                metadata["snippet_truncated"] = True
                item = item.model_copy(update={"snippet": snippet[:threshold], "metadata": metadata})
            existing.append(item.model_dump(mode="json"))
        self._atomic_write_json(self.root / "evidence" / "manifest.json", existing)

    def list_evidence(
        self,
        *,
        source_filter: list[EvidenceSource] | None = None,
    ) -> list[EvidenceItem]:
        path = self._validate_path(self.root / "evidence" / "manifest.json")
        if not path.is_file():
            return []
        raw = self._read_json_file(path, default=[])
        if not isinstance(raw, list):
            return []
        allowed = set(source_filter or [])
        items: list[EvidenceItem] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if allowed and entry.get("source") not in allowed:
                continue
            metadata = entry.get("metadata")
            snippet_file = metadata.get("snippet_file") if isinstance(metadata, dict) else None
            if isinstance(snippet_file, str):
                snippet_path = self._validate_path(self.root / snippet_file)
                if snippet_path.is_file():
                    entry = {**entry, "snippet": snippet_path.read_text(encoding="utf-8")}
            items.append(EvidenceItem.model_validate(entry))
        return items

    def evidence_dir(self) -> Path:
        self._ensure_task_layout()
        return self._validate_path(self.root / "evidence")

    def write_plan(self, *, plan_payload: GeneratedPlanPayload, reason: str) -> None:
        self._ensure_task_layout()
        plan_dict = plan_payload.model_dump(mode="json")
        self._atomic_write_json(self.root / "plan" / "current.json", plan_dict)
        self._atomic_write_text(self.root / "plan" / "current.md", self._format_plan_md(plan_dict))
        self._append_jsonl(
            self.root / "plan" / "history.jsonl",
            {
                "written_at": _utcnow_iso(),
                "reason": reason,
                "plan": plan_dict,
            },
        )

    def read_plan(self) -> dict[str, Any] | None:
        path = self._validate_path(self.root / "plan" / "current.json")
        if not path.is_file():
            return None
        payload = self._read_json_file(path, default=None)
        return payload if isinstance(payload, dict) else None

    def next_attempt_index(self) -> int:
        attempts = self._validate_path(self.root / "attempts")
        if not attempts.is_dir():
            return 1
        indices = [
            int(child.name)
            for child in attempts.iterdir()
            if child.is_dir() and child.name.isdigit()
        ]
        return max(indices, default=0) + 1

    def attempt_dir(self, n: int) -> Path:
        if n < 1:
            raise ValueError("attempt index must be >= 1")
        self._ensure_task_layout()
        path = self.root / "attempts" / f"{n:03d}"
        return self._validate_path(path)

    def write_attempt_diff(self, n: int, diff: str) -> None:
        path = self.attempt_dir(n)
        path.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(path / "diff.patch", diff)

    def write_attempt_review(self, n: int, *, report_dict: dict, narrative: str) -> None:
        path = self.attempt_dir(n)
        path.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(path / "review.json", report_dict)
        self._atomic_write_text(path / "review.md", narrative.rstrip() + "\n")

    def write_attempt_compile(self, n: int, result_dict: dict) -> None:
        path = self.attempt_dir(n)
        path.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(path / "compile.json", result_dict)

    def append_audit(self, event_name: str, payload: dict) -> None:
        self._ensure_task_layout()
        self._append_jsonl(
            self.root / "audit.jsonl",
            {
                "at": _utcnow_iso(),
                "event_name": event_name,
                "payload": payload,
            },
        )

    def write_memory_artifact(self, name: str, payload: dict[str, Any]) -> None:
        """Write a named learning-loop artifact inside this task workspace."""
        self._ensure_task_layout()
        safe_name = _safe_identifier(name, field_name="memory artifact name")
        self._atomic_write_json(self.root / "memory" / safe_name, payload)

    def write_checkpoint(
        self,
        *,
        stage_completed: str,
        next_stage: str | None,
        resume_args: dict,
    ) -> None:
        self._ensure_task_layout()
        payload = {
            "task_id": self.task_id,
            "stage_completed": stage_completed,
            "next_stage": next_stage,
            "resume_args": resume_args,
            "updated_at": _utcnow_iso(),
        }
        self._atomic_write_json(self.root / "checkpoint.json", payload)

    def read_checkpoint(self) -> dict[str, Any] | None:
        path = self._validate_path(self.root / "checkpoint.json")
        if not path.is_file():
            return None
        payload = self._read_json_file(path, default=None)
        return payload if isinstance(payload, dict) else None

    def has_checkpoint(self) -> bool:
        return self._validate_path(self.root / "checkpoint.json").is_file()

    def archive(self) -> None:
        if not self.root.is_dir():
            return
        archive_dir = self._validate_workspace_path(self._workspace_base / "_archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = self._validate_workspace_path(archive_dir / f"{self.task_id}.tar.gz")
        tmp_path = self._validate_workspace_path(archive_dir / f"{self.task_id}.tar.gz.tmp")
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(self.root, arcname=self.task_id, recursive=True)
        os.replace(tmp_path, archive_path)

    def _ensure_task_layout(self) -> None:
        self.ensure_global_memory_dirs(self.settings)
        for relative in (
            "",
            "evidence/snippets",
            "plan",
            "attempts",
            "memory",
        ):
            self._validate_path(self.root / relative).mkdir(parents=True, exist_ok=True)

    def _validate_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not self._is_under(resolved, self._task_root):
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    def _validate_workspace_path(self, path: Path) -> Path:
        resolved = path.resolve()
        if not self._is_under(resolved, self._workspace_base):
            raise ValueError(f"Path escapes workspace: {path}")
        allowed = {"_archive"}
        try:
            first_part = resolved.relative_to(self._workspace_base).parts[0]
        except (IndexError, ValueError):
            raise ValueError(f"Path escapes workspace: {path}") from None
        if first_part not in allowed:
            raise ValueError(f"Path escapes workspace: {path}")
        return resolved

    @staticmethod
    def _is_under(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _atomic_write_text(self, path: Path, content: str) -> None:
        target = self._validate_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._validate_path(target.with_name(f"{target.name}.tmp"))
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)

    def _atomic_write_json(self, path: Path, payload: object) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self._atomic_write_text(path, text)

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        target = self._validate_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _read_json_file(path: Path, *, default: object) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default

    @staticmethod
    def _format_plan_md(plan_dict: Mapping[str, Any]) -> str:
        lines = [
            "# Current Plan",
            "",
            f"Objective: {plan_dict.get('objective', '')}",
            f"Scenario: {plan_dict.get('scenario', '')}",
            "",
            "## Summary",
            "",
            str(plan_dict.get("change_summary") or plan_dict.get("request_summary") or ""),
            "",
        ]
        steps = plan_dict.get("steps")
        if isinstance(steps, list) and steps:
            lines.extend(["## Steps", ""])
            for index, step in enumerate(steps, start=1):
                if isinstance(step, Mapping):
                    title = step.get("title") or step.get("step_id") or f"Step {index}"
                    lines.append(f"{index}. {title}")
        return "\n".join(lines).rstrip() + "\n"


def sweep_task_workspaces(
    *,
    settings: Settings | None = None,
    task_statuses: Mapping[str, tuple[object, datetime | None]] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    resolved_settings = settings or get_settings()
    base = Path(resolved_settings.agent_workspace_root).resolve()
    TaskWorkspace.ensure_global_memory_dirs(resolved_settings)
    if not base.is_dir():
        return {"deleted": 0, "archived": 0, "skipped_active": 0, "skipped_recent": 0, "failed": 0}

    statuses = task_statuses or {}
    retention_hours = int(getattr(resolved_settings, "agent_workspace_retention_hours", 168))
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=retention_hours)
    archive_on_complete = bool(getattr(resolved_settings, "agent_workspace_archive_on_complete", False))
    counts = {"deleted": 0, "archived": 0, "skipped_active": 0, "skipped_recent": 0, "failed": 0}

    for child in base.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        status_info = statuses.get(child.name)
        if status_info is None or not _is_terminal_status(status_info[0]):
            counts["skipped_active"] += 1
            continue
        mtime = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
        if mtime >= cutoff:
            counts["skipped_recent"] += 1
            continue
        try:
            if archive_on_complete:
                TaskWorkspace.for_task(child.name, resolved_settings).archive()
                counts["archived"] += 1
            _remove_tree(child)
            if child.exists():
                counts["failed"] += 1
            elif not archive_on_complete:
                counts["deleted"] += 1
        except Exception:  # noqa: BLE001
            counts["failed"] += 1
    return counts


def list_interrupted_workspaces(
    *,
    settings: Settings | None = None,
    task_statuses: Mapping[str, tuple[object, datetime | None]] | None = None,
) -> list[dict[str, Any]]:
    resolved_settings = settings or get_settings()
    base = Path(resolved_settings.agent_workspace_root).resolve()
    if not base.is_dir():
        return []
    statuses = task_statuses or {}
    interrupted: list[dict[str, Any]] = []
    for child in base.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        status_info = statuses.get(child.name)
        if status_info is None or _is_terminal_status(status_info[0]):
            continue
        workspace = TaskWorkspace.for_task(child.name, resolved_settings)
        checkpoint = workspace.read_checkpoint()
        if checkpoint:
            interrupted.append(
                {
                    "task_id": child.name,
                    "status": _status_value(status_info[0]),
                    "stage_completed": checkpoint.get("stage_completed"),
                    "checkpoint_updated_at": checkpoint.get("updated_at"),
                }
            )
    return interrupted
