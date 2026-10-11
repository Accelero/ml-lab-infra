# Copyright (c) 2026 David Schmid
"""Reject failed or unverified SkyPilot policy writes without deployed services."""

import json
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


def config_row(config: dict[str, object]) -> str:
    """Encode a SQL row without exposing YAML whitespace to output trimming."""
    return json.dumps({"value": yaml.safe_dump(config)})


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
        """Only the policy changes in the YAML supplied to the conditional write."""
        expected = _ORIGINAL | {"admin_policy": _PROPS["admin_policy"]}
        for operation in ("create", "update"):
            self.executor.query.reset_mock()
            self.executor.query.side_effect = [
                "1",
                config_row(_ORIGINAL),
                "1",
                config_row(expected),
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
                expect_equal(variables["previous_value"], yaml.safe_dump(_ORIGINAL))
                expect_equal(self.executor.query.call_count, 4)
        expect_equal(self.api_client.return_value.__exit__.call_count, 2)

    def test_sql_failures_propagate_at_every_stage(self) -> None:
        """Fail deployment on SQL errors at every stage."""
        for stage in range(4):
            outputs = ["1", config_row(_ORIGINAL), "1", ""]
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
                    config_row(_ORIGINAL),
                    "1",
                    config_row(observed),
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
            config_row(_ORIGINAL),
            "1",
            config_row(expected),
        ]
        Provider().delete("test", _PROPS)
        variables = self.executor.query.call_args_list[2].args[1]
        expect_equal(yaml.safe_load(variables["config_value"]), expected)
        expect_equal(self.executor.query.call_count, 4)

    def test_delete_failure_or_unverified_removal_is_not_skipped(self) -> None:
        """A command timeout must not be mistaken for an absent table."""
        cases = (
            [TimeoutError()],
            ["1", config_row(_ORIGINAL), "1", config_row(_ORIGINAL)],
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
            self.executor.query.side_effect = ["1", json.dumps({"value": text})]
            with (
                self.subTest(text=text),
                pytest.raises((TypeError, RuntimeError)) as error,
            ):
                Provider().create(_PROPS)
            expect_equal("sensitive-value" in str(error.value), expected=False)

    def test_missing_config_row_has_no_yaml_snapshot(self) -> None:
        """A successful SELECT with no row remains a valid empty configuration."""
        self.executor.query.return_value = ""
        expect_equal(read_config(self.executor), ({}, None))

    def test_missing_config_row_is_inserted_and_verified(self) -> None:
        """Initial creation stores only the desired policy and confirms it."""
        expected = {"admin_policy": _PROPS["admin_policy"]}
        self.executor.query.side_effect = ["1", "", "1", config_row(expected)]
        result = Provider().create(_PROPS)
        expect_equal(result.outs, _PROPS)
        variables = self.executor.query.call_args_list[2].args[1]
        expect_equal("previous_value" in variables, expected=False)
        expect_equal(yaml.safe_load(variables["config_value"]), expected)
        expect_equal(self.executor.query.call_count, 4)

    def test_conflicting_writes_reread_and_preserve_concurrent_settings(self) -> None:
        """Creation, update, and removal must merge against the latest YAML."""
        concurrent = _ORIGINAL | {"other_setting": {"nested": "concurrent"}}
        for operation in ("create", "update", "delete"):
            expected = dict(concurrent)
            if operation == "delete":
                del expected["admin_policy"]
            else:
                expected["admin_policy"] = _PROPS["admin_policy"]
            self.executor.query.reset_mock()
            self.executor.query.side_effect = [
                "1",
                config_row(_ORIGINAL),
                "",
                config_row(concurrent),
                "1",
                config_row(expected),
            ]
            with self.subTest(operation=operation):
                run_operation(operation)
                variables = self.executor.query.call_args_list[4].args[1]
                expect_equal(variables["previous_value"], yaml.safe_dump(concurrent))
                expect_equal(yaml.safe_load(variables["config_value"]), expected)
                expect_equal(self.executor.query.call_count, 6)

    def test_concurrent_insert_is_reread_before_updating(self) -> None:
        """An absent-row snapshot must not overwrite a newly inserted row."""
        concurrent = {"other_setting": "created by another writer"}
        expected = concurrent | {"admin_policy": _PROPS["admin_policy"]}
        self.executor.query.side_effect = [
            "1",
            "",
            "",
            config_row(concurrent),
            "1",
            config_row(expected),
        ]
        Provider().create(_PROPS)
        insertion = self.executor.query.call_args_list[2]
        expect_equal("ON CONFLICT (key) DO NOTHING" in insertion.args[0], expected=True)
        expect_equal("previous_value" in insertion.args[1], expected=False)
        variables = self.executor.query.call_args_list[4].args[1]
        expect_equal(yaml.safe_load(variables["config_value"]), expected)
        expect_equal(variables["previous_value"], yaml.safe_dump(concurrent))

    def test_concurrent_row_deletion_is_recreated_only_when_applying_policy(
        self,
    ) -> None:
        """Removal never recreates a row that another writer has deleted."""
        for operation in ("create", "update", "delete"):
            self.executor.query.reset_mock()
            outputs = ["1", config_row(_ORIGINAL), "", ""]
            if operation != "delete":
                outputs += ["1", config_row({"admin_policy": _PROPS["admin_policy"]})]
            self.executor.query.side_effect = outputs
            with self.subTest(operation=operation):
                run_operation(operation)
                expect_equal(self.executor.query.call_count, len(outputs))
                if operation != "delete":
                    sql = self.executor.query.call_args_list[4].args[0]
                    expect_equal("ON CONFLICT (key) DO NOTHING" in sql, expected=True)

    def test_continuous_conflicts_fail_without_leaking_configuration(self) -> None:
        """Retries stop after five stale writes without an unconditional overwrite."""
        for operation in ("create", "update", "delete"):
            self.executor.query.reset_mock()
            outputs = ["1"]
            for revision in range(5):
                config = _ORIGINAL | {"secret": "sensitive-value", "revision": revision}
                outputs += [config_row(config), ""]
            self.executor.query.side_effect = outputs
            with (
                self.subTest(operation=operation),
                pytest.raises(RuntimeError, match="all 5 write attempts") as error,
            ):
                run_operation(operation)
            expect_equal(self.executor.query.call_count, 11)
            expect_equal("sensitive-value" in str(error.value), expected=False)

    def test_sql_failure_on_conflict_retry_is_not_retried(self) -> None:
        """Retry only stale writes, never hide a subsequent SQL execution failure."""
        self.executor.query.side_effect = [
            "1",
            config_row(_ORIGINAL),
            "",
            TimeoutError(),
        ]
        with pytest.raises(TimeoutError):
            Provider().create(_PROPS)
        expect_equal(self.executor.query.call_count, 4)

    def test_exact_yaml_snapshot_survives_sql_output_trimming(self) -> None:
        """Comparison uses original whitespace, comments, and quoted YAML values."""
        raw = '\n# keep this snapshot\nother_setting: "quoted \\"value\\""\n\n'
        expected = {
            "other_setting": 'quoted "value"',
            "admin_policy": _PROPS["admin_policy"],
        }
        self.executor.query.side_effect = [
            "1",
            json.dumps({"value": raw}).strip(),
            "1",
            config_row(expected),
        ]
        Provider().create(_PROPS)
        variables = self.executor.query.call_args_list[2].args[1]
        expect_equal(variables["previous_value"], raw)
        expect_equal(yaml.safe_load(variables["config_value"]), expected)

    def test_invalid_read_or_write_responses_fail_without_logging_contents(
        self,
    ) -> None:
        """Malformed SQL responses and null or empty rows must fail closed."""
        for response in (
            "sensitive-value",
            json.dumps(["sensitive-value"]),
            json.dumps({"wrong": "sensitive-value"}),
            json.dumps({"value": None}),
            json.dumps({"value": ""}),
        ):
            self.executor.query.side_effect = ["1", response]
            with (
                self.subTest(response=response),
                pytest.raises((TypeError, RuntimeError)) as error,
            ):
                Provider().create(_PROPS)
            expect_equal("sensitive-value" in str(error.value), expected=False)
        self.executor.query.side_effect = [
            "1",
            config_row(_ORIGINAL),
            "sensitive-value",
        ]
        with pytest.raises(RuntimeError, match="Unexpected response") as error:
            Provider().create(_PROPS)
        expect_equal("sensitive-value" in str(error.value), expected=False)

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
