"""Verify bounded HTTPS ingress readiness before test metadata writes."""

from unittest.mock import MagicMock, patch

import pytest
import requests
from support import mlflow_probe

from .helpers import expect_equal


def response(status: int) -> MagicMock:
    """Provide a closable response without contacting an external service."""
    result = MagicMock()
    result.__enter__.return_value.status_code = status
    return result


def test_readiness_retries_connection_failure_and_starting_proxy() -> None:
    """New tailnet ingress may appear before its application backend is ready."""
    starting = response(requests.codes.service_unavailable)
    ready = response(requests.codes.ok)
    with (
        patch.object(
            mlflow_probe.requests,
            "get",
            side_effect=[requests.exceptions.ConnectionError, starting, ready],
        ) as get,
        patch.object(mlflow_probe.time, "sleep"),
    ):
        mlflow_probe.wait_for_tracking_server("https://mlflow.test.invalid/")
    expect_equal(get.call_count, 3)
    expect_equal(get.call_args.args, ("https://mlflow.test.invalid/health",))
    expect_equal(get.call_args.kwargs["allow_redirects"], expected=False)
    starting.__exit__.assert_called_once()
    ready.__exit__.assert_called_once()


def test_readiness_deadline_hides_connection_error_details() -> None:
    """An unreachable service cannot retry forever or expose exception values."""
    with (
        patch.object(
            mlflow_probe.requests,
            "get",
            side_effect=requests.exceptions.ConnectionError("private-value"),
        ) as get,
        patch.object(mlflow_probe.time, "monotonic", side_effect=[0, 0, 1, 5]),
        patch.object(mlflow_probe.time, "sleep"),
        pytest.raises(TimeoutError, match="did not become ready") as failure,
    ):
        mlflow_probe.wait_for_tracking_server("https://mlflow.test.invalid", timeout=5)
    get.assert_called_once_with(
        "https://mlflow.test.invalid/health",
        timeout=5,
        allow_redirects=False,
    )
    expect_equal("private-value" in str(failure.value), expected=False)


def test_readiness_rejects_authentication_failure_without_retrying() -> None:
    """An authentication problem must fail immediately instead of waiting."""
    denied = response(requests.codes.unauthorized)
    with (
        patch.object(mlflow_probe.requests, "get", return_value=denied) as get,
        patch.object(mlflow_probe.time, "sleep") as sleep,
        pytest.raises(RuntimeError, match="HTTP 401"),
    ):
        mlflow_probe.wait_for_tracking_server("https://mlflow.test.invalid")
    get.assert_called_once()
    sleep.assert_not_called()
    denied.__exit__.assert_called_once()
