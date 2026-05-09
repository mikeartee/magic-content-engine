"""Tests for IAM policy documents in docs/iam-policies/.

Covers:
- Each policy JSON is valid and parseable
- Correct IAM policy structure (Version + Statement)
- Researcher policy excludes S3 PutObject and SES
- Desk Editor policy excludes all S3 actions
- Writer policy excludes S3 DeleteObject and SES
- Subeditor policy excludes S3 PutObject and SES
- Publisher policy excludes S3 DeleteObject
- Archivist policy excludes SES
- Editor-in-Chief policy has Lambda InvokeFunction and DynamoDB permissions

Requirements: Bullpen Req 3, 5, 7, 9, 12, 18, 20
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Resolve docs/iam-policies/ relative to this test file (magic_content_engine/)
_REPO_ROOT = Path(__file__).parent.parent
_POLICY_DIR = _REPO_ROOT / "docs" / "iam-policies"

_POLICY_FILES = {
    "researcher": "researcher-policy.json",
    "desk_editor": "desk-editor-policy.json",
    "writer": "writer-policy.json",
    "subeditor": "subeditor-policy.json",
    "publisher": "publisher-policy.json",
    "archivist": "archivist-policy.json",
    "editor_in_chief": "editor-in-chief-policy.json",
}


def _load_policy(name: str) -> dict:
    """Load and parse a policy JSON file by agent name key."""
    path = _POLICY_DIR / _POLICY_FILES[name]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _all_actions(policy: dict) -> set[str]:
    """Return the flat set of all Action strings across all statements."""
    actions: set[str] = set()
    for stmt in policy.get("Statement", []):
        raw = stmt.get("Action", [])
        if isinstance(raw, str):
            actions.add(raw.lower())
        else:
            actions.update(a.lower() for a in raw)
    return actions


def _has_action_prefix(policy: dict, prefix: str) -> bool:
    """Return True if any action in the policy starts with the given prefix."""
    prefix = prefix.lower()
    return any(a.startswith(prefix) for a in _all_actions(policy))


def _has_specific_action(policy: dict, action: str) -> bool:
    """Return True if the exact action (case-insensitive) is present."""
    return action.lower() in _all_actions(policy)


# ---------------------------------------------------------------------------
# 1. All policy files exist and are valid JSON
# ---------------------------------------------------------------------------


class TestPolicyFilesExistAndAreValid:
    @pytest.mark.parametrize("agent_key", list(_POLICY_FILES.keys()))
    def test_policy_file_exists(self, agent_key: str) -> None:
        path = _POLICY_DIR / _POLICY_FILES[agent_key]
        assert path.exists(), f"Policy file missing: {path}"

    @pytest.mark.parametrize("agent_key", list(_POLICY_FILES.keys()))
    def test_policy_file_is_valid_json(self, agent_key: str) -> None:
        path = _POLICY_DIR / _POLICY_FILES[agent_key]
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        # Should not raise
        parsed = json.loads(content)
        assert isinstance(parsed, dict), f"Policy root must be a JSON object: {agent_key}"

    @pytest.mark.parametrize("agent_key", list(_POLICY_FILES.keys()))
    def test_policy_has_version(self, agent_key: str) -> None:
        policy = _load_policy(agent_key)
        assert "Version" in policy, f"Missing 'Version' key in {agent_key} policy"
        assert policy["Version"] == "2012-10-17", (
            f"Expected Version '2012-10-17' in {agent_key} policy, "
            f"got {policy['Version']!r}"
        )

    @pytest.mark.parametrize("agent_key", list(_POLICY_FILES.keys()))
    def test_policy_has_statement_list(self, agent_key: str) -> None:
        policy = _load_policy(agent_key)
        assert "Statement" in policy, f"Missing 'Statement' key in {agent_key} policy"
        assert isinstance(policy["Statement"], list), (
            f"'Statement' must be a list in {agent_key} policy"
        )
        assert len(policy["Statement"]) > 0, (
            f"'Statement' must not be empty in {agent_key} policy"
        )

    @pytest.mark.parametrize("agent_key", list(_POLICY_FILES.keys()))
    def test_each_statement_has_required_fields(self, agent_key: str) -> None:
        policy = _load_policy(agent_key)
        for i, stmt in enumerate(policy["Statement"]):
            assert "Effect" in stmt, (
                f"Statement[{i}] missing 'Effect' in {agent_key} policy"
            )
            assert stmt["Effect"] in ("Allow", "Deny"), (
                f"Statement[{i}] 'Effect' must be Allow or Deny in {agent_key} policy"
            )
            assert "Action" in stmt, (
                f"Statement[{i}] missing 'Action' in {agent_key} policy"
            )
            assert "Resource" in stmt, (
                f"Statement[{i}] missing 'Resource' in {agent_key} policy"
            )


# ---------------------------------------------------------------------------
# 2. Researcher policy — NO S3 PutObject, NO SES
# ---------------------------------------------------------------------------


class TestResearcherPolicy:
    def test_has_s3_get_object(self) -> None:
        policy = _load_policy("researcher")
        assert _has_specific_action(policy, "s3:GetObject"), (
            "Researcher policy must include s3:GetObject"
        )

    def test_no_s3_put_object(self) -> None:
        policy = _load_policy("researcher")
        assert not _has_specific_action(policy, "s3:PutObject"), (
            "Researcher policy must NOT include s3:PutObject"
        )

    def test_no_s3_delete_object(self) -> None:
        policy = _load_policy("researcher")
        assert not _has_specific_action(policy, "s3:DeleteObject"), (
            "Researcher policy must NOT include s3:DeleteObject"
        )

    def test_no_ses_actions(self) -> None:
        policy = _load_policy("researcher")
        assert not _has_action_prefix(policy, "ses:"), (
            "Researcher policy must NOT include any SES actions"
        )

    def test_has_bedrock_invoke_model(self) -> None:
        policy = _load_policy("researcher")
        assert _has_specific_action(policy, "bedrock:InvokeModel"), (
            "Researcher policy must include bedrock:InvokeModel"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("researcher")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Researcher policy must include logs:PutLogEvents"
        )

    def test_s3_resource_scoped_to_ami_context(self) -> None:
        policy = _load_policy("researcher")
        s3_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "s3:getobject" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    s3_resources.append(resource)
                else:
                    s3_resources.extend(resource)
        assert any("ami-context/" in r for r in s3_resources), (
            "Researcher S3 GetObject must be scoped to ami-context/ prefix"
        )


# ---------------------------------------------------------------------------
# 3. Desk Editor policy — NO S3, NO SES
# ---------------------------------------------------------------------------


class TestDeskEditorPolicy:
    def test_no_s3_actions(self) -> None:
        policy = _load_policy("desk_editor")
        assert not _has_action_prefix(policy, "s3:"), (
            "Desk Editor policy must NOT include any S3 actions"
        )

    def test_no_ses_actions(self) -> None:
        policy = _load_policy("desk_editor")
        assert not _has_action_prefix(policy, "ses:"), (
            "Desk Editor policy must NOT include any SES actions"
        )

    def test_has_bedrock_invoke_model(self) -> None:
        policy = _load_policy("desk_editor")
        assert _has_specific_action(policy, "bedrock:InvokeModel"), (
            "Desk Editor policy must include bedrock:InvokeModel"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("desk_editor")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Desk Editor policy must include logs:PutLogEvents"
        )


# ---------------------------------------------------------------------------
# 4. Writer policy — NO S3 DeleteObject, NO SES
# ---------------------------------------------------------------------------


class TestWriterPolicy:
    def test_has_s3_put_object(self) -> None:
        policy = _load_policy("writer")
        assert _has_specific_action(policy, "s3:PutObject"), (
            "Writer policy must include s3:PutObject"
        )

    def test_no_s3_delete_object(self) -> None:
        policy = _load_policy("writer")
        assert not _has_specific_action(policy, "s3:DeleteObject"), (
            "Writer policy must NOT include s3:DeleteObject"
        )

    def test_no_ses_actions(self) -> None:
        policy = _load_policy("writer")
        assert not _has_action_prefix(policy, "ses:"), (
            "Writer policy must NOT include any SES actions"
        )

    def test_has_bedrock_invoke_model(self) -> None:
        policy = _load_policy("writer")
        assert _has_specific_action(policy, "bedrock:InvokeModel"), (
            "Writer policy must include bedrock:InvokeModel"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("writer")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Writer policy must include logs:PutLogEvents"
        )

    def test_s3_resource_scoped_to_output(self) -> None:
        policy = _load_policy("writer")
        put_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "s3:putobject" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    put_resources.append(resource)
                else:
                    put_resources.extend(resource)
        assert any("output/" in r for r in put_resources), (
            "Writer S3 PutObject must be scoped to output/ prefix"
        )


# ---------------------------------------------------------------------------
# 5. Subeditor policy — NO S3 PutObject, NO SES
# ---------------------------------------------------------------------------


class TestSubeditorPolicy:
    def test_has_s3_get_object(self) -> None:
        policy = _load_policy("subeditor")
        assert _has_specific_action(policy, "s3:GetObject"), (
            "Subeditor policy must include s3:GetObject"
        )

    def test_no_s3_put_object(self) -> None:
        policy = _load_policy("subeditor")
        assert not _has_specific_action(policy, "s3:PutObject"), (
            "Subeditor policy must NOT include s3:PutObject"
        )

    def test_no_s3_delete_object(self) -> None:
        policy = _load_policy("subeditor")
        assert not _has_specific_action(policy, "s3:DeleteObject"), (
            "Subeditor policy must NOT include s3:DeleteObject"
        )

    def test_no_ses_actions(self) -> None:
        policy = _load_policy("subeditor")
        assert not _has_action_prefix(policy, "ses:"), (
            "Subeditor policy must NOT include any SES actions"
        )

    def test_has_bedrock_invoke_model(self) -> None:
        policy = _load_policy("subeditor")
        assert _has_specific_action(policy, "bedrock:InvokeModel"), (
            "Subeditor policy must include bedrock:InvokeModel"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("subeditor")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Subeditor policy must include logs:PutLogEvents"
        )

    def test_s3_resource_scoped_to_output(self) -> None:
        policy = _load_policy("subeditor")
        get_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "s3:getobject" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    get_resources.append(resource)
                else:
                    get_resources.extend(resource)
        assert any("output/" in r for r in get_resources), (
            "Subeditor S3 GetObject must be scoped to output/ prefix"
        )


# ---------------------------------------------------------------------------
# 6. Publisher policy — NO S3 DeleteObject
# ---------------------------------------------------------------------------


class TestPublisherPolicy:
    def test_has_s3_put_object(self) -> None:
        policy = _load_policy("publisher")
        assert _has_specific_action(policy, "s3:PutObject"), (
            "Publisher policy must include s3:PutObject"
        )

    def test_no_s3_delete_object(self) -> None:
        policy = _load_policy("publisher")
        assert not _has_specific_action(policy, "s3:DeleteObject"), (
            "Publisher policy must NOT include s3:DeleteObject"
        )

    def test_has_ses_send_email(self) -> None:
        policy = _load_policy("publisher")
        assert _has_specific_action(policy, "ses:SendEmail"), (
            "Publisher policy must include ses:SendEmail"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("publisher")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Publisher policy must include logs:PutLogEvents"
        )

    def test_s3_resource_scoped_to_output(self) -> None:
        policy = _load_policy("publisher")
        s3_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() in ("s3:putobject", "s3:getobject") for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    s3_resources.append(resource)
                else:
                    s3_resources.extend(resource)
        assert any("output/" in r for r in s3_resources), (
            "Publisher S3 actions must be scoped to output/ prefix"
        )


# ---------------------------------------------------------------------------
# 7. Archivist policy — NO SES
# ---------------------------------------------------------------------------


class TestArchivistPolicy:
    def test_has_s3_get_object(self) -> None:
        policy = _load_policy("archivist")
        assert _has_specific_action(policy, "s3:GetObject"), (
            "Archivist policy must include s3:GetObject"
        )

    def test_has_s3_put_object(self) -> None:
        policy = _load_policy("archivist")
        assert _has_specific_action(policy, "s3:PutObject"), (
            "Archivist policy must include s3:PutObject"
        )

    def test_no_ses_actions(self) -> None:
        policy = _load_policy("archivist")
        assert not _has_action_prefix(policy, "ses:"), (
            "Archivist policy must NOT include any SES actions"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("archivist")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Archivist policy must include logs:PutLogEvents"
        )

    def test_get_object_scoped_to_ami_context(self) -> None:
        policy = _load_policy("archivist")
        get_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "s3:getobject" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    get_resources.append(resource)
                else:
                    get_resources.extend(resource)
        assert any("ami-context/" in r for r in get_resources), (
            "Archivist S3 GetObject must be scoped to ami-context/ prefix"
        )

    def test_put_object_scoped_to_archive(self) -> None:
        policy = _load_policy("archivist")
        put_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "s3:putobject" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    put_resources.append(resource)
                else:
                    put_resources.extend(resource)
        assert any("archive/" in r for r in put_resources), (
            "Archivist S3 PutObject must be scoped to archive/ prefix"
        )


# ---------------------------------------------------------------------------
# 8. Editor-in-Chief policy
# ---------------------------------------------------------------------------


class TestEditorInChiefPolicy:
    def test_has_lambda_invoke_function(self) -> None:
        policy = _load_policy("editor_in_chief")
        assert _has_specific_action(policy, "lambda:InvokeFunction"), (
            "Editor-in-Chief policy must include lambda:InvokeFunction"
        )

    def test_lambda_resource_scoped_to_mce_functions(self) -> None:
        policy = _load_policy("editor_in_chief")
        lambda_resources: list[str] = []
        for stmt in policy["Statement"]:
            actions = stmt.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            if any(a.lower() == "lambda:invokefunction" for a in actions):
                resource = stmt.get("Resource", "")
                if isinstance(resource, str):
                    lambda_resources.append(resource)
                else:
                    lambda_resources.extend(resource)
        assert any("mce-" in r or "mce-*" in r for r in lambda_resources), (
            "Editor-in-Chief lambda:InvokeFunction must be scoped to mce-* functions"
        )

    def test_has_dynamodb_read_write(self) -> None:
        policy = _load_policy("editor_in_chief")
        assert _has_specific_action(policy, "dynamodb:GetItem"), (
            "Editor-in-Chief policy must include dynamodb:GetItem"
        )
        assert _has_specific_action(policy, "dynamodb:PutItem"), (
            "Editor-in-Chief policy must include dynamodb:PutItem"
        )

    def test_has_ses_send_email(self) -> None:
        policy = _load_policy("editor_in_chief")
        assert _has_specific_action(policy, "ses:SendEmail"), (
            "Editor-in-Chief policy must include ses:SendEmail (approval gate)"
        )

    def test_has_cloudwatch_logs(self) -> None:
        policy = _load_policy("editor_in_chief")
        assert _has_specific_action(policy, "logs:PutLogEvents"), (
            "Editor-in-Chief policy must include logs:PutLogEvents"
        )

    def test_no_s3_actions(self) -> None:
        policy = _load_policy("editor_in_chief")
        assert not _has_action_prefix(policy, "s3:"), (
            "Editor-in-Chief policy must NOT include any S3 actions"
        )
