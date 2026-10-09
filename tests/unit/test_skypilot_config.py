# Copyright (c) 2026 David Schmid
"""Reject failed or unverified SkyPilot policy writes without deployed services."""

import unittest
from unittest.mock import MagicMock, patch

import pytest
import yaml

from resources import skypilot_config
from resources.skypilot_config import _Provider as Provider
from resources.skypilot_config import _read_config as read_config
from resources.skypilot_config import _wait_for_table as wait_for_table

from .helpers import expect_equal

_PROPS = {
    "kubeconfig": "test-connection",
    "admin_policy": "test_module.TestPolicy",
}
_ORIGINAL = {"admin_policy": "old.Policy", "other_setting": {"nested": "keep"}}


def run_operation(operation: str) -> None:
    """Dispatch a provider lifecycle call for failure regression cases."""
    provider = Provider()
    if operation == "create":
        provider.create(_PROPS)
    elif operation == "update":
        provider.update("test", {}, _PROPS)
    else:
        provider.delete("test", _PROPS)


class PolicyTests(unittest.TestCase):
    """Check provider success and failure at the persisted configuration boundary."""

    def setUp(self) -> None:
        """Replace the Kubernetes connection and shared SQL executor."""
        self.executor = MagicMock()
        self.api_client = self.enterContext(
            patch.object(skypilot_config, "_api_client"),
        )
        self.enterContext(
            patch.object(skypilot_config, "PostgresExec", return_value=self.executor),
        )

    def test_create_and_update_preserve_settings_and_verify_policy(self) -> None:
        """Only the policy changes in the YAML supplied to the UPSERT."""
        expected = _ORIGINAL | {"admin_policy": _PROPS["admin_policy"]}
        for operation in ("create", "update"):
            self.executor.query.reset_mock()
            self.executor.query.side_effect = [
                "1",
                yaml.safe_dump(_ORIGINAL),
                "",
                yaml.safe_dump(expected),
            ]
            with self.subTest(operation=operation):
                provider = Provider()
                result = (
                    provider.create(_PROPS)
                    if operation == "create"
                    else provider.update("test", {}, _PROPS)
                )
                expect_equal(result.outs, _PROPS)
                variables = self.executor.query.call_args_list[2].args[1]
                expect_equal(yaml.safe_load(variables["config_value"]), expected)
                expect_equal(self.executor.query.call_count, 4)
        expect_equal(self.api_client.return_value.__exit__.call_count, 2)

    def test_sql_failures_propagate_at_every_stage(self) -> None:
        """Fail deployment on SQL errors at every stage."""
        for stage in range(4):
            outputs = ["1", yaml.safe_dump(_ORIGINAL), "", ""]
            outputs[stage] = RuntimeError("SQL failed")
            for operation in ("create", "update", "delete"):
                self.executor.query.side_effect = outputs
                with (
                    self.subTest(stage=stage, operation=operation),
                    pytest.raises(RuntimeError, match="SQL failed"),
                ):
                    run_operation(operation)

    def test_unverified_policy_never_reports_deployment_success(self) -> None:
        """A successful command alone does not prove the intended policy was stored."""
        for observed in ({}, {"admin_policy": "wrong.Policy"}):
            for operation in ("create", "update"):
                self.executor.query.side_effect = [
                    "1",
                    yaml.safe_dump(_ORIGINAL),
                    "",
                    yaml.safe_dump(observed),
                ]
                with (
                    self.subTest(observed=observed, operation=operation),
                    pytest.raises(RuntimeError, match="verified"),
                ):
                    run_operation(operation)

    def test_delete_preserves_other_settings_and_verifies_removal(self) -> None:
        """Remove just the policy field and require a confirming read."""
        expected = {"other_setting": _ORIGINAL["other_setting"]}
        self.executor.query.side_effect = [
            "1",
            yaml.safe_dump(_ORIGINAL),
            "",
            yaml.safe_dump(expected),
        ]
        Provider().delete("test", _PROPS)
        variables = self.executor.query.call_args_list[2].args[1]
        expect_equal(yaml.safe_load(variables["config_value"]), expected)
        expect_equal(self.executor.query.call_count, 4)

    def test_delete_failure_or_unverified_removal_is_not_skipped(self) -> None:
        """A command timeout must not be mistaken for an absent table."""
        cases = (
            [TimeoutError()],
            ["1", yaml.safe_dump(_ORIGINAL), "", yaml.safe_dump(_ORIGINAL)],
        )
        for outputs in cases:
            self.executor.query.side_effect = outputs
            with (
                self.subTest(outputs=outputs),
                pytest.raises((TimeoutError, RuntimeError)),
            ):
                Provider().delete("test", _PROPS)

    def test_delete_skips_only_confirmed_absent_table_or_policy(self) -> None:
        """Absence is a successful query result, not an ignored execution error."""
        for outputs in ([""], ["1", ""]):
            self.executor.query.reset_mock()
            self.executor.query.side_effect = outputs
            with self.subTest(outputs=outputs):
                Provider().delete("test", _PROPS)
                expect_equal(self.executor.query.call_count, len(outputs))

    def test_invalid_config_is_rejected_without_overwriting_or_leaking_it(self) -> None:
        """Reject invalid config without including its contents in errors."""
        for text in (
            "sensitive-value",
            "- sensitive-value",
            "null",
            "secret: [sensitive-value",
        ):
            self.executor.query.side_effect = ["1", text]
            with (
                self.subTest(text=text),
                pytest.raises((TypeError, RuntimeError)) as error,
            ):
                Provider().create(_PROPS)
            expect_equal("sensitive-value" in str(error.value), expected=False)

    def test_missing_config_row_is_initialized(self) -> None:
        """A successful SELECT with no row remains a valid empty configuration."""
        self.executor.query.return_value = ""
        expect_equal(read_config(self.executor), {})

    def test_empty_policy_cannot_be_verified_as_an_absent_config(self) -> None:
        """Require a policy before connecting or writing configuration."""
        for policy in (None, "", " "):
            props = _PROPS | {"admin_policy": policy}
            with (
                self.subTest(policy=policy),
                pytest.raises(ValueError, match="class path"),
            ):
                Provider().create(props)
        expect_equal(self.api_client.call_count, 0)

    def test_table_wait_retries_only_successful_absence_and_bounds_each_query(
        self,
    ) -> None:
        """Transient absence is polled within the remaining deployment deadline."""
        self.executor.query.side_effect = ["", "1"]
        with patch.object(skypilot_config, "time") as clock:
            clock.monotonic.side_effect = [0, 250, 255, 260]
            wait_for_table(self.executor)
        expect_equal(
            [call.kwargs["timeout"] for call in self.executor.query.call_args_list],
            [50, 40],
        )
        expect_equal(clock.sleep.call_args.args[0], 10)

    def test_table_wait_exhaustion_and_query_failure_raise(self) -> None:
        """The polling timeout and a command timeout both prevent deployment."""
        with patch.object(skypilot_config, "time") as clock:
            clock.monotonic.side_effect = [0, 301]
            with pytest.raises(TimeoutError):
                wait_for_table(self.executor)
        self.executor.query.side_effect = TimeoutError
        with pytest.raises(TimeoutError):
            wait_for_table(self.executor)

    def test_unexpected_table_check_output_is_not_treated_as_absence(self) -> None:
        """Reject malformed responses immediately."""
        self.executor.query.return_value = "ERROR: query failed"
        with pytest.raises(RuntimeError, match="Unexpected response"):
            Provider().create(_PROPS)
