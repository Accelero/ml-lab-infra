# Copyright (c) 2026 David Schmid
"""Create uniquely owned backup probes and clean up on every exit path."""

import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import kubernetes.client
from kubernetes.client.exceptions import ApiException

from resources.postgres_archive import ArchiveTarget, PostgresArchive
from resources.postgres_exec import PostgresExec
from resources.postgres_lifecycle import PostgresLifecycle, wait_for_archive

if TYPE_CHECKING:
    from collections.abc import Generator

GROUP = "postgresql.cnpg.io"
VERSION = "v1"
NAMESPACE = "infra"
REQUEST_TIMEOUT = (5, 30)
TIMEOUT = 600
POLL_INTERVAL = 2
NOT_FOUND = 404


class ClusterExec(PostgresExec):
    """Discover the current primary of a named temporary recovery cluster."""

    def __init__(self, client: kubernetes.client.ApiClient, name: str) -> None:
        """Retain the production checked SQL executor."""
        super().__init__(client)
        self.name = name

    def cluster(self) -> dict | None:
        """Resolve the temporary primary rather than a fixed pod name."""
        try:
            return self.custom.get_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                "clusters",
                self.name,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status != NOT_FOUND:
                raise
        return None


@dataclass(frozen=True)
class BackupProbe:
    """Identify exactly the backup and rows belonging to this test."""

    name: str
    backup_id: str
    table: str
    before: str
    after: str
    target: ArchiveTarget


def delete_object(
    api: kubernetes.client.CustomObjectsApi,
    plural: str,
    name: str,
) -> None:
    """Delete only the uniquely named object, tolerating prior removal."""
    try:
        api.delete_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            plural,
            name,
            _request_timeout=REQUEST_TIMEOUT,
        )
    except ApiException as exc:
        if exc.status != NOT_FOUND:
            raise


def wait_for_object(
    api: kubernetes.client.CustomObjectsApi,
    plural: str,
    name: str,
) -> dict:
    """Bound backup completion and recovery readiness waits."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        obj = api.get_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            plural,
            name,
            _request_timeout=REQUEST_TIMEOUT,
        )
        status = obj.get("status", {})
        if plural == "backups":
            if status.get("phase") == "failed":
                msg = "Test backup failed."
                raise RuntimeError(msg)
            if status.get("phase") == "completed":
                return obj
        elif status.get("readyInstances", 0) > 0:
            return obj
        time.sleep(POLL_INTERVAL)
    msg = f"Test {plural} did not become ready before the deadline."
    raise TimeoutError(msg)


def _drop_table(executor: PostgresExec, table: str) -> None:
    executor.query(
        "SELECT format('DROP TABLE IF EXISTS %I', :'table_name') \\gexec",
        {"table_name": table},
        database="mlflow_db",
    )


def _insert(executor: PostgresExec, table: str, sentinel: str) -> None:
    executor.query(
        "SELECT format('INSERT INTO %I VALUES (%L)', :'table_name', :'sentinel') "
        "\\gexec",
        {"table_name": table, "sentinel": sentinel},
        database="mlflow_db",
    )


@contextmanager
def backup_probe(
    client: kubernetes.client.ApiClient,
    s3: dict[str, str],
) -> Generator[BackupProbe]:
    """Write before and after a base backup, then verify the WAL archive."""
    executor = PostgresExec(client)
    api = kubernetes.client.CustomObjectsApi(client)
    identifier = uuid.uuid4().hex
    table = f"restore_probe_{identifier}"
    name = f"backup-test-{identifier}"
    before, after = str(uuid.uuid4()), str(uuid.uuid4())
    with ExitStack() as cleanup:
        cleanup.callback(_drop_table, executor, table)
        executor.query(
            "SELECT format('CREATE TABLE %I (val text)', :'table_name') \\gexec",
            {"table_name": table},
            database="mlflow_db",
        )
        _insert(executor, table, before)
        cleanup.callback(delete_object, api, "backups", name)
        api.create_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            "backups",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "Backup",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {
                    "method": "barmanObjectStore",
                    "cluster": {"name": "postgres"},
                },
            },
            _request_timeout=REQUEST_TIMEOUT,
        )
        completed = wait_for_object(api, "backups", name)
        backup_id = completed.get("status", {}).get("backupId")
        if not isinstance(backup_id, str) or not backup_id:
            msg = "Completed test backup has no backupId."
            raise RuntimeError(msg)
        _insert(executor, table, after)
        target = PostgresLifecycle(
            client,
            {"databases": ["mlflow_db", "skypilot_db"], "kubeconfig": "test-probe"},
        ).cutoff()
        archive = PostgresArchive.from_props(
            {
                "s3_bucket": s3["bucket"],
                "s3_endpoint": s3["endpoint"],
                "s3_access_key": s3["access_key"],
                "s3_secret_key": s3["secret_key"],
            },
        )
        with ExitStack() as connections:
            connections.callback(archive.client.close)
            deadline = time.monotonic() + TIMEOUT
            matching = [
                base
                for base in archive.backups(deadline)
                if base.identifier == backup_id
            ]
            if len(matching) != 1:
                msg = "The completed test backup is not present in the archive."
                raise RuntimeError(msg)
            wait_for_archive(archive, matching[0], target, deadline)
        yield BackupProbe(name, backup_id, table, before, after, target)


def _delete_recovery(client: kubernetes.client.ApiClient, name: str) -> None:
    api = kubernetes.client.CustomObjectsApi(client)
    core = kubernetes.client.CoreV1Api(client)
    delete_object(api, "clusters", name)
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        pods = core.list_namespaced_pod(
            NAMESPACE,
            label_selector=f"cnpg.io/cluster={name}",
            _request_timeout=REQUEST_TIMEOUT,
        )
        try:
            api.get_namespaced_custom_object(
                GROUP,
                VERSION,
                NAMESPACE,
                "clusters",
                name,
                _request_timeout=REQUEST_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status != NOT_FOUND:
                raise
            if not pods.items:
                return
        time.sleep(POLL_INTERVAL)
    msg = "Test recovery pods remain after deletion."
    raise TimeoutError(msg)


@contextmanager
def restored_cluster(
    client: kubernetes.client.ApiClient,
    s3: dict[str, str],
    probe: BackupProbe,
) -> Generator[ClusterExec]:
    """Recover the exact test backup and WAL, cleaning up failed setup too."""
    name = f"restore-test-{uuid.uuid4().hex}"
    api = kubernetes.client.CustomObjectsApi(client)
    with ExitStack() as cleanup:
        cleanup.callback(_delete_recovery, client, name)
        api.create_namespaced_custom_object(
            GROUP,
            VERSION,
            NAMESPACE,
            "clusters",
            {
                "apiVersion": f"{GROUP}/{VERSION}",
                "kind": "Cluster",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {
                    "instances": 1,
                    "bootstrap": {
                        "recovery": {
                            "source": "backup-source",
                            "recoveryTarget": {"backupID": probe.backup_id},
                        },
                    },
                    "storage": {"size": "10Gi"},
                    "externalClusters": [
                        {
                            "name": "backup-source",
                            "barmanObjectStore": {
                                "destinationPath": f"s3://{s3['bucket']}",
                                "endpointURL": s3["endpoint"],
                                "serverName": "postgres",
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
            },
            _request_timeout=REQUEST_TIMEOUT,
        )
        wait_for_object(api, "clusters", name)
        yield ClusterExec(client, name)
