# Copyright (c) 2026 David Schmid
"""Exercise the SQL execution boundary shared by lifecycle and SkyPilot providers."""

import base64
import unittest
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from resources import postgres_exec
from resources.postgres_exec import PostgresExec

from .helpers import expect_equal


class ExecutionTests(unittest.TestCase):
    """Reject command failures, missing primaries, and partial output."""

    def setUp(self) -> None:
        """Construct API and process doubles without Kubernetes configuration."""
        with (
            patch.object(postgres_exec.kubernetes.client, "CoreV1Api"),
            patch.object(postgres_exec.kubernetes.client, "CustomObjectsApi"),
        ):
            self.executor = PostgresExec(MagicMock())
        self.executor.custom.get_namespaced_custom_object.return_value = {
            "status": {"currentPrimary": "postgres-3"},
        }
        self.process = MagicMock()
        self.process.is_open.return_value = False
        self.process.returncode = 0
        self.process.read_stdout.return_value = "  result\n"
        self.run = self.enterContext(
            patch.object(postgres_exec, "stream", return_value=self.process),
        )

    def test_success_uses_current_primary_and_returns_only_stdout(self) -> None:
        """Warnings on stderr must not become query data."""
        self.process.read_stderr.return_value = "warning"
        result = self.executor.query("SELECT 1;", database="skypilot_db")
        expect_equal(result, "result")
        expect_equal(self.run.call_args.args[1], "postgres-3")
        command = self.run.call_args.kwargs["command"]
        expect_equal(command[command.index("-d") + 1], "skypilot_db")
        for option in ("-X", "-w", "ON_ERROR_STOP=1"):
            expect_equal(option in command, expected=True)
        expect_equal(self.run.call_args.kwargs["_preload_content"], expected=False)
        expect_equal(self.process.close.call_count, 1)

    def test_each_query_discovers_the_current_primary(self) -> None:
        """A failover changes the execution target without a deployment change."""
        self.executor.custom.get_namespaced_custom_object.side_effect = [
            {"status": {"currentPrimary": "postgres-3"}},
            {"status": {"currentPrimary": "postgres-4"}},
        ]
        self.executor.query("SELECT 1;")
        self.executor.query("SELECT 1;")
        expect_equal(
            [call.args[1] for call in self.run.call_args_list],
            ["postgres-3", "postgres-4"],
        )

    def test_failure_or_missing_exit_status_never_returns_partial_output(self) -> None:
        """Do not trust stdout unless the remote status explicitly reports success."""
        self.process.read_stderr.return_value = "sensitive-sql-value"
        for returncode in (1, 2, 3, None):
            self.process.returncode = returncode
            with (
                self.subTest(returncode=returncode),
                pytest.raises(RuntimeError) as error,
            ):
                self.executor.query("SELECT 1;")
            expect_equal("sensitive-sql-value" in str(error.value), expected=False)
        expect_equal(self.process.read_stdout.call_count, 0)
        expect_equal(self.process.close.call_count, 4)

    def test_process_timeout_closes_the_stream(self) -> None:
        """A still-open command must raise even if it has already produced output."""
        self.process.is_open.return_value = True
        with pytest.raises(TimeoutError):
            self.executor.query("SELECT 1;", timeout=2)
        expect_equal(self.process.run_forever.call_args.kwargs["timeout"], 2)
        expect_equal(self.process.close.call_count, 1)
        expect_equal(self.process.read_stdout.call_count, 0)

    def test_nonpositive_deadline_does_not_start_a_query(self) -> None:
        """An expired caller deadline cannot turn into an unlimited exec wait."""
        with pytest.raises(TimeoutError):
            self.executor.query("SELECT 1;", timeout=0)
        expect_equal(self.run.call_count, 0)

    def test_missing_or_terminating_primary_does_not_start_exec(self) -> None:
        """Do not select an assumed pod when CNPG cannot identify a usable primary."""
        cases = [
            {},
            {"status": {"currentPrimary": ""}},
            {
                "metadata": {"deletionTimestamp": "2026-10-09T12:00:00Z"},
                "status": {"currentPrimary": "postgres-3"},
            },
        ]
        for cluster in cases:
            self.executor.custom.get_namespaced_custom_object.return_value = cluster
            with self.subTest(cluster=cluster), pytest.raises(RuntimeError):
                self.executor.query("SELECT 1;")
        expect_equal(self.run.call_count, 0)

    def test_discovery_distinguishes_absence_from_api_failures(self) -> None:
        """Do not hide authorization failures behind an absent-Cluster result."""
        not_found = ApiException(status=404)
        self.executor.custom.get_namespaced_custom_object.side_effect = not_found
        expect_equal(self.executor.cluster(), None)
        for status in (403, 500):
            self.executor.custom.get_namespaced_custom_object.side_effect = (
                ApiException(status=status)
            )
            with self.subTest(status=status), pytest.raises(ApiException):
                self.executor.cluster()

    def test_values_are_sent_over_stdin_without_becoming_sql_or_argv(self) -> None:
        """Preserve multiline values, quotes, backslashes, and Unicode safely."""
        value = "secret: 'quoted'\\path\n\\q\nSELECT 1; # ü"
        self.executor.query("SELECT :'config_value';", {"config_value": value})
        command = self.run.call_args.kwargs["command"]
        expect_equal(any(value in argument for argument in command), expected=False)
        payload = self.process.write_stdin.call_args.args[0]
        encoded = payload.split("decode('", 1)[1].split("'", 1)[0]
        expect_equal(base64.b64decode(encoded).decode(), value)
        expect_equal(value in payload, expected=False)
        expect_equal('AS "config_value"\n\\gset' in payload, expected=True)
        expect_equal(payload.endswith("SELECT :'config_value';\n\\q\n"), expected=True)

    def test_variable_names_cannot_inject_commands_or_disable_error_checking(
        self,
    ) -> None:
        """Only ordinary variable identifiers can be used as gset aliases."""
        for name in ("ON_ERROR_STOP", 'value"; DROP TABLE config_yaml;', "value\n\\q"):
            with (
                self.subTest(name=name),
                pytest.raises(ValueError, match="variable name"),
            ):
                self.executor.query("SELECT 1;", {name: "0"})
        expect_equal(self.run.call_count, 0)
