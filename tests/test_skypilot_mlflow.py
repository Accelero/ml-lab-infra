# ruff: noqa: S101, INP001  # assert is idiomatic in pytest
"""SkyPilot MLflow integration test: managed job → MLflow tracking → S3 artifact.

Submits a SkyPilot managed job to the remote API server.  The job:
  1. Provisions a RunPod GPU VM.
  2. Tailscale is auto-injected by the server-side admin policy so the VM
     can reach MLflow without any manual key management.
  3. Runs a dummy training loop that logs params/metrics and uploads a model
     artifact via the MLflow tracking server.
  4. Terminates automatically on completion, avoiding lingering RunPod costs.

URLs are derived from ``pulumi config get tailscale:tailnet``.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest
from mlflow.tracking import MlflowClient

EXPERIMENT_NAME = "skypilot-integration-test"
JOB_NAME = "skypilot-integration-test"
TASK_YAML = pathlib.Path(__file__).parent / "jobs" / "mlflow_train.yaml"
_SKY = shutil.which("sky") or "sky"


def test_skypilot_mlflow_integration(
    monkeypatch: pytest.MonkeyPatch,
    pulumi_config: object,
    mlflow_s3_config: dict[str, str],
) -> None:
    """Managed job runs, logs metrics/params, and uploads artifact to S3."""
    tailnet = pulumi_config("tailscale:tailnet")
    mlflow_tracking_uri = f"https://mlflow.{tailnet}"
    skypilot_endpoint = f"https://skypilot.{tailnet}"
    s3_endpoint = mlflow_s3_config["endpoint"]
    s3_access_key = mlflow_s3_config["access_key"]
    s3_secret_key = mlflow_s3_config["secret_key"]

    env = {**os.environ, "SKYPILOT_API_SERVER_ENDPOINT": skypilot_endpoint}
    result = subprocess.run(  # noqa: S603
        [
            _SKY,
            "jobs",
            "launch",
            "-y",
            "--name",
            JOB_NAME,
            "--env",
            f"MLFLOW_TRACKING_URI={mlflow_tracking_uri}",
            "--env",
            f"AWS_ACCESS_KEY_ID={s3_access_key}",
            "--env",
            f"AWS_SECRET_ACCESS_KEY={s3_secret_key}",
            "--env",
            f"MLFLOW_S3_ENDPOINT_URL={s3_endpoint}",
            str(TASK_YAML),
        ],
        env=env,
        check=False,
        timeout=1800,
    )
    try:
        assert result.returncode == 0, (
            f"sky jobs launch failed (exit {result.returncode})"
        )

        client = MlflowClient(tracking_uri=mlflow_tracking_uri)
        experiment = client.get_experiment_by_name(EXPERIMENT_NAME)
        assert experiment is not None, (
            f"Experiment '{EXPERIMENT_NAME}' not found in MLflow"
            f" at {mlflow_tracking_uri}"
        )
        runs = client.search_runs(
            [experiment.experiment_id],
            order_by=["start_time DESC"],
            max_results=1,
        )
        assert runs, "No runs found in experiment after job completed"
        run = runs[0]

        params = run.data.params
        metrics = run.data.metrics
        assert params.get("lr") == "0.01", f"unexpected lr param: {params}"
        assert params.get("epochs") == "5", f"unexpected epochs param: {params}"
        assert "loss" in metrics, f"loss metric missing: {metrics}"
        assert "accuracy" in metrics, f"accuracy metric missing: {metrics}"
        assert metrics["loss"] == pytest.approx(0.2, abs=1e-6)
        assert metrics["accuracy"] == pytest.approx(0.9, abs=1e-6)

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", s3_access_key)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", s3_secret_key)
        monkeypatch.setenv("MLFLOW_S3_ENDPOINT_URL", s3_endpoint)
        artifacts = client.list_artifacts(run.info.run_id, path="model")
        artifact_paths = [a.path for a in artifacts]
        assert any("model_weights.txt" in p for p in artifact_paths), (
            f"model/model_weights.txt not found in S3 artifacts.\n"
            f"Run ID : {run.info.run_id}\n"
            f"Found  : {artifact_paths}"
        )
    finally:
        subprocess.run(  # noqa: S603
            [_SKY, "jobs", "cancel", "-y", "--name", JOB_NAME],
            env=env,
            check=False,
            timeout=300,
        )
