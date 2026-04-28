from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from app.core.config import get_settings


@dataclass(frozen=True)
class CCFileMatch:
    path: str
    line: int | None = None


@dataclass(frozen=True)
class CCToolResult:
    tool: Literal["glob", "grep", "read"]
    args: dict[str, Any]
    matches: list[CCFileMatch]
    raw_text: str | None
    duration_ms: int
    error: str | None = None


def cc_glob(pattern: str, *, cwd: Path, timeout_s: float = 10.0) -> CCToolResult:
    start = time.perf_counter()
    args = {"pattern": pattern}
    prompt = (
        "Use Claude Code Glob to list repository-relative files matching this pattern.\n"
        "Output only one path per line, no prose.\n"
        f"Pattern: {pattern}\n"
    )
    output, error = _run_claude_cli(prompt, cwd=cwd, timeout_s=timeout_s)
    matches = _parse_file_lines(output, cwd=cwd) if not error else []
    return CCToolResult("glob", args, matches, output or None, _elapsed_ms(start), error)


def cc_grep(
    pattern: str,
    *,
    cwd: Path,
    file_glob: str | None = None,
    case_insensitive: bool = True,
    timeout_s: float = 20.0,
) -> CCToolResult:
    settings = get_settings()
    excludes = list(getattr(settings, "cc_grep_default_excludes", []) or [])
    start = time.perf_counter()
    args: dict[str, Any] = {
        "pattern": pattern,
        "file_glob": file_glob,
        "case_insensitive": case_insensitive,
        "excludes": excludes,
    }
    exclude_text = "\n".join(f"- {item}" for item in excludes)
    prompt = (
        "Use Claude Code Grep to search the repository.\n"
        "Output only matches as path:line:content, no prose.\n"
        f"Pattern: {pattern}\n"
        f"File glob: {file_glob or '*'}\n"
        f"Case insensitive: {case_insensitive}\n"
        f"Exclude these globs:\n{exclude_text}\n"
    )
    output, error = _run_claude_cli(prompt, cwd=cwd, timeout_s=timeout_s)
    matches = _parse_grep_lines(output, cwd=cwd) if not error else []
    return CCToolResult("grep", args, matches, output or None, _elapsed_ms(start), error)


def cc_read(
    path: str,
    *,
    cwd: Path,
    line_range: tuple[int, int] | None = None,
    timeout_s: float = 15.0,
) -> CCToolResult:
    start = time.perf_counter()
    args: dict[str, Any] = {"path": path, "line_range": line_range}
    normalized = _normalize_repo_path(path, cwd=cwd)
    if normalized is None:
        return CCToolResult(
            "read",
            args,
            [],
            None,
            _elapsed_ms(start),
            "path must be repository-relative and inside cwd",
        )
    range_text = f"{line_range[0]}-{line_range[1]}" if line_range else "whole file"
    prompt = (
        "Use Claude Code Read to read this repository-relative file.\n"
        "Output only the requested file contents, no prose.\n"
        f"Path: {normalized}\n"
        f"Line range: {range_text}\n"
    )
    output, error = _run_claude_cli(prompt, cwd=cwd, timeout_s=timeout_s)
    matches = [CCFileMatch(path=normalized, line=line_range[0] if line_range else None)] if not error else []
    return CCToolResult("read", args, matches, output or None, _elapsed_ms(start), error)


def _run_claude_cli(prompt: str, *, cwd: Path, timeout_s: float) -> tuple[str, str | None]:
    if not cwd.exists():
        raise FileNotFoundError(f"source repo path does not exist: {cwd}")
    settings = get_settings()
    command = shutil.which(str(settings.claude_code_command))
    if not command:
        return "", f"Claude Code CLI not found: {settings.claude_code_command}"

    cli_args = str(getattr(settings, "claude_code_args", "") or "").split()
    if "-p" not in cli_args and "--print" not in cli_args:
        cli_args.append("--print")
    if "--dangerously-skip-permissions" not in cli_args:
        cli_args.append("--dangerously-skip-permissions")
    cmd = [command, *cli_args, "-"]
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return "", f"timeout after {timeout_s}s"
    except OSError as exc:
        return "", str(exc)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return result.stdout or "", f"rc={result.returncode}: {stderr[:500]}"
    stderr_noise = (result.stderr or "").strip()
    return result.stdout or "", stderr_noise[:500] or None


def _parse_file_lines(output: str, *, cwd: Path) -> list[CCFileMatch]:
    paths = _json_paths(output)
    if paths is None:
        paths = []
        for raw_line in output.splitlines():
            line = _clean_output_line(raw_line)
            if line:
                paths.append(line)
    matches: list[CCFileMatch] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_repo_path(str(path), cwd=cwd)
        if normalized and normalized not in seen:
            seen.add(normalized)
            matches.append(CCFileMatch(path=normalized))
    return matches


def _parse_grep_lines(output: str, *, cwd: Path) -> list[CCFileMatch]:
    matches: list[CCFileMatch] = []
    seen: set[tuple[str, int | None]] = set()
    for raw_line in output.splitlines():
        line = _clean_output_line(raw_line)
        if not line:
            continue
        parsed = re.match(r"^(?P<path>.+?):(?P<line>\d+)(?::|[-\s])(.*)$", line)
        if parsed:
            normalized = _normalize_repo_path(parsed.group("path"), cwd=cwd)
            line_number: int | None = int(parsed.group("line"))
        else:
            normalized = _normalize_repo_path(line, cwd=cwd)
            line_number = None
        if normalized:
            key = (normalized, line_number)
            if key not in seen:
                seen.add(key)
                matches.append(CCFileMatch(path=normalized, line=line_number))
    return matches


def _json_paths(output: str) -> list[str] | None:
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return [str(item) for item in data if isinstance(item, str)]
    if isinstance(data, dict):
        value = data.get("paths") or data.get("files") or data.get("matches")
        if isinstance(value, list):
            result: list[str] = []
            for item in value:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    result.append(item["path"])
            return result
    return None


def _clean_output_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    cleaned = cleaned.strip("`'\" ")
    return cleaned


def _normalize_repo_path(path: str, *, cwd: Path) -> str | None:
    cleaned = _clean_output_line(path).replace("\\", "/")
    if not cleaned:
        return None
    try:
        candidate = Path(cleaned)
        if candidate.is_absolute():
            rel = candidate.resolve().relative_to(cwd.resolve())
            cleaned = rel.as_posix()
    except (OSError, ValueError):
        return None
    if PurePosixPath(cleaned).is_absolute() or PureWindowsPath(cleaned).is_absolute():
        return None
    parts = PurePosixPath(cleaned).parts
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return str(PurePosixPath(cleaned))


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
