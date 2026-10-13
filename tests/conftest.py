# ruff: noqa: INP001  # tests/ is not a package
"""Gate external operations and share development-stack fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from support.backups import BackupProbe, ClusterExec, backup_probe, restored_cluster
from support.stack import DevStack, SecretValues

from resources.kubernetes_client import kubeconfig_client
from resources.postgres_exec import PostgresExec
from resources.skypilot_config import _change_policy as change_skypilot_policy
from resources.skypilot_config import _read_config as read_skypilot_config

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    import kubernetes.client


pytest_plugins = ["support.stack_fixtures"]


@pytest.fixture(scope="session")
def pulumi_config(dev_stack: DevStack) -> Callable[[str], str]:
    """Read a Pulumi configuration value."""
    return dev_stack.config


@pytest.fixture
def k8s_client(dev_stack: DevStack) -> Generator[kubernetes.client.ApiClient]:
    """Connect using the stack's in-memory kubeconfig."""
    with kubeconfig_client(dev_stack.output("hub_kubeconfig")) as client:
        yield client


@pytest.fixture(scope="module")
def mlflow_s3_config(pulumi_config: Callable[[str], str]) -> dict[str, str]:
    """Fetch MLflow S3 credentials from Pulumi config at test runtime."""
    return SecretValues(
        {
            "endpoint": pulumi_config("mlflow:s3Endpoint"),
            "access_key": pulumi_config("mlflow:s3AccessKey"),
            "secret_key": pulumi_config("mlflow:s3SecretKey"),
        },
    )


@pytest.fixture(scope="module")
def backup_s3_config(pulumi_config: Callable[[str], str]) -> dict[str, str]:
    """Fetch S3 credentials from Pulumi config at test runtime, not collection time."""
    return SecretValues(
        {
            "endpoint": pulumi_config("backup:s3Endpoint"),
            "bucket": pulumi_config("backup:s3BucketName"),
            "access_key": pulumi_config("backup:s3AccessKey"),
            "secret_key": pulumi_config("backup:s3SecretKey"),
        },
    )


@pytest.fixture
def sentinel_and_backup(
    k8s_client: kubernetes.client.ApiClient,
    backup_s3_config: dict[str, str],
) -> Generator[BackupProbe]:
    """Verify the base backup and post-backup WAL before yielding."""
    with backup_probe(k8s_client, backup_s3_config) as probe:
        yield probe


@pytest.fixture
def recovery_cluster(
    k8s_client: kubernetes.client.ApiClient,
    backup_s3_config: dict[str, str],
    sentinel_and_backup: BackupProbe,
) -> Generator[ClusterExec]:
    """Recover the exact test backup and release resources even on setup failure."""
    with restored_cluster(
        k8s_client,
        backup_s3_config,
        sentinel_and_backup,
    ) as executor:
        yield executor


@pytest.fixture
def original_skypilot_config(
    k8s_client: kubernetes.client.ApiClient,
) -> Generator[dict[str, object]]:
    """Snapshot settings and restore only the policy after the test."""
    executor = PostgresExec(k8s_client)
    snapshot, _value = read_skypilot_config(executor)
    policy = snapshot.get("admin_policy")
    if "admin_policy" in snapshot and not isinstance(policy, str):
        msg = "The original SkyPilot admin policy must be a string."
        raise TypeError(msg)
    try:
        yield SecretValues(snapshot)
    finally:
        change_skypilot_policy(executor, policy)
