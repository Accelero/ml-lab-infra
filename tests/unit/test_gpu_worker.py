"""Retain useful worker failure categories without recording secret values."""

import io
import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from support import gpu_worker
from support.gpu_worker import _record_worker_failure

from .helpers import expect_equal

if TYPE_CHECKING:
    import pathlib


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            "Tailscale OAuth token exchange failed (HTTP 401). private-secret",
            "Tailscale OAuth token exchange failed with HTTP 401.",
        ),
        (
            "Tailscale auth key creation failed (HTTP 403). private-secret",
            "Tailscale auth key creation failed with HTTP 403.",
        ),
        (
            "RunPod insufficient account balance; api_key=private-secret",
            "The provider reported insufficient account balance.",
        ),
        (
            (
                "TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET "
                "are not set. "
                "private-secret"
            ),
            "The admin policy is missing its Tailscale OAuth environment variables.",
        ),
        (
            "Unrecognized server response with private-secret",
            "Unclassified worker failure.",
        ),
    ],
    ids=["oauth", "auth-key", "balance", "environment", "unknown"],
)
def test_failure_report_retains_only_safe_classification(
    tmp_path: pathlib.Path,
    message: str,
    reason: str,
) -> None:
    """Known and unknown server messages must not reach the persisted report."""
    _record_worker_failure(
        "launch",
        tmp_path / "sky-request-id",
        "wait_for_launch",
        RuntimeError(message),
    )
    raw = (tmp_path / "sky-worker-error.json").read_text()
    expect_equal("private-secret" in raw, expected=False)
    expect_equal(
        json.loads(raw),
        {
            "phase": "wait_for_launch",
            "error_type": "RuntimeError",
            "reason": reason,
        },
    )


def test_cleanup_failure_preserves_the_launch_report(tmp_path: pathlib.Path) -> None:
    """A second cleanup exception must not erase the original launch category."""
    request_file = tmp_path / "sky-request-id"
    _record_worker_failure("launch", request_file, "wait_for_launch", ValueError())
    original = (tmp_path / "sky-worker-error.json").read_text()
    _record_worker_failure("cleanup", request_file, "terminate_cluster", RuntimeError())
    expect_equal((tmp_path / "sky-worker-error.json").read_text(), original)
    cleanup = json.loads((tmp_path / "sky-worker-cleanup-error.json").read_text())
    expect_equal(cleanup["phase"], "terminate_cluster")
    expect_equal(cleanup["error_type"], "RuntimeError")


def test_launch_failure_records_phase_and_preserves_exception(
    tmp_path: pathlib.Path,
) -> None:
    """A rejected launch keeps its request ID and useful diagnostics."""
    request_file = tmp_path / "sky-request-id"
    configuration = {
        "action": "launch",
        "endpoint": "https://skypilot.test.invalid",
        "cluster": "mltest-0123456789abcdef",
        "request_file": str(request_file),
        "task": {},
    }
    sky = MagicMock()
    sky.launch.return_value = "request-id"
    failure = RuntimeError(
        "Tailscale auth key creation failed (HTTP 403). private-secret",
    )
    sky.get.side_effect = failure
    with (
        patch.object(gpu_worker, "_sky_client", return_value=sky),
        patch.object(gpu_worker.sys, "stdin", io.StringIO(json.dumps(configuration))),
        pytest.raises(RuntimeError) as error,
    ):
        gpu_worker.main()
    expect_equal(error.value, failure)
    expect_equal(request_file.read_text(), "request-id")
    details = json.loads((tmp_path / "sky-worker-error.json").read_text())
    expect_equal(details["phase"], "wait_for_launch")
    expect_equal(details["reason"], "Tailscale auth key creation failed with HTTP 403.")


def test_failed_cancellation_still_terminates_and_retains_its_phase(
    tmp_path: pathlib.Path,
) -> None:
    """Successful termination must not mislabel a cancellation exception."""
    request_file = tmp_path / "sky-request-id"
    request_file.write_text("request-id")
    configuration = {
        "action": "cleanup",
        "endpoint": "https://skypilot.test.invalid",
        "cluster": "mltest-0123456789abcdef",
        "request_file": str(request_file),
    }
    sky = MagicMock()
    failure = RuntimeError("private-secret")
    sky.get.side_effect = [failure, None]
    with (
        patch.object(gpu_worker, "_sky_client", return_value=sky),
        patch.object(gpu_worker.sys, "stdin", io.StringIO(json.dumps(configuration))),
        pytest.raises(RuntimeError) as error,
    ):
        gpu_worker.main()
    expect_equal(error.value, failure)
    sky.down.assert_called_once_with(configuration["cluster"])
    details = json.loads((tmp_path / "sky-worker-cleanup-error.json").read_text())
    expect_equal(details["phase"], "cancel_request")
