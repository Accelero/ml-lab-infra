"""Write and read application data through the MLflow client API."""

import math
import pathlib
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from mlflow.tracking import MlflowClient


def wait_for_tracking_server(endpoint: str, *, timeout: float = 120) -> None:
    """Wait for new HTTPS ingress before creating experiment metadata."""
    if not math.isfinite(timeout) or timeout <= 0:
        msg = "MLflow readiness requires a finite positive timeout."
        raise ValueError(msg)
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            msg = "MLflow HTTPS ingress did not become ready before its deadline."
            raise TimeoutError(msg)
        try:
            with requests.get(
                f"{endpoint.rstrip('/')}/health",
                timeout=min(10, remaining),
                allow_redirects=False,
            ) as response:
                if response.status_code == requests.codes.ok:
                    return
                if (
                    response.status_code < requests.codes.server_error
                    and response.status_code != requests.codes.not_found
                ):
                    msg = (
                        f"MLflow HTTPS readiness failed (HTTP {response.status_code})."
                    )
                    raise RuntimeError(msg)
        except requests.exceptions.ConnectionError, requests.exceptions.Timeout:
            pass
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))


@dataclass(frozen=True)
class RunProbe:
    """Remember an exact run ID and expected artifact bytes."""

    run_id: str
    content: str


@dataclass(frozen=True)
class RecoveryProbe:
    """Keep the application data and database identity across stack recreation."""

    run: RunProbe
    system_identifier: str
    policy: str | None


def write_run(
    client: MlflowClient,
    experiment: str,
    directory: pathlib.Path,
) -> RunProbe:
    """Log metric history and a small unique artifact, marking failures explicitly."""
    run = client.create_run(experiment)
    identifier = run.info.run_id
    content = uuid.uuid4().hex
    artifact = directory / "probe.txt"
    artifact.write_text(content)
    try:
        client.log_param(identifier, "test_value", content)
        for step in range(3):
            client.log_metric(identifier, "loss", 1 / (step + 1), step=step)
        client.log_artifact(identifier, str(artifact), artifact_path="probe")
        client.set_terminated(identifier, status="FINISHED")
    except BaseException:
        client.set_terminated(identifier, status="FAILED")
        raise
    return RunProbe(identifier, content)


def verify_run(client: MlflowClient, probe: RunProbe, directory: pathlib.Path) -> None:
    """Verify status, parameter, every metric step, and downloaded artifact contents."""
    run = client.get_run(probe.run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.params.get("test_value") != probe.content
    ):
        msg = "MLflow did not preserve the exact test run."
        raise RuntimeError(msg)
    history = client.get_metric_history(probe.run_id, "loss")
    if sorted((entry.step, entry.value) for entry in history) != [
        (step, 1 / (step + 1)) for step in range(3)
    ]:
        msg = "MLflow did not preserve metric history."
        raise RuntimeError(msg)
    directory.mkdir(exist_ok=True)
    artifact = pathlib.Path(
        client.download_artifacts(
            probe.run_id,
            "probe/probe.txt",
            dst_path=str(directory),
        ),
    )
    if artifact.read_text() != probe.content:
        msg = "MLflow artifact contents did not match."
        raise RuntimeError(msg)
