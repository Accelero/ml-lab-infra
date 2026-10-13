"""Run bounded SkyPilot operations in a child process with secrets on stdin."""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import re
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_ERROR_REASONS = {
    "NoCloudAccessError": "SkyPilot has no enabled cloud with usable credentials.",
    "ResourcesUnavailableError": "No resources satisfied the GPU test constraints.",
    "APINotSupportedError": "The SkyPilot server does not support this client request.",
    "ClusterDoesNotExist": "SkyPilot has no cluster for this test invocation.",
    "TimeoutError": "The worker operation exceeded its deadline.",
}
_MESSAGE_REASONS = (
    (
        ("tailscale_oauth_client_id", "tailscale_oauth_client_secret", "not set"),
        "The admin policy is missing its Tailscale OAuth environment variables.",
    ),
    (
        ("skypilot_policies", "no module named"),
        "The configured admin policy module could not be imported.",
    ),
    (
        ("skypilot_policies", "failed to import"),
        "The configured admin policy module could not be imported.",
    ),
    (
        ("insufficient", "balance"),
        "The provider reported insufficient account balance.",
    ),
    (
        ("insufficient", "credits"),
        "The provider reported insufficient account balance.",
    ),
)


def _failure_details(error: BaseException) -> dict[str, str]:
    """Classify known failures without retaining raw error text or request data."""
    error_type = type(error).__name__
    reason = _ERROR_REASONS.get(error_type, "Unclassified worker failure.")
    message = str(error)[:8192]
    for fragments, description in _MESSAGE_REASONS:
        if all(fragment in message.lower() for fragment in fragments):
            reason = description
            break
    match = re.search(
        r"Tailscale (OAuth token exchange|auth key creation) failed \(HTTP (\d{3})\)",
        message,
    )
    if match:
        reason = f"Tailscale {match[1]} failed with HTTP {match[2]}."
    return {"error_type": error_type, "reason": reason}


def _record_worker_failure(
    action: str,
    request_file: pathlib.Path,
    phase: str,
    error: BaseException,
) -> None:
    """Keep separate safe summaries for launch and cleanup failures."""
    filename = (
        "sky-worker-cleanup-error.json"
        if action == "cleanup"
        else "sky-worker-error.json"
    )
    report = request_file.with_name(filename)
    temporary = report.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps({"phase": phase, **_failure_details(error)}))
        temporary.replace(report)
    except OSError:
        sys.stderr.write("The GPU worker failure summary could not be written.\n")


def _sky_client(endpoint: str) -> ModuleType:
    """Set the remote endpoint before SkyPilot caches it during import."""
    os.environ["SKYPILOT_API_SERVER_ENDPOINT"] = endpoint
    return importlib.import_module("sky")


def _wait_for_job(sky: ModuleType, cluster: str, job_id: int) -> None:
    """Wait for success while preserving terminal job failures."""
    while True:
        statuses = sky.get(sky.job_status(cluster, job_ids=[job_id]))
        status = statuses.get(job_id)
        if status == sky.JobStatus.SUCCEEDED:
            return
        if status is not None and status.is_terminal():
            msg = "The GPU acceptance job did not succeed."
            raise RuntimeError(msg)
        time.sleep(2)


def main() -> None:
    """Persist the launch request ID before waiting, enabling precise cancellation."""
    configuration = json.load(sys.stdin)
    request_file = pathlib.Path(configuration["request_file"])
    cluster = configuration["cluster"]
    phase = "initialize_client"
    try:
        sky = _sky_client(configuration["endpoint"])
        if configuration["action"] == "cleanup":
            try:
                phase = "cancel_request"
                if request_file.exists():
                    request = request_file.read_text()
                    sky.get(sky.api_cancel(request_ids=[request], silent=True))
            finally:
                previous_phase = phase
                phase = "terminate_cluster"
                sky.get(sky.down(cluster))
                phase = previous_phase
            return
        phase = "parse_task"
        task = sky.Task.from_yaml_config(configuration["task"])
        phase = "submit_launch"
        request = sky.launch(
            task,
            cluster_name=cluster,
            retry_until_up=False,
            idle_minutes_to_autostop=1,
            down=True,
        )
        request_file.write_text(str(request))
        phase = "wait_for_launch"
        job_id, _handle = sky.get(request)
        phase = "wait_for_job"
        _wait_for_job(sky, cluster, job_id)
    except BaseException as error:
        _record_worker_failure(
            configuration["action"],
            request_file,
            phase,
            error,
        )
        raise


if __name__ == "__main__":
    main()
