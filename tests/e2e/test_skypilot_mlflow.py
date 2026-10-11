# Copyright (c) 2026 David Schmid
"""Draft one-node GPU acceptance with direct RunPod termination verification."""

import asyncio
import pathlib
import uuid
from typing import TYPE_CHECKING

import pytest
import yaml
from support.gpu import run_gpu_job
from support.process import REPO_ROOT
from support.runpod_watchdog import Lease
from support.stack import SecretValues

if TYPE_CHECKING:
    from mlflow.tracking import MlflowClient
    from support.stack import DevStack

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e,
    pytest.mark.gpu,
    pytest.mark.timeout(900),
]


def test_gpu_logs_exact_mlflow_run_and_artifact(
    dev_stack: DevStack,
    mlflow_client: MlflowClient,
    mlflow_s3_config: dict[str, str],
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> None:
    """Exercise provisioning, CUDA, injected Tailscale, MLflow, S3, and cleanup."""
    invocation = uuid.uuid4().hex
    experiment = mlflow_client.get_experiment(experiment_id)
    task = yaml.safe_load((REPO_ROOT / "tests/jobs/mlflow_train.yaml").read_text())
    tailnet = dev_stack.config("tailscale:tailnet")
    task["envs"].update(
        {
            "TEST_RUN_ID": invocation,
            "TEST_EXPERIMENT_NAME": experiment.name,
            "MLFLOW_TRACKING_URI": f"https://mlflow.{tailnet}",
            "MLFLOW_HTTP_REQUEST_TIMEOUT": "10",
            "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0",
            "AWS_ACCESS_KEY_ID": mlflow_s3_config["access_key"],
            "AWS_SECRET_ACCESS_KEY": mlflow_s3_config["secret_key"],
            "MLFLOW_S3_ENDPOINT_URL": mlflow_s3_config["endpoint"],
            "AWS_MAX_ATTEMPTS": "1",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
    )
    lease = Lease(cluster=f"mltest-{invocation[:16]}")
    asyncio.run(
        run_gpu_job(
            SecretValues(
                {
                    "endpoint": f"https://skypilot.{tailnet}",
                    "api_key": dev_stack.config("runpod:apiKey"),
                    "task": task,
                },
            ),
            lease,
            tmp_path,
        ),
    )
    runs = mlflow_client.search_runs(
        [experiment_id],
        filter_string=f"tags.test_invocation = '{invocation}'",
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
