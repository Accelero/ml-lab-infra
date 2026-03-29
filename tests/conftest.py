# ruff: noqa: INP001  # tests/ is not a package
"""Shared fixtures for all integration tests."""

from __future__ import annotations

import copy
import pathlib
import shutil
import subprocess
import time
import uuid
from typing import TYPE_CHECKING

import kubernetes
import kubernetes.client
import kubernetes.config
import pytest
import yaml
from kubernetes.stream import stream
from psycopg2.extensions import adapt as _pg_adapt

if TYPE_CHECKING:
    from collections.abc import Generator

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_NAMESPACE = "infra"
_PRIMARY_CLUSTER = "postgres"
_PRIMARY_POD = "postgres-1"
_CNPG_GROUP = "postgresql.cnpg.io"
_CNPG_VERSION = "v1"
_RESTORE_CLUSTER = "postgres-restore-test"
_RESTORE_POD = f"{_RESTORE_CLUSTER}-1"
_BACKUP_CR_NAME = "backup-restore-test"
_POLL_INTERVAL = 10
_BACKUP_TIMEOUT = 600
_RESTORE_TIMEOUT = 600
_SKYPILOT_DB = "skypilot_db"
_CONFIG_KEY = "api_server_config"

# ── Private helpers ───────────────────────────────────────────────────────────


def _pulumi_stack_output(key: str) -> str:
    result = subprocess.run(  # noqa: S603
        [shutil.which("pulumi") or "pulumi", "stack", "output", key, "--show-secrets"],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
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


def _psql_exec(core_api: kubernetes.client.CoreV1Api, sql: str) -> str:
    return stream(
        core_api.connect_get_namespaced_pod_exec,
        _PRIMARY_POD,
        _NAMESPACE,
        command=["psql", "-U", "postgres", "-d", _SKYPILOT_DB, "-t", "-A", "-c", sql],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _pg_quote(value: str) -> str:
    q = _pg_adapt(value)
    q.encoding = "utf-8"
    return q.getquoted().decode()


def _read_skypilot_config(core_api: kubernetes.client.CoreV1Api) -> dict:
    sql = f"SELECT value FROM config_yaml WHERE key = '{_CONFIG_KEY}';"  # noqa: S608
    result = _psql_exec(core_api, sql).strip()
    if not result:
        return {}
    return yaml.safe_load(result) or {}


def _write_skypilot_config(
    core_api: kubernetes.client.CoreV1Api,
    config: dict,
) -> None:
    yaml_str = yaml.dump(config, default_flow_style=False)
    key_q, val_q = _pg_quote(_CONFIG_KEY), _pg_quote(yaml_str)
    sql = (
        f"INSERT INTO config_yaml (key, value) VALUES ({key_q}, {val_q}) "  # noqa: S608
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    )
    _psql_exec(core_api, sql)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pulumi_config() -> object:
    """Return a callable ``fn(key) -> str`` for pulumi config."""

    def _get(key: str) -> str:
        cmd = [shutil.which("pulumi") or "pulumi", "config", "get", key]
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=_REPO_ROOT,
        )
        return result.stdout.strip()

    return _get


@pytest.fixture(scope="module")
def k8s_client() -> kubernetes.client.ApiClient:
    """Kubernetes ApiClient from the Pulumi stack kubeconfig (in memory)."""
    return _build_k8s_client()


@pytest.fixture(scope="module")
def mlflow_s3_config(pulumi_config: object) -> dict[str, str]:
    """Fetch MLflow S3 credentials from Pulumi config at test runtime."""
    return {
        "endpoint": pulumi_config("mlflow:s3Endpoint"),
        "access_key": pulumi_config("mlflow:s3AccessKey"),
        "secret_key": pulumi_config("mlflow:s3SecretKey"),
    }


@pytest.fixture(scope="module")
def backup_s3_config(pulumi_config: object) -> dict[str, str]:
    """Fetch S3 credentials from Pulumi config at test runtime, not collection time."""
    return {
        "endpoint": pulumi_config("backup:s3Endpoint"),
        "bucket": pulumi_config("backup:s3BucketName"),
        "access_key": pulumi_config("backup:s3AccessKey"),
        "secret_key": pulumi_config("backup:s3SecretKey"),
    }


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

    # UUID is hex+dashes only — no SQL injection risk.
    create_sentinel = "CREATE TABLE IF NOT EXISTS backup_restore_sentinel (val TEXT);"
    insert_sentinel = f"INSERT INTO backup_restore_sentinel VALUES ('{sentinel_uuid}');"  # noqa: S608
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
            create_sentinel + insert_sentinel,
        ],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )

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

    assert completed, f"Backup did not complete within {_BACKUP_TIMEOUT}s"  # noqa: S101

    yield _BACKUP_CR_NAME, sentinel_uuid

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
    backup_s3_config: dict[str, str],
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
                        "destinationPath": f"s3://{backup_s3_config['bucket']}",
                        "endpointURL": backup_s3_config["endpoint"],
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

    assert ready, f"Recovery cluster not ready within {_RESTORE_TIMEOUT}s"  # noqa: S101

    yield _RESTORE_POD

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

    deadline = time.monotonic() + _RESTORE_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        pods = core_api.list_namespaced_pod(
            _NAMESPACE,
            label_selector=f"cnpg.io/cluster={_RESTORE_CLUSTER}",
        )
        if not pods.items:
            break


@pytest.fixture(scope="module")
def original_skypilot_config(
    k8s_client: kubernetes.client.ApiClient,
) -> Generator[dict]:
    """Snapshot the current config_yaml row and restore it on teardown."""
    core_api = kubernetes.client.CoreV1Api(k8s_client)
    snapshot = copy.deepcopy(_read_skypilot_config(core_api))

    yield snapshot

    _write_skypilot_config(core_api, snapshot)
