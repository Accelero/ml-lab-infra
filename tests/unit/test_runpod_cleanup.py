"""Verify GPU ownership, cost limits, inventory validation, and cleanup retries."""

import json
import pathlib
import tempfile
import unittest
from decimal import Decimal
from http.client import BadStatusLine, IncompleteRead
from unittest.mock import MagicMock, patch

import pytest
from support import runpod, runpod_watchdog
from support.runpod import Pod, RunPodAPI, budget_violation, owns_pod
from support.runpod_watchdog import CleanupState, Lease, monitor

from .helpers import expect_equal

CLUSTER = "mltest-0123456789abcdef"


def pod(identifier: str = "owned", **changes: object) -> Pod:
    """Construct a one-GPU pod with a known cheap price."""
    fields = {
        "identifier": identifier,
        "name": f"{CLUSTER}-abcdef12-head",
        "status": "RUNNING",
        "hourly_cost": Decimal("0.20"),
        "gpu_count": 1,
        "network_volume": False,
    }
    return Pod(**(fields | changes))


class OwnershipTests(unittest.TestCase):
    """Protect unrelated hardware while allowing exact cloud naming variations."""

    def test_only_exact_test_cluster_nodes_are_owned(self) -> None:
        """The optional user hash must not broaden the invocation prefix."""
        for name in (f"{CLUSTER}-head", f"{CLUSTER}-abcdef12-worker"):
            expect_equal(owns_pod(CLUSTER, name), expected=True)
        for name in (
            "production-head",
            f"{CLUSTER}0-head",
            f"prefix-{CLUSTER}-head",
            f"{CLUSTER}-head-extra",
            f"{CLUSTER}-controller",
        ):
            with self.subTest(name=name):
                expect_equal(owns_pod(CLUSTER, name), expected=False)

    def test_broad_cluster_prefixes_are_rejected(self) -> None:
        """A mistaken generic prefix cannot authorize bulk deletion."""
        for name in ("mltest", "", "production", "mltest-123"):
            with (
                self.subTest(name=name),
                pytest.raises(ValueError, match=r"require|allow|Invalid"),
            ):
                owns_pod(name, "production-head")

    def test_unknown_price_multiple_gpus_and_volumes_are_rejected(self) -> None:
        """Every billable resource must fit the test's explicit limits."""
        cases = (
            [pod(hourly_cost=None)],
            [pod(hourly_cost=Decimal("0.60"))],
            [pod(gpu_count=2)],
            [pod(network_volume=True)],
            [pod(), pod("second")],
        )
        for pods in cases:
            with self.subTest(pods=pods):
                expect_equal(
                    budget_violation(pods, Decimal("0.35")) is not None,
                    expected=True,
                )
        expect_equal(budget_violation([pod()], Decimal("0.35")), None)

    def test_lease_rejects_unbounded_or_excessive_budgets(self) -> None:
        """Invalid cost and cleanup settings fail before any hardware request."""
        for changes in (
            {"lifetime": 0},
            {"lifetime": 601},
            {"hourly_limit": Decimal("0.51")},
            {"hourly_limit": Decimal("NaN")},
            {"quiet_period": 0},
            {"cleanup_limit": 181},
        ):
            with (
                self.subTest(changes=changes),
                pytest.raises(ValueError, match=r"require|allow|Invalid"),
            ):
                Lease(CLUSTER, **changes)


class InventoryTests(unittest.TestCase):
    """Check the HTTP contract without using real credentials or hardware."""

    def test_invalid_inventory_never_means_empty(self) -> None:
        """Malformed successful responses cannot provide cleanup proof."""
        for payload in ({}, [{"id": "owned"}], ["private-response"]):
            with (
                self.subTest(payload=payload),
                patch.object(RunPodAPI, "request", return_value=payload),
                pytest.raises((RuntimeError, TypeError)),
            ):
                RunPodAPI("private-api-key").pods()

    def test_nonfinite_or_missing_prices_are_unknown(self) -> None:
        """NaN, infinity, negative, or absent prices cannot bypass the budget."""
        for price in (None, "NaN", "Infinity", "-1", "0", "invalid"):
            payload = [
                {
                    "id": "owned",
                    "name": f"{CLUSTER}-head",
                    "desiredStatus": "RUNNING",
                    "costPerHr": price,
                    "gpuCount": 1,
                },
            ]
            with (
                self.subTest(price=price),
                patch.object(RunPodAPI, "request", return_value=payload),
            ):
                expect_equal(RunPodAPI("private-api-key").pods()[0].hourly_cost, None)

    def test_http_failures_close_handles_without_exposing_response(self) -> None:
        """Error bodies and API keys remain absent from raised exceptions."""
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 403
        response.read.return_value = b"private-api-key"
        with (
            patch.object(runpod, "HTTPSConnection", return_value=connection),
            pytest.raises(RuntimeError, match="HTTP 403") as error,
        ):
            RunPodAPI("private-api-key").pods()
        response.read.assert_not_called()
        response.close.assert_called_once()
        connection.close.assert_called_once()
        expect_equal("private-api-key" in str(error.value), expected=False)
        expect_equal(
            "private-api-key" in repr(RunPodAPI("private-api-key")),
            expected=False,
        )

    def test_delete_is_idempotent_and_quotes_the_pod_id(self) -> None:
        """Already-removed pods are harmless; IDs cannot alter the API path."""
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 404
        with patch.object(runpod, "HTTPSConnection", return_value=connection):
            RunPodAPI("private-api-key").delete("id/with?path")
        expect_equal(
            connection.request.call_args.args[:2],
            ("DELETE", "/v1/pods/id%2Fwith%3Fpath"),
        )

    def test_broken_http_responses_are_redacted_retryable_errors(self) -> None:
        """HTTP protocol failures must not crash the independent cleanup loop."""
        for failure in (BadStatusLine("private-api-key"), IncompleteRead(b"private")):
            connection = MagicMock()
            connection.getresponse.side_effect = failure
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(runpod, "HTTPSConnection", return_value=connection),
                pytest.raises(RuntimeError, match="transport or response") as error,
            ):
                RunPodAPI("private-api-key").pods()
            connection.close.assert_called_once()
            expect_equal("private-api-key" in str(error.value), expected=False)


