# ruff: noqa: S101  # assert is idiomatic in pytest
"""Integration test: PostgreSQL backup and restore via CloudNativePG + S3.

Steps:
1. Write a sentinel row into mlflow_db via psql exec inside the primary pod.
2. Trigger an on-demand backup via a CNPG Backup CR.
3. Poll the Backup CR until status.phase == "completed".
4. Verify backup objects exist in S3 via boto3.
5. Create a temporary recovery cluster (postgres-restore-test) from the backup.
6. Poll the recovery cluster until status.readyInstances > 0.
7. Exec into the recovery pod and verify the sentinel row survived.
8. Teardown: drop sentinel table, delete recovery cluster, delete Backup CR.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import boto3
import botocore.config
import kubernetes
import kubernetes.client
import kubernetes.config
import pytest
import yaml
from kubernetes.stream import stream

if TYPE_CHECKING:
    from collections.abc import Generator

_NAMESPACE = "infra"
_PRIMARY_CLUSTER = "postgres"
_RESTORE_CLUSTER = "postgres-restore-test"
_PRIMARY_POD = "postgres-1"
_RESTORE_POD = f"{_RESTORE_CLUSTER}-1"
_BACKUP_CR_NAME = "backup-restore-test"
_POLL_INTERVAL = 10
_BACKUP_TIMEOUT = 600
_RESTORE_TIMEOUT = 600
_CNPG_GROUP = "postgresql.cnpg.io"
_CNPG_VERSION = "v1"

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _pulumi_config(key: str, *, secret: bool = False) -> str:  # noqa: ARG001
    cmd = [shutil.which("pulumi") or "pulumi", "config", "get", key]
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


def _pulumi_stack_output(key: str) -> str:
    result = subprocess.run(  # noqa: S603
        [shutil.which("pulumi") or "pulumi", "stack", "output", key, "--show-secrets"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


def _build_k8s_client() -> kubernetes.client.ApiClient:
    raw = _pulumi_stack_output("hub_kubeconfig")
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(raw),
        client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def s3_config() -> dict[str, str]:
    """Fetch S3 credentials from Pulumi config at test runtime, not collection time."""
    return {
        "endpoint": _pulumi_config("backup:s3Endpoint"),
        "bucket": _pulumi_config("backup:s3BucketName"),
        "access_key": _pulumi_config("backup:s3AccessKey", secret=True),
        "secret_key": _pulumi_config("backup:s3SecretKey", secret=True),
    }


@pytest.fixture(scope="module")
def k8s_client() -> kubernetes.client.ApiClient:
    """Build a kubernetes ApiClient from the Pulumi stack kubeconfig (in memory)."""
    return _build_k8s_client()


@pytest.fixture(scope="module")
def sentinel_and_backup(
    k8s_client: kubernetes.client.ApiClient,
) -> Generator[tuple[str, str]]:
    """Write sentinel data, trigger backup, wait for completion.

    Yields (backup_cr_name, sentinel_uuid).
    Teardown: drop sentinel table, delete Backup CR.
    """
    core_api = kubernetes.client.CoreV1Api(k8s_client)
    custom_api = kubernetes.client.CustomObjectsApi(k8s_client)

    sentinel_uuid = str(uuid.uuid4())

    # Write sentinel row into mlflow_db on the primary cluster pod.
    # UUID is hex+dashes only — no SQL injection risk.
    create_sentinel = "CREATE TABLE IF NOT EXISTS backup_restore_sentinel (val TEXT);"
    insert_sentinel = f"INSERT INTO backup_restore_sentinel VALUES ('{sentinel_uuid}');"  # noqa: S608
    write_sql = create_sentinel + insert_sentinel
    stream(
        core_api.connect_get_namespaced_pod_exec,
        _PRIMARY_POD,
        _NAMESPACE,
        command=["psql", "-U", "postgres", "-d", "mlflow_db", "-c", write_sql],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )

    # Create the on-demand Backup CR.
    backup_manifest = {
        "apiVersion": f"{_CNPG_GROUP}/{_CNPG_VERSION}",
        "kind": "Backup",
        "metadata": {"name": _BACKUP_CR_NAME, "namespace": _NAMESPACE},
        "spec": {
            "method": "barmanObjectStore",
            "cluster": {"name": _PRIMARY_CLUSTER},
        },
    }
    custom_api.create_namespaced_custom_object(
        _CNPG_GROUP,
        _CNPG_VERSION,
        _NAMESPACE,
        "backups",
        backup_manifest,
    )

    # Poll until backup completes.
    deadline = time.monotonic() + _BACKUP_TIMEOUT
    completed = False
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        obj = custom_api.get_namespaced_custom_object(
            _CNPG_GROUP,
            _CNPG_VERSION,
            _NAMESPACE,
            "backups",
            _BACKUP_CR_NAME,
        )
        phase = obj.get("status", {}).get("phase")
        if phase == "completed":
            completed = True
            break
        if phase == "failed":
            msg = f"Backup CR entered failed phase: {obj.get('status', {})}"
            raise RuntimeError(msg)

    assert completed, f"Backup did not complete within {_BACKUP_TIMEOUT}s"

    try:
        yield _BACKUP_CR_NAME, sentinel_uuid
    finally:
        # Drop sentinel table from the primary cluster.
        stream(
            core_api.connect_get_namespaced_pod_exec,
            _PRIMARY_POD,
            _NAMESPACE,
            command=[
                "psql",
                "-U",
                "postgres",
                "-d",
                "mlflow_db",
                "-c",
                "DROP TABLE IF EXISTS backup_restore_sentinel;",
            ],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        # Delete Backup CR.
        try:
            custom_api.delete_namespaced_custom_object(
                _CNPG_GROUP,
                _CNPG_VERSION,
                _NAMESPACE,
                "backups",
                _BACKUP_CR_NAME,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != 404:  # noqa: PLR2004
                raise


@pytest.fixture(scope="module")
def recovery_cluster(
    k8s_client: kubernetes.client.ApiClient,
    s3_config: dict[str, str],
    sentinel_and_backup: tuple[str, str],  # noqa: ARG001  # ordering dependency
) -> Generator[str]:
    """Create postgres-restore-test recovery cluster, yield pod name, teardown."""
    custom_api = kubernetes.client.CustomObjectsApi(k8s_client)
    core_api = kubernetes.client.CoreV1Api(k8s_client)

    cluster_manifest = {
        "apiVersion": f"{_CNPG_GROUP}/{_CNPG_VERSION}",
        "kind": "Cluster",
        "metadata": {"name": _RESTORE_CLUSTER, "namespace": _NAMESPACE},
        "spec": {
            "instances": 1,
            "bootstrap": {"recovery": {"source": "backup-source"}},
            "storage": {"size": "10Gi"},
            "externalClusters": [
                {
                    "name": "backup-source",
                    "barmanObjectStore": {
                        "destinationPath": f"s3://{s3_config['bucket']}",
                        "endpointURL": s3_config["endpoint"],
                        "serverName": _PRIMARY_CLUSTER,
                        "s3Credentials": {
                            "accessKeyId": {
                                "name": "postgres-backup-credentials",
                                "key": "ACCESS_KEY_ID",
                            },
                            "secretAccessKey": {
                                "name": "postgres-backup-credentials",
                                "key": "ACCESS_SECRET_KEY",
                            },
                        },
                    },
                },
            ],
        },
    }

    custom_api.create_namespaced_custom_object(
        _CNPG_GROUP,
        _CNPG_VERSION,
        _NAMESPACE,
        "clusters",
        cluster_manifest,
    )

    # Poll until recovery cluster is ready.
    deadline = time.monotonic() + _RESTORE_TIMEOUT
    ready = False
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        obj = custom_api.get_namespaced_custom_object(
            _CNPG_GROUP,
            _CNPG_VERSION,
            _NAMESPACE,
            "clusters",
            _RESTORE_CLUSTER,
        )
        if obj.get("status", {}).get("readyInstances", 0) > 0:
            ready = True
            break

    assert ready, f"Recovery cluster not ready within {_RESTORE_TIMEOUT}s"

    try:
        yield _RESTORE_POD
    finally:
        try:
            custom_api.delete_namespaced_custom_object(
                _CNPG_GROUP,
                _CNPG_VERSION,
                _NAMESPACE,
                "clusters",
                _RESTORE_CLUSTER,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != 404:  # noqa: PLR2004
                raise

        # Wait for pod to terminate before returning.
        deadline = time.monotonic() + _RESTORE_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL)
            pods = core_api.list_namespaced_pod(
                _NAMESPACE,
                label_selector=f"cnpg.io/cluster={_RESTORE_CLUSTER}",
            )
            if not pods.items:
                break


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_backup_completed(sentinel_and_backup: tuple[str, str]) -> None:
    """Backup CR must reach completed phase before the fixture yields."""
    backup_name, _ = sentinel_and_backup
    assert backup_name, "Backup did not complete"


def test_backup_objects_in_s3(
    s3_config: dict[str, str],
    sentinel_and_backup: tuple[str, str],
) -> None:
    """Base backup objects must be present in S3 under postgres/base/."""
    backup_name, _ = sentinel_and_backup
    s3 = boto3.client(
        "s3",
        endpoint_url=s3_config["endpoint"],
        aws_access_key_id=s3_config["access_key"],
        aws_secret_access_key=s3_config["secret_key"],
        config=botocore.config.Config(signature_version="s3v4"),
    )
    resp = s3.list_objects_v2(
        Bucket=s3_config["bucket"],
        Prefix=f"{_PRIMARY_CLUSTER}/base/",
        MaxKeys=1,
    )
    assert resp.get("KeyCount", 0) > 0, (
        f"No backup objects found under"
        f" s3://{s3_config['bucket']}/{_PRIMARY_CLUSTER}/base/\n"
        f"Backup name: {backup_name}"
    )


def test_restore_contains_sentinel_data(
    k8s_client: kubernetes.client.ApiClient,
    recovery_cluster: str,
    sentinel_and_backup: tuple[str, str],
) -> None:
    """The sentinel row written before the backup must exist in the recovery cluster."""
    _, sentinel_uuid = sentinel_and_backup
    core_api = kubernetes.client.CoreV1Api(k8s_client)

    # UUID is hex+dashes only — no SQL injection risk.
    select_sql = (
        f"SELECT val FROM backup_restore_sentinel WHERE val = '{sentinel_uuid}';"  # noqa: S608
    )
    output = stream(
        core_api.connect_get_namespaced_pod_exec,
        recovery_cluster,
        _NAMESPACE,
        command=[
            "psql",
            "-U",
            "postgres",
            "-d",
            "mlflow_db",
            "-t",
            "-A",
            "-c",
            select_sql,
        ],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )

    assert sentinel_uuid in output, (
        f"Sentinel UUID '{sentinel_uuid}' not found in recovery cluster.\n"
        f"psql output: {output!r}"
    )
