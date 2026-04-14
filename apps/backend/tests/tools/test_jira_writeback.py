from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.tools.gateway import ToolGateway, ToolInvocationError  # noqa: E402


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        tool_permission_overrides=None,
        tool_default_timeout_seconds=15.0,
        slack_bot_token=None,
        slack_post_message_timeout_seconds=10.0,
        slack_post_message_retry_count=1,
        jira_base_url="https://example.atlassian.net/some/path",
        jira_email="jira@example.com",
        jira_api_token="secret",
        jira_bearer_token=None,
        jira_project_key="OPS",
        jira_issue_type="Task",
        jira_timeout_seconds=15.0,
        jira_retry_count=1,
        internal_api_base_url=None,
        internal_api_timeout_seconds=10.0,
        internal_api_retry_count=1,
        internal_db_url=None,
        internal_db_timeout_seconds=8.0,
        internal_db_retry_count=0,
    )


class JiraWritebackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ToolGateway(db=Mock(), settings=_settings())
        self.transition_definition = self.gateway.registry.get_definition("jira.transition_issue")
        self.comment_definition = self.gateway.registry.get_definition("jira.add_comment")

    def test_transition_issue_success(self) -> None:
        with patch.object(
            self.gateway,
            "_request_json",
            side_effect=[
                {"fields": {"status": {"name": "To Do"}}},
                {"transitions": [{"id": "21", "name": "In Progress"}]},
                {},
                {"fields": {"status": {"name": "In Progress"}}},
            ],
        ) as request_json:
            result = self.gateway._execute_jira_transition_issue(
                definition=self.transition_definition,
                payload={"issue_key": "ops-123", "transition_name": "in progress"},
            )

        self.assertEqual(result["status"], "transitioned")
        self.assertEqual(result["from_status"], "To Do")
        self.assertEqual(result["to_status"], "In Progress")
        self.assertEqual(result["transition_id"], "21")
        self.assertEqual(str(result["transition_name"]).casefold(), "in progress")
        self.assertEqual(result["issue_key"], "OPS-123")
        self.assertEqual(request_json.call_count, 4)

    def test_transition_issue_unknown_transition(self) -> None:
        with patch.object(
            self.gateway,
            "_request_json",
            side_effect=[
                {"fields": {"status": {"name": "To Do"}}},
                {"transitions": [{"id": "20", "name": "Start Progress"}, {"id": "31", "name": "Done"}]},
            ],
        ):
            with self.assertRaises(ToolInvocationError) as raised:
                self.gateway._execute_jira_transition_issue(
                    definition=self.transition_definition,
                    payload={"issue_key": "OPS-123", "transition_name": "In Progress"},
                )

        self.assertFalse(raised.exception.retryable)
        message = str(raised.exception)
        self.assertIn("Start Progress", message)
        self.assertIn("Done", message)

    def test_transition_issue_missing_fields(self) -> None:
        with self.assertRaises(ToolInvocationError):
            self.gateway._execute_jira_transition_issue(
                definition=self.transition_definition,
                payload={"issue_key": "", "transition_name": "In Progress"},
            )
        with self.assertRaises(ToolInvocationError):
            self.gateway._execute_jira_transition_issue(
                definition=self.transition_definition,
                payload={"issue_key": "OPS-123", "transition_name": ""},
            )

    def test_add_comment_success(self) -> None:
        text = "x" * 250
        with patch.object(
            self.gateway,
            "_request_json",
            return_value={"id": "10101", "created": "2026-04-12T00:00:00.000+0000"},
        ) as request_json:
            result = self.gateway._execute_jira_add_comment(
                definition=self.comment_definition,
                payload={"issue_key": "ops-123", "text": text},
            )

        self.assertEqual(result["status"], "commented")
        self.assertEqual(result["comment_id"], "10101")
        self.assertEqual(result["created"], "2026-04-12T00:00:00.000+0000")
        self.assertEqual(result["excerpt"], text[:200])
        self.assertEqual(result["issue_key"], "OPS-123")
        request_json.assert_called_once()
        self.assertEqual(
            request_json.call_args.kwargs["json_body"]["body"]["content"][0]["content"][0]["text"],
            text,
        )

    def test_add_comment_missing_fields(self) -> None:
        with self.assertRaises(ToolInvocationError):
            self.gateway._execute_jira_add_comment(
                definition=self.comment_definition,
                payload={"issue_key": "OPS-123", "text": ""},
            )


if __name__ == "__main__":
    unittest.main()
