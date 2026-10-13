"""Configure bounded application requests and isolated MLflow experiments."""

import asyncio
import uuid
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import yaml
from mlflow.tracking import MlflowClient
from support.experiments import owned_experiment
from support.gpu import run_gpu_job
from support.managed_stack import recreated_stack as stack_recreation
from support.mlflow_probe import RecoveryProbe, wait_for_tracking_server, write_run
from support.process import REPO_ROOT
from support.runpod_watchdog import Lease
from support.stack import SecretValues
from support.storage import Bucket

from resources.kubernetes_client import kubeconfig_client
from resources.postgres_exec import PostgresExec
from resources.skypilot_config import _read_config as read_config

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator

    from support.stack import DevStack


@pytest.fixture
def mlflow_client(
    dev_stack: DevStack,
    mlflow_s3_config: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> MlflowClient:
    """Use the real HTTPS ingress and S3 credentials with short HTTP deadlines."""
    for key, value in {
        "MLFLOW_HTTP_REQUEST_TIMEOUT": "10",
        "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "0",
        "AWS_ACCESS_KEY_ID": mlflow_s3_config["access_key"],
        "AWS_SECRET_ACCESS_KEY": mlflow_s3_config["secret_key"],
        "MLFLOW_S3_ENDPOINT_URL": mlflow_s3_config["endpoint"],
        "AWS_MAX_ATTEMPTS": "1",
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    tailnet = dev_stack.config("tailscale:tailnet")
    endpoint = f"https://mlflow.{tailnet}"
    wait_for_tracking_server(endpoint)
    return MlflowClient(tracking_uri=endpoint)


@pytest.fixture
def experiment_id(
    mlflow_client: MlflowClient,
    dev_stack: DevStack,
) -> Generator[str]:
    """Remove both experiment metadata and its exact S3 artifact directory."""
    bucket = Bucket.from_stack(dev_stack, "mlflow")
    with closing(bucket.client), owned_experiment(mlflow_client, bucket) as identifier:
        yield identifier


@pytest.fixture
def gpu_invocation(
    dev_stack: DevStack,
    mlflow_client: MlflowClient,
    mlflow_s3_config: dict[str, str],
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> str:
    """Return the job ID only after independently verified hardware cleanup."""
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
    asyncio.run(
        run_gpu_job(
            SecretValues(
                {
                    "endpoint": f"https://skypilot.{tailnet}",
                    "api_key": dev_stack.config("runpod:apiKey"),
                    "task": task,
                },
            ),
            Lease(cluster=f"mltest-{invocation[:16]}"),
            tmp_path,
        ),
    )
    return invocation


@pytest.fixture
def persisted_application(
    dev_stack: DevStack,
    mlflow_client: MlflowClient,
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> RecoveryProbe:
    """Write data and close the old Kubernetes client before stack destruction."""
    probe = write_run(mlflow_client, experiment_id, tmp_path)
    with kubeconfig_client(dev_stack.output("hub_kubeconfig")) as client:
        executor = PostgresExec(client)
        identity = executor.query(
            "SELECT (pg_control_system()).system_identifier::text;",
        )
        settings, _raw = read_config(executor)
    policy = settings.get("admin_policy")
    if policy is not None and not isinstance(policy, str):
        msg = "The persisted policy must be a string."
        raise TypeError(msg)
    return RecoveryProbe(probe, identity, policy)


@pytest.fixture
def recreated_stack(
    dev_stack: DevStack,
    persisted_application: RecoveryProbe,
) -> Generator[DevStack]:
    """Recreate only after the probe exists; recover failed setup before yielding."""
    if not persisted_application.system_identifier:
        pytest.fail("The recovery probe has no database identity.")
    with stack_recreation(dev_stack) as restored:
        yield restored
