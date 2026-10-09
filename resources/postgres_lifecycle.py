"""Quiesce Postgres and verify its recovery archive before deletion."""

import hashlib
import json
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING

import kubernetes.client
from kubernetes.stream import stream
from pulumi import log

from resources.postgres_archive import ArchiveTarget, BaseBackup, PostgresArchive

if TYPE_CHECKING:
    from collections.abc import Iterator

_NAMESPACE = "infra"
_CLUSTER = "postgres"
_CHECKPOINT = "postgres-teardown-checkpoint"
_NOT_FOUND = 404
_CONFLICT = 409
_REQUEST_TIMEOUT = (5, 30)
_EXEC_TIMEOUT = 60
_POLL_INTERVAL = 10
_TIMEOUT = 600

_OPEN_DATABASES = r"""
SELECT format('ALTER DATABASE %I ALLOW_CONNECTIONS true', datname)
FROM pg_database
WHERE datname IN (SELECT jsonb_array_elements_text(:'databases'::jsonb))
\gexec
"""
_CLOSE_DATABASES = r"""
SELECT format('ALTER DATABASE %I ALLOW_CONNECTIONS false', datname)
FROM pg_database
WHERE datname IN (SELECT jsonb_array_elements_text(:'databases'::jsonb))
\gexec
"""
_DATABASE_ACCESS = """
SELECT coalesce(jsonb_object_agg(datname, datallowconn), '{}'::jsonb)
FROM pg_database
WHERE datname IN (SELECT jsonb_array_elements_text(:'databases'::jsonb));
"""
_DRAIN_SESSIONS = """
SELECT pg_terminate_backend(pid, 30000) FROM pg_stat_activity
WHERE datname IN (SELECT jsonb_array_elements_text(:'databases'::jsonb))
AND pid <> pg_backend_pid();
"""
_SESSION_COUNT = """
SELECT count(*) FROM pg_stat_activity
WHERE datname IN (SELECT jsonb_array_elements_text(:'databases'::jsonb));
"""
_RESTORE_ACCESS = r"""
SELECT format('ALTER DATABASE %I ALLOW_CONNECTIONS %s', key, value)
FROM jsonb_each_text(:'access'::jsonb)
\gexec
"""


