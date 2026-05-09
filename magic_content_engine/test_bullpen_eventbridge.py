"""Unit tests for EventBridge Scheduler configuration.

Covers the schedule rules defined in scripts/create_infrastructure.py:
- Editor-in-Chief: cron(0 9 ? * MON *), Pacific/Auckland, FLEXIBLE_TIME_WINDOW OFF
- Archivist (Whakaaro): cron(0 23 * * ? *), Pacific/Auckland, FLEXIBLE_TIME_WINDOW OFF

Tests verify the constants and the boto3 calls made by
create_eventbridge_schedules() without hitting real AWS.

Requirements: REQ-24
"""

from __future__ import annotations

import importlib
import sys
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

# The script lives in scripts/, not a package — import it directly.
import importlib.util
import pathlib

_SCRIPT_PATH = (
    pathlib.Path(__file__).parent.parent / "scripts" / "create_infrastructure.py"
)

_spec = importlib.util.spec_from_file_location("create_infrastructure", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Aliases for the constants and functions under test
SCHEDULE_EDITOR_IN_CHIEF = _mod.SCHEDULE_EDITOR_IN_CHIEF
SCHEDULE_ARCHIVIST = _mod.SCHEDULE_ARCHIVIST
SCHEDULE_EXPR_EDITOR_IN_CHIEF = _mod.SCHEDULE_EXPR_EDITOR_IN_CHIEF
SCHEDULE_EXPR_ARCHIVIST = _mod.SCHEDULE_EXPR_ARCHIVIST
SCHEDULE_TIMEZONE = _mod.SCHEDULE_TIMEZONE
LAMBDA_EDITOR_IN_CHIEF = _mod.LAMBDA_EDITOR_IN_CHIEF
LAMBDA_ARCHIVIST = _mod.LAMBDA_ARCHIVIST
REGION = _mod.REGION
create_eventbridge_schedules = _mod.create_eventbridge_schedules
_create_or_update_schedule = _mod._create_or_update_schedule


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACCOUNT_ID = "123456789012"
_SCHEDULER_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/eventbridge-scheduler-role"


class FakeSchedulerClient:
    """Minimal fake boto3 scheduler client.

    Tracks create_schedule and update_schedule calls.
    Simulates get_schedule raising ResourceNotFoundException by default
    (schedule does not exist yet), or returning a dict if pre-seeded.
    """

    def __init__(
        self,
        existing_schedules: set[str] | None = None,
        create_raises: Exception | None = None,
        update_raises: Exception | None = None,
    ) -> None:
        self._existing = existing_schedules or set()
        self._create_raises = create_raises
        self._update_raises = update_raises
        self.create_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []

    def get_schedule(self, Name: str) -> dict[str, Any]:
        if Name in self._existing:
            return {"Name": Name}
        from botocore.exceptions import ClientError
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "GetSchedule",
        )

    def create_schedule(self, **kwargs: Any) -> dict[str, Any]:
        if self._create_raises is not None:
            raise self._create_raises
        self.create_calls.append(kwargs)
        return {"ScheduleArn": f"arn:aws:scheduler:{REGION}:{_ACCOUNT_ID}:schedule/default/{kwargs['Name']}"}

    def update_schedule(self, **kwargs: Any) -> dict[str, Any]:
        if self._update_raises is not None:
            raise self._update_raises
        self.update_calls.append(kwargs)
        return {"ScheduleArn": f"arn:aws:scheduler:{REGION}:{_ACCOUNT_ID}:schedule/default/{kwargs['Name']}"}


def _run_create_schedules(
    existing_schedules: set[str] | None = None,
    create_raises: Exception | None = None,
    update_raises: Exception | None = None,
) -> FakeSchedulerClient:
    """Run create_eventbridge_schedules() with a fake scheduler client.

    Patches boto3.client so no real AWS calls are made.
    Returns the fake client so callers can inspect recorded calls.
    """
    fake_client = FakeSchedulerClient(
        existing_schedules=existing_schedules,
        create_raises=create_raises,
        update_raises=update_raises,
    )

    with patch.object(_mod.boto3, "client", return_value=fake_client):
        create_eventbridge_schedules(
            account_id=_ACCOUNT_ID,
            scheduler_role_arn=_SCHEDULER_ROLE_ARN,
        )

    return fake_client


# ---------------------------------------------------------------------------
# Schedule name constants
# ---------------------------------------------------------------------------


class TestScheduleNames:
    def test_editor_in_chief_schedule_name(self) -> None:
        assert SCHEDULE_EDITOR_IN_CHIEF == "mce-editor-in-chief-weekly"

    def test_archivist_schedule_name(self) -> None:
        assert SCHEDULE_ARCHIVIST == "mce-archivist-nightly"


