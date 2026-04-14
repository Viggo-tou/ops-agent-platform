from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


JIRA_ISSUE_KEY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9]{1,9}-\d+)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
JIRA_URL_PATTERN = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)


@dataclass(frozen=True)
class JiraIssueReference:
    issue_key: str
    issue_url: str | None = None
    base_url: str | None = None
    source: str = "issue_key"


def extract_jira_issue_reference(text: str) -> JiraIssueReference | None:
    for matched_url in JIRA_URL_PATTERN.findall(text):
        cleaned_url = matched_url.rstrip(").,]")
        parsed = urlparse(cleaned_url)
        if not parsed.scheme or not parsed.netloc:
            continue

        query_values = parse_qs(parsed.query)
        selected_issue = _first_query_value(query_values, "selectedIssue", "issueKey", "issue")
        if selected_issue:
            issue_key = _normalize_issue_key(selected_issue)
            if issue_key:
                return JiraIssueReference(
                    issue_key=issue_key,
                    issue_url=cleaned_url,
                    base_url=f"{parsed.scheme}://{parsed.netloc}",
                    source="url_query",
                )

        path_segments = [segment for segment in parsed.path.split("/") if segment]
        for index, segment in enumerate(path_segments):
            if segment.lower() != "browse" or index + 1 >= len(path_segments):
                continue
            issue_key = _normalize_issue_key(path_segments[index + 1])
            if issue_key:
                return JiraIssueReference(
                    issue_key=issue_key,
                    issue_url=cleaned_url,
                    base_url=f"{parsed.scheme}://{parsed.netloc}",
                    source="url_browse",
                )

        direct_match = JIRA_ISSUE_KEY_PATTERN.search(cleaned_url)
        if direct_match:
            issue_key = _normalize_issue_key(direct_match.group(1))
            if issue_key:
                return JiraIssueReference(
                    issue_key=issue_key,
                    issue_url=cleaned_url,
                    base_url=f"{parsed.scheme}://{parsed.netloc}",
                    source="url_text",
                )

    direct_match = JIRA_ISSUE_KEY_PATTERN.search(text)
    if not direct_match:
        return None

    issue_key = _normalize_issue_key(direct_match.group(1))
    if not issue_key:
        return None

    return JiraIssueReference(issue_key=issue_key)


def looks_like_jira_issue_url(text: str) -> bool:
    reference = extract_jira_issue_reference(text)
    return bool(reference and reference.issue_url)


def _first_query_value(query_values: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = query_values.get(key)
        if values:
            return values[0]
    return None


def _normalize_issue_key(raw_value: str) -> str | None:
    match = JIRA_ISSUE_KEY_PATTERN.search(raw_value or "")
    if not match:
        return None
    return match.group(1).upper()
