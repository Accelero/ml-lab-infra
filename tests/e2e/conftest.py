# Copyright (c) 2026 David Schmid
"""Configure bounded application requests and isolated MLflow experiments."""

import uuid
from typing import TYPE_CHECKING

import pytest
from mlflow.tracking import MlflowClient

if TYPE_CHECKING:
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
    return MlflowClient(tracking_uri=f"https://mlflow.{tailnet}")


@pytest.fixture
def experiment_id(mlflow_client: MlflowClient) -> Generator[str]:
    """Give each invocation a unique experiment and delete it even on failure."""
    identifier = mlflow_client.create_experiment(f"acceptance-{uuid.uuid4().hex}")
    try:
        yield identifier
    finally:
        mlflow_client.delete_experiment(identifier)