# ---------------------------------------------------------------------------
# Schedule expressions
# ---------------------------------------------------------------------------


class TestScheduleExpressions:
    def test_editor_in_chief_expression(self) -> None:
        assert SCHEDULE_EXPR_EDITOR_IN_CHIEF == "cron(0 9 ? * MON *)"

    def test_archivist_expression(self) -> None:
        assert SCHEDULE_EXPR_ARCHIVIST == "cron(0 23 * * ? *)"

    def test_editor_in_chief_expression_starts_with_cron(self) -> None:
        assert SCHEDULE_EXPR_EDITOR_IN_CHIEF.startswith("cron(")

    def test_archivist_expression_starts_with_cron(self) -> None:
        assert SCHEDULE_EXPR_ARCHIVIST.startswith("cron(")

    def test_editor_in_chief_fires_at_hour_9(self) -> None:
        # cron(minute hour ...) — second token is hour
        inner = SCHEDULE_EXPR_EDITOR_IN_CHIEF[len("cron("):-1]
        parts = inner.split()
        assert parts[1] == "9", f"Expected hour=9, got {parts[1]!r}"

    def test_editor_in_chief_fires_at_minute_0(self) -> None:
        inner = SCHEDULE_EXPR_EDITOR_IN_CHIEF[len("cron("):-1]
        parts = inner.split()
        assert parts[0] == "0", f"Expected minute=0, got {parts[0]!r}"

    def test_editor_in_chief_fires_on_monday(self) -> None:
        inner = SCHEDULE_EXPR_EDITOR_IN_CHIEF[len("cron("):-1]
        parts = inner.split()
        # EventBridge cron: minute hour day-of-month month day-of-week year
        assert parts[4] == "MON", f"Expected day-of-week=MON, got {parts[4]!r}"

    def test_archivist_fires_at_hour_23(self) -> None:
        inner = SCHEDULE_EXPR_ARCHIVIST[len("cron("):-1]
        parts = inner.split()
        assert parts[1] == "23", f"Expected hour=23, got {parts[1]!r}"

    def test_archivist_fires_at_minute_0(self) -> None:
        inner = SCHEDULE_EXPR_ARCHIVIST[len("cron("):-1]
        parts = inner.split()
        assert parts[0] == "0", f"Expected minute=0, got {parts[0]!r}"


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------


class TestTimezone:
    def test_timezone_is_pacific_auckland(self) -> None:
        assert SCHEDULE_TIMEZONE == "Pacific/Auckland"

    def test_editor_in_chief_uses_pacific_auckland(self) -> None:
        # Verified via the constant — both schedules share SCHEDULE_TIMEZONE
        assert SCHEDULE_TIMEZONE == "Pacific/Auckland"

    def test_archivist_uses_pacific_auckland(self) -> None:
        assert SCHEDULE_TIMEZONE == "Pacific/Auckland"


# ---------------------------------------------------------------------------
# FLEXIBLE_TIME_WINDOW OFF
# ---------------------------------------------------------------------------


class TestFlexibleTimeWindowOff:
    def test_editor_in_chief_flexible_time_window_off(self) -> None:
        fake = _run_create_schedules()
        eic_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_EDITOR_IN_CHIEF]
        assert len(eic_calls) == 1
        assert eic_calls[0]["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_archivist_flexible_time_window_off(self) -> None:
        fake = _run_create_schedules()
        arch_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_ARCHIVIST]
        assert len(arch_calls) == 1
        assert arch_calls[0]["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_update_editor_in_chief_flexible_time_window_off(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF, SCHEDULE_ARCHIVIST}
        )
        eic_calls = [c for c in fake.update_calls if c["Name"] == SCHEDULE_EDITOR_IN_CHIEF]
        assert len(eic_calls) == 1
        assert eic_calls[0]["FlexibleTimeWindow"] == {"Mode": "OFF"}

    def test_update_archivist_flexible_time_window_off(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF, SCHEDULE_ARCHIVIST}
        )
        arch_calls = [c for c in fake.update_calls if c["Name"] == SCHEDULE_ARCHIVIST]
        assert len(arch_calls) == 1
        assert arch_calls[0]["FlexibleTimeWindow"] == {"Mode": "OFF"}


# ---------------------------------------------------------------------------
# Target Lambda ARN format
# ---------------------------------------------------------------------------


