# Copyright (c) 2026 David Schmid
"""Draft one-node GPU acceptance with direct RunPod termination verification."""

import pathlib
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from mlflow.tracking import MlflowClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e,
    pytest.mark.gpu,
    pytest.mark.timeout(900),
]


def test_gpu_logs_exact_mlflow_run_and_artifact(
    gpu_invocation: str,
    mlflow_client: MlflowClient,
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> None:
    """Exercise provisioning, CUDA, injected Tailscale, MLflow, S3, and cleanup."""
    runs = mlflow_client.search_runs(
        [experiment_id],
        filter_string=f"tags.test_invocation = '{gpu_invocation}'",
    )
    if len(runs) != 1 or runs[0].info.status != "FINISHED":
        pytest.fail("The exact GPU test invocation did not finish in MLflow.")
    run = runs[0]
    if (
        run.data.params != {"lr": "0.01", "epochs": "5"}
        or run.data.metrics.get("gpu_count") != 1
        or run.data.metrics.get("loss") != pytest.approx(0.2)
        or run.data.metrics.get("accuracy") != pytest.approx(0.9)
    ):
        pytest.fail("The GPU test run did not contain the expected data.")
    artifact = pathlib.Path(
        mlflow_client.download_artifacts(
            run.info.run_id,
            "model/model_weights.txt",
            dst_path=str(tmp_path),
        ),
    )
    if artifact.read_text() != "weights: 0.42\nbias: 0.1\n":
        pytest.fail("The downloaded GPU artifact did not match.")
