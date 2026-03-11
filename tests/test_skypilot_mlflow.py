# ruff: noqa: S101  # assert is idiomatic in pytest
"""Integration test: SkyPilot job → MLflow tracking → S3 artifact.

Submits a SkyPilot task to the remote API server.  The task:
  1. Provisions a RunPod GPU VM.
  2. Tailscale is auto-injected by the server-side admin policy so the VM
     can reach MLflow without any manual key management.
  3. Runs a dummy training loop that logs params/metrics and uploads a model
     artifact via the MLflow tracking server.

After the job completes the test queries MLflow to assert that:
  - The run exists with the expected params and metrics.
  - The model artifact is stored in the MLflow-managed S3 bucket.

URLs are derived from ``pulumi config get tailscale:tailnet``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from mlflow.tracking import MlflowClient


def _pulumi_config(key: str, *, secret: bool = False) -> str:
    cmd = [shutil.which("pulumi") or "pulumi", "config", "get", key]
    if secret:
        cmd.append("--show-secrets")
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=pathlib.Path(__file__).parent.parent,
    )
    return result.stdout.strip()


_TAILNET = _pulumi_config("tailscale:tailnet")
_MLFLOW_S3_ENDPOINT = _pulumi_config("mlflow:s3Endpoint")
_MLFLOW_S3_ACCESS_KEY = _pulumi_config("mlflow:s3AccessKey")
_MLFLOW_S3_SECRET_KEY = _pulumi_config("mlflow:s3SecretKey")


if TYPE_CHECKING:
    from collections.abc import Generator

    from mlflow.entities import Run

MLFLOW_TRACKING_URI: str = f"https://mlflow-test.{_TAILNET}"
SKYPILOT_API_SERVER_ENDPOINT: str = f"https://skypilot-test.{_TAILNET}"

EXPERIMENT_NAME = "skypilot-integration-test"
CLUSTER_NAME = "mlflow-test"
TASK_YAML = pathlib.Path(__file__).parent / "jobs" / "mlflow_train.yaml"
_SKY = shutil.which("sky") or "sky"


def _sky_env() -> dict[str, str]:
    """Return os.environ extended with the SkyPilot API server endpoint."""
    return {**os.environ, "SKYPILOT_API_SERVER_ENDPOINT": SKYPILOT_API_SERVER_ENDPOINT}


@pytest.fixture(scope="module")
def launched_cluster() -> Generator[subprocess.CompletedProcess[str]]:
    """Submit the SkyPilot job, yield the CompletedProcess, then tear down.

    ``sky launch`` (without ``--detach-run``) blocks until the job finishes,
    so the fixture returns only after the remote training script has exited.
    The cluster is always torn down in the finaliser, even on failure.
    """
    env = _sky_env()
    result = subprocess.run(  # noqa: S603
        [
            _SKY,
            "launch",
            "-y",
            "--cluster",
            CLUSTER_NAME,
            "--env",
            f"MLFLOW_TRACKING_URI={MLFLOW_TRACKING_URI}",
            "--env",
            f"AWS_ACCESS_KEY_ID={_MLFLOW_S3_ACCESS_KEY}",
            "--env",
            f"AWS_SECRET_ACCESS_KEY={_MLFLOW_S3_SECRET_KEY}",
            "--env",
            f"MLFLOW_S3_ENDPOINT_URL={_MLFLOW_S3_ENDPOINT}",
            str(TASK_YAML),
        ],
        env=env,
        check=False,
        timeout=1800,
    )
    try:
        yield result
    finally:
        subprocess.run(  # noqa: S603
            [_SKY, "down", "-y", CLUSTER_NAME],
            env=env,
            check=False,
            timeout=300,
        )


@pytest.fixture(scope="module")
def mlflow_run(launched_cluster: subprocess.CompletedProcess[str]) -> Run:  # noqa: ARG001
    """Return the most-recent MLflow run from the integration-test experiment."""
    client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
    experiment = client.get_experiment_by_name(EXPERIMENT_NAME)
    assert experiment is not None, (
        f"Experiment '{EXPERIMENT_NAME}' not found in MLflow at {MLFLOW_TRACKING_URI}"
    )
    runs = client.search_runs(
        [experiment.experiment_id],
        order_by=["start_time DESC"],
        max_results=1,
    )
    assert runs, "No runs found in experiment after job completed"
    return runs[0]


def test_job_succeeds(launched_cluster: subprocess.CompletedProcess[str]) -> None:
    """The sky launch command must exit 0."""
    assert launched_cluster.returncode == 0, (
        f"sky launch failed (exit {launched_cluster.returncode})"
    )


def test_mlflow_run_has_params_and_metrics(mlflow_run: Run) -> None:
    """Logged hyperparams and loss/accuracy metrics must be present."""
    params = mlflow_run.data.params
    metrics = mlflow_run.data.metrics

    assert params.get("lr") == "0.01", f"unexpected lr param: {params}"
    assert params.get("epochs") == "5", f"unexpected epochs param: {params}"
    assert "loss" in metrics, f"loss metric missing: {metrics}"
    assert "accuracy" in metrics, f"accuracy metric missing: {metrics}"

    # Sanity-check final values (step 4 → loss=0.2, accuracy=0.9)
    assert metrics["loss"] == pytest.approx(0.2, abs=1e-6)
    assert metrics["accuracy"] == pytest.approx(0.9, abs=1e-6)


def test_mlflow_artifact_stored_in_s3(mlflow_run: Run) -> None:
    """model/model_weights.txt must be listed under the run's artifact store (S3)."""
    client = MlflowClient(tracking_uri=MLFLOW_TRACKING_URI)
    artifacts = client.list_artifacts(mlflow_run.info.run_id, path="model")
    artifact_paths = [a.path for a in artifacts]

    assert any("model_weights.txt" in p for p in artifact_paths), (
        f"model/model_weights.txt not found in S3 artifacts.\n"
        f"Run ID : {mlflow_run.info.run_id}\n"
        f"Found  : {artifact_paths}"
    )
