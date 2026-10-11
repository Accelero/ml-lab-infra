# Copyright (c) 2026 David Schmid
"""Exercise integration-test policy setup and teardown without a cluster."""

import json
import unittest
from unittest.mock import MagicMock, patch

import conftest
import pytest
import yaml
from integration import test_skypilot_admin_policy

from .helpers import expect_equal


def row(settings: dict[str, object]) -> str:
    """Encode a persisted configuration row."""
    return json.dumps({"value": yaml.safe_dump(settings)})


class PolicyFixtureTests(unittest.TestCase):
    """Preserve concurrent settings when restoring a test's original policy."""

    def test_teardown_restores_only_the_policy(self) -> None:
        """Merge the original policy into settings changed during the test."""
        for original in ({"admin_policy": "original.Policy"}, {}):
            current = {"admin_policy": "test.Policy", "other_setting": "concurrent"}
            expected = {"other_setting": "concurrent", **original}
            executor = MagicMock()
            executor.query.side_effect = [
                row(original),
                row(current),
                "1",
                row(expected),
            ]
            with (
                self.subTest(original=original),
                patch.object(conftest, "PostgresExec", return_value=executor),
            ):
                fixture = conftest.original_skypilot_config.__wrapped__(MagicMock())
                expect_equal(next(fixture), original)
                with pytest.raises(StopIteration):
                    next(fixture)
            values = executor.query.call_args_list[2].args[1]
            expect_equal(yaml.safe_load(values["config_value"]), expected)

    def test_exception_still_restores_the_policy(self) -> None:
        """Restore the policy when the fixture receives a test exception."""
        original = {"admin_policy": "original.Policy"}
        executor = MagicMock()
        executor.query.side_effect = [
            row(original),
            row({"admin_policy": "test.Policy"}),
            "1",
            row(original),
        ]
        with patch.object(conftest, "PostgresExec", return_value=executor):
            fixture = conftest.original_skypilot_config.__wrapped__(MagicMock())
            next(fixture)
            with pytest.raises(RuntimeError, match="test failed"):
                fixture.throw(RuntimeError("test failed"))
        expect_equal(executor.query.call_count, 4)

    def test_live_test_uses_the_conditional_policy_writer(self) -> None:
        """Exercise the live test's helper calls with a checked SQL double."""
        original = {"admin_policy": "original.Policy", "other_setting": "keep"}
        expected = original | {"admin_policy": "test_module.SentinelPolicy"}
        executor = MagicMock()
        executor.query.side_effect = [row(original), "1", row(expected), row(expected)]
        with patch.object(
            test_skypilot_admin_policy,
            "PostgresExec",
            return_value=executor,
        ):
            test_skypilot_admin_policy.test_admin_policy_patch(MagicMock(), original)
        expect_equal(executor.query.call_count, 4)

    def test_invalid_original_policy_fails_before_mutation(self) -> None:
        """Reject a snapshot the fixture cannot restore exactly."""
        executor = MagicMock()
        executor.query.return_value = row({"admin_policy": None})
        with patch.object(conftest, "PostgresExec", return_value=executor):
            fixture = conftest.original_skypilot_config.__wrapped__(MagicMock())
            with pytest.raises(TypeError):
                next(fixture)
        expect_equal(executor.query.call_count, 1)

    def test_kubernetes_fixture_closes_after_teardown(self) -> None:
        """Keep the shared client open until dependent fixtures finish."""
        with (
            patch.object(conftest, "kubeconfig_client") as client,
        ):
            stack = MagicMock()
            stack.output.return_value = "test-config"
            fixture = conftest.k8s_client.__wrapped__(stack)
            expect_equal(next(fixture), client.return_value.__enter__.return_value)
            client.return_value.__exit__.assert_not_called()
            with pytest.raises(StopIteration):
                next(fixture)
            client.return_value.__exit__.assert_called_once()