class SweepTests(unittest.TestCase):
    """Require repeated observed absence after successful termination requests."""

    def test_default_lease_allows_fifty_cents_and_terminates_above_it(self) -> None:
        """Provider prices at the cap run; one cent above triggers cleanup."""
        for price, terminate in (("0.50", False), ("0.51", True)):
            with self.subTest(price=price):
                api = MagicMock()
                api.pods.return_value = [pod(hourly_cost=Decimal(price))]
                state = CleanupState()
                expect_equal(state.sweep(api, Lease(CLUSTER)), expected=False)
                expect_equal(state.violation is not None, expected=terminate)
                if terminate:
                    api.delete.assert_called_once_with("owned")
                else:
                    api.delete.assert_not_called()

    def test_delete_is_followed_by_a_fresh_inventory_and_quiet_period(self) -> None:
        """A stopped or still-visible pod cannot count as cleaned."""
        api = MagicMock()
        api.pods.side_effect = [
            [pod(status="EXITED"), pod("foreign", name="production-head")],
            [],
            [],
        ]
        lease = Lease(CLUSTER)
        state = CleanupState()
        state.begin(lease)
        with patch.object(runpod_watchdog.time, "monotonic") as clock:
            clock.return_value = 0
            expect_equal(state.sweep(api, lease), expected=False)
            expect_equal(state.sweep(api, lease), expected=False)
            clock.return_value = 61
            expect_equal(state.sweep(api, lease), expected=True)
        api.delete.assert_called_once_with("owned")

    def test_late_pod_creation_resets_cleanup_proof(self) -> None:
        """A second pod appearing during cleanup must also be terminated."""
        api = MagicMock()
        api.pods.side_effect = [[], [pod("late")], []]
        state = CleanupState()
        lease = Lease(CLUSTER)
        state.begin(lease)
        with patch.object(runpod_watchdog.time, "monotonic") as clock:
            clock.return_value = 0
            state.sweep(api, lease)
            clock.return_value = 59
            expect_equal(state.sweep(api, lease), expected=False)
            clock.return_value = 61
            expect_equal(state.sweep(api, lease), expected=False)
        api.delete.assert_called_once_with("late")

    def test_observed_pod_remains_owned_after_a_rename(self) -> None:
        """A changed display name cannot make a known rented pod escape cleanup."""
        api = MagicMock()
        api.pods.side_effect = [[pod()], [pod(name="renamed-by-provider")]]
        state = CleanupState()
        lease = Lease(CLUSTER)
        expect_equal(state.sweep(api, lease), expected=False)
        api.delete.assert_not_called()
        state.begin(lease)
        expect_equal(state.sweep(api, lease), expected=False)
        api.delete.assert_called_once_with("owned")

    def test_api_outage_fails_cleanup_without_leaking_external_error(self) -> None:
        """An unreadable inventory never proves hardware release."""
        api = MagicMock()
        api.pods.side_effect = RuntimeError("private-api-key")
        lease = Lease(CLUSTER)
        with tempfile.TemporaryDirectory() as directory:
            report = pathlib.Path(directory) / "cleanup.json"
            report.with_suffix(".stop").touch()
            with patch.object(runpod_watchdog.time, "monotonic") as clock:
                clock.side_effect = [0, 0, 0, 181]
                with pytest.raises(RuntimeError, match="not be verified"):
                    monitor(api, lease, report)
            result = json.loads(report.read_text())
        expect_equal(result["status"], "failed")
        expect_equal("private-api-key" in json.dumps(result), expected=False)