class TestTargetLambdaArn:
    def _get_create_call(self, fake: FakeSchedulerClient, name: str) -> dict[str, Any]:
        calls = [c for c in fake.create_calls if c["Name"] == name]
        assert len(calls) == 1, f"Expected 1 create call for {name!r}, got {len(calls)}"
        return calls[0]

    def test_editor_in_chief_target_arn_format(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_EDITOR_IN_CHIEF)
        expected_arn = (
            f"arn:aws:lambda:{REGION}:{_ACCOUNT_ID}:function:{LAMBDA_EDITOR_IN_CHIEF}"
        )
        assert call["Target"]["Arn"] == expected_arn

    def test_archivist_target_arn_format(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_ARCHIVIST)
        expected_arn = (
            f"arn:aws:lambda:{REGION}:{_ACCOUNT_ID}:function:{LAMBDA_ARCHIVIST}"
        )
        assert call["Target"]["Arn"] == expected_arn

    def test_editor_in_chief_target_lambda_name(self) -> None:
        assert LAMBDA_EDITOR_IN_CHIEF == "mce-editor-in-chief"

    def test_archivist_target_lambda_name(self) -> None:
        assert LAMBDA_ARCHIVIST == "mce-archivist"

    def test_editor_in_chief_arn_contains_region(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_EDITOR_IN_CHIEF)
        assert REGION in call["Target"]["Arn"]

    def test_archivist_arn_contains_region(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_ARCHIVIST)
        assert REGION in call["Target"]["Arn"]

    def test_editor_in_chief_arn_contains_account_id(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_EDITOR_IN_CHIEF)
        assert _ACCOUNT_ID in call["Target"]["Arn"]

    def test_archivist_arn_contains_account_id(self) -> None:
        fake = _run_create_schedules()
        call = self._get_create_call(fake, SCHEDULE_ARCHIVIST)
        assert _ACCOUNT_ID in call["Target"]["Arn"]


# ---------------------------------------------------------------------------
# Schedule expression passed to boto3
# ---------------------------------------------------------------------------


class TestScheduleExpressionPassedToBoto3:
    def test_editor_in_chief_expression_passed_to_create(self) -> None:
        fake = _run_create_schedules()
        eic_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_EDITOR_IN_CHIEF]
        assert eic_calls[0]["ScheduleExpression"] == SCHEDULE_EXPR_EDITOR_IN_CHIEF

    def test_archivist_expression_passed_to_create(self) -> None:
        fake = _run_create_schedules()
        arch_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_ARCHIVIST]
        assert arch_calls[0]["ScheduleExpression"] == SCHEDULE_EXPR_ARCHIVIST

    def test_editor_in_chief_timezone_passed_to_create(self) -> None:
        fake = _run_create_schedules()
        eic_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_EDITOR_IN_CHIEF]
        assert eic_calls[0]["ScheduleExpressionTimezone"] == SCHEDULE_TIMEZONE

    def test_archivist_timezone_passed_to_create(self) -> None:
        fake = _run_create_schedules()
        arch_calls = [c for c in fake.create_calls if c["Name"] == SCHEDULE_ARCHIVIST]
        assert arch_calls[0]["ScheduleExpressionTimezone"] == SCHEDULE_TIMEZONE


# ---------------------------------------------------------------------------
# Idempotency — update when schedule already exists
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_update_called_when_schedule_exists(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF}
        )
        update_names = [c["Name"] for c in fake.update_calls]
        assert SCHEDULE_EDITOR_IN_CHIEF in update_names

    def test_create_not_called_when_schedule_exists(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF}
        )
        create_names = [c["Name"] for c in fake.create_calls]
        assert SCHEDULE_EDITOR_IN_CHIEF not in create_names

    def test_both_schedules_created_when_neither_exists(self) -> None:
        fake = _run_create_schedules()
        create_names = {c["Name"] for c in fake.create_calls}
        assert SCHEDULE_EDITOR_IN_CHIEF in create_names
        assert SCHEDULE_ARCHIVIST in create_names

    def test_both_schedules_updated_when_both_exist(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF, SCHEDULE_ARCHIVIST}
        )
        update_names = {c["Name"] for c in fake.update_calls}
        assert SCHEDULE_EDITOR_IN_CHIEF in update_names
        assert SCHEDULE_ARCHIVIST in update_names
        assert fake.create_calls == []

    def test_expression_passed_on_update(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF, SCHEDULE_ARCHIVIST}
        )
        eic_update = next(
            c for c in fake.update_calls if c["Name"] == SCHEDULE_EDITOR_IN_CHIEF
        )
        assert eic_update["ScheduleExpression"] == SCHEDULE_EXPR_EDITOR_IN_CHIEF

    def test_timezone_passed_on_update(self) -> None:
        fake = _run_create_schedules(
            existing_schedules={SCHEDULE_EDITOR_IN_CHIEF, SCHEDULE_ARCHIVIST}
        )
        arch_update = next(
            c for c in fake.update_calls if c["Name"] == SCHEDULE_ARCHIVIST
        )
        assert arch_update["ScheduleExpressionTimezone"] == SCHEDULE_TIMEZONE