class PostgresLifecycle:
    """Manage the database write cutoff and durable deletion checkpoint."""

    def __init__(self, client: kubernetes.client.ApiClient, props: dict) -> None:
        """Keep credentials in memory and bind operations to this cluster."""
        self.core = kubernetes.client.CoreV1Api(client)
        self.custom = kubernetes.client.CustomObjectsApi(client)
        self.databases = props["databases"]
        if (
            not self.databases
            or any(not isinstance(name, str) or not name for name in self.databases)
            or set(self.databases) & {"postgres", "template0", "template1"}
        ):
            msg = "Application databases must exclude administration databases."
            raise ValueError(msg)
        connection_bytes = props["kubeconfig"].encode()
        self.connection_digest = hashlib.sha256(connection_bytes).hexdigest()

    def cluster(self) -> dict | None:
        """Read the live Cluster, distinguishing absence from API failures."""
        try:
            return self.custom.get_namespaced_custom_object(
                "postgresql.cnpg.io",
                "v1",
                _NAMESPACE,
                "clusters",
                _CLUSTER,
                _request_timeout=_REQUEST_TIMEOUT,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != _NOT_FOUND:
                raise
        return None

    def query(self, sql: str, variables: dict[str, str] | None = None) -> str:
        """Execute psql input with checked exit status and a bounded wait."""
        cluster = self.cluster()
        primary = (cluster or {}).get("status", {}).get("currentPrimary")
        if not primary:
            msg = "Postgres has no known primary for archive verification."
            raise RuntimeError(msg)
        command = ["psql", "-X", "-U", "postgres", "-d", "postgres", "-t", "-A"]
        for name, value in {"ON_ERROR_STOP": "1", **(variables or {})}.items():
            command.extend(["--set", f"{name}={value}"])
        response = stream(
            self.core.connect_get_namespaced_pod_exec,
            primary,
            _NAMESPACE,
            command=command,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
            _request_timeout=_REQUEST_TIMEOUT,
        )
        with closing(response) as process:
            process.write_stdin(sql.strip() + "\n\\q\n")
            process.run_forever(timeout=_EXEC_TIMEOUT)
            if process.is_open():
                msg = "Postgres archive command timed out."
                raise TimeoutError(msg)
            if process.returncode != 0:
                error = process.read_stderr().strip()
                msg = f"Postgres archive command failed: {error}"
                raise RuntimeError(msg)
            return process.read_stdout().strip()

    def open_databases(self) -> None:
        """Reopen application databases after restoring a teardown cutoff."""
        self.query(
            _OPEN_DATABASES,
            {"databases": json.dumps(self.databases)},
        )

    @contextmanager
    def quiesce(self) -> Iterator[None]:
        """Block connections and drain writers; restore access on gate failure."""
        variables = {"databases": json.dumps(self.databases)}
        access = json.loads(self.query(_DATABASE_ACCESS, variables))
        if set(access) != set(self.databases) or any(
            not isinstance(value, bool) for value in access.values()
        ):
            msg = "Cannot establish connection state for every application database."
            raise RuntimeError(msg)
        verified = False
        try:
            self.query(_CLOSE_DATABASES, variables)
            self.query(_DRAIN_SESSIONS, variables)
            if self.query(_SESSION_COUNT, variables) != "0":
                msg = "Application database sessions have not drained."
                raise RuntimeError(msg)
            yield
            verified = True
        finally:
            if not verified:
                self.query(
                    _RESTORE_ACCESS,
                    {"access": json.dumps(access)},
                )

    def cutoff(self) -> ArchiveTarget:
        """Create a unique restore point and close the segment containing it."""
        output = self.query(
            "SELECT json_build_object("
            "'wal', pg_walfile_name(pg_create_restore_point(:'point_name')), "
            "'system_identifier', (pg_control_system()).system_identifier::text, "
            "'segment_size', pg_size_bytes(current_setting('wal_segment_size')));",
            {"point_name": f"pulumi_teardown_{uuid.uuid4().hex}"},
        )
        target = ArchiveTarget(**json.loads(output))
        self.query("SELECT pg_switch_wal();")
        return target

    def clear_checkpoint(self) -> None:
        """Discard an earlier deletion proof before accepting application writes."""
        try:
            self.core.delete_namespaced_config_map(
                _CHECKPOINT,
                _NAMESPACE,
                _request_timeout=_REQUEST_TIMEOUT,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != _NOT_FOUND:
                raise

    def save_checkpoint(self, backup: BaseBackup, target: ArchiveTarget) -> None:
        """Persist the verified cutoff so interrupted deletion can be retried."""
        data = {
            "connection_digest": self.connection_digest,
            "backup_id": backup.identifier,
            "target": json.dumps(asdict(target)),
        }
        body = {
            "metadata": {"name": _CHECKPOINT, "namespace": _NAMESPACE},
            "data": data,
        }
        try:
            self.core.create_namespaced_config_map(
                _NAMESPACE,
                body,
                _request_timeout=_REQUEST_TIMEOUT,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != _CONFLICT:
                raise
            self.core.patch_namespaced_config_map(
                _CHECKPOINT,
                _NAMESPACE,
                body,
                _request_timeout=_REQUEST_TIMEOUT,
            )

    def resume_teardown(self, archive: PostgresArchive) -> None:
        """Require a saved, still-complete cutoff when the Cluster is gone."""
        try:
            checkpoint = self.core.read_namespaced_config_map(
                _CHECKPOINT,
                _NAMESPACE,
                _request_timeout=_REQUEST_TIMEOUT,
            ).data
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != _NOT_FOUND:
                raise
            msg = "Postgres is absent and no verified teardown checkpoint exists."
            raise RuntimeError(msg) from exc
        if (
            not checkpoint
            or checkpoint.get("connection_digest") != self.connection_digest
        ):
            msg = "Teardown checkpoint does not belong to this Kubernetes connection."
            raise RuntimeError(msg)
        target = ArchiveTarget(**json.loads(checkpoint["target"]))
        deadline = time.monotonic() + _TIMEOUT
        backup = next(
            (
                backup
                for backup in archive.backups(deadline)
                if backup.identifier == checkpoint["backup_id"]
            ),
            None,
        )
        if backup is None:
            msg = "The verified teardown base backup is no longer available."
            raise RuntimeError(msg)
        wait_for_archive(archive, backup, target, deadline)

    def initial_backup(self, archive: PostgresArchive) -> None:
        """Complete the first base backup before applications are deployed."""
        deadline = time.monotonic() + _TIMEOUT
        manifest = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Backup",
            "metadata": {"generateName": "postgres-initial-", "namespace": _NAMESPACE},
            "spec": {"method": "barmanObjectStore", "cluster": {"name": _CLUSTER}},
        }
        created = self.custom.create_namespaced_custom_object(
            "postgresql.cnpg.io",
            "v1",
            _NAMESPACE,
            "backups",
            manifest,
            _request_timeout=_REQUEST_TIMEOUT,
        )
        name = created["metadata"]["name"]
        while time.monotonic() < deadline:
            backup = self.custom.get_namespaced_custom_object(
                "postgresql.cnpg.io",
                "v1",
                _NAMESPACE,
                "backups",
                name,
                _request_timeout=_REQUEST_TIMEOUT,
            )
            phase = backup.get("status", {}).get("phase")
            if phase == "failed":
                msg = "Initial Postgres base backup failed."
                raise RuntimeError(msg)
            if phase == "completed":
                target = self.cutoff()
                selected = select_backup(archive, target, deadline)
                wait_for_archive(archive, selected, target, deadline)
                log.info("Initial Postgres base backup and WAL verified in S3.")
                return
            time.sleep(_POLL_INTERVAL)
        msg = "Initial Postgres base backup did not complete before the deadline."
        raise TimeoutError(msg)


def select_backup(
    archive: PostgresArchive,
    target: ArchiveTarget,
    deadline: float,
) -> BaseBackup:
    """Require a completed base backup of this PostgreSQL system."""
    selected = next(
        (
            backup
            for backup in archive.backups(deadline)
            if backup.system_identifier == target.system_identifier
        ),
        None,
    )
    if selected is None:
        msg = "No completed base backup exists for this PostgreSQL system."
        raise RuntimeError(msg)
    return selected


def wait_for_archive(
    archive: PostgresArchive,
    backup: BaseBackup,
    target: ArchiveTarget,
    deadline: float,
) -> None:
    """Stop deletion unless every required WAL and history object is present."""
    while time.monotonic() < deadline:
        missing = archive.missing_wals(backup, target, deadline)
        if not missing:
            log.info(f"Verified S3 WAL coverage through {target.wal}.")
            return
        message = f"Waiting for {len(missing)} WAL objects; first missing: {missing[0]}"
        log.info(message)
        time.sleep(_POLL_INTERVAL)
    msg = "Required WAL archive objects are still missing; refusing Postgres deletion."
    raise TimeoutError(msg)
