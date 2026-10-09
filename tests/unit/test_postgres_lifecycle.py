# Copyright (c) 2026 David Schmid
"""Ensure every deletion failure preserves Postgres infrastructure."""

import json
import unittest
from contextlib import nullcontext
from dataclasses import asdict
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes.client.exceptions import ApiException

from resources import postgres_cluster, postgres_exec, postgres_lifecycle
from resources.postgres_cluster import (
    _cluster_manifest as cluster_manifest,
)
from resources.postgres_cluster import (
    _Provider as Provider,
)
from resources.postgres_cluster import (
    _wait_backups_complete as wait_backups,
)
from resources.postgres_cluster import (
    _wait_pod_terminated as wait_pods,
)
from resources.postgres_lifecycle import PostgresLifecycle, wait_for_archive

from .helpers import expect_equal
from .test_postgres_archive import base_backup, target

_PROPS = {
    "kubeconfig": "test-connection",
    "databases": ["mlflow_db", "skypilot_db"],
    "s3_bucket": "bucket",
    "s3_endpoint": "https://s3.example.org",
}


def lifecycle() -> PostgresLifecycle:
    """Construct a lifecycle with API doubles and no Kubernetes configuration."""
    with (
        patch.object(postgres_lifecycle.kubernetes.client, "CoreV1Api"),
        patch.object(postgres_lifecycle.kubernetes.client, "CustomObjectsApi"),
    ):
        return PostgresLifecycle(MagicMock(), _PROPS)


class DatabaseGateTests(unittest.TestCase):
    """Exercise command failures and database access restoration."""

    def test_failed_gate_restores_original_connection_state(self) -> None:
        """Preserve originally disabled databases while reopening enabled ones."""
        instance = lifecycle()
        access = {"mlflow_db": True, "skypilot_db": False}
        with patch.object(instance, "query") as query:
            query.side_effect = [json.dumps(access), "", "", "0", ""]
            msg = "Missing WAL"
            with pytest.raises(TimeoutError), instance.quiesce():
                raise TimeoutError(msg)
            expect_equal(query.call_args.args[1], {"access": json.dumps(access)})
            expect_equal("jsonb_each_text" in query.call_args.args[0], expected=True)

    def test_successful_gate_keeps_connections_blocked(self) -> None:
        """No application writes may follow the archived cutoff."""
        instance = lifecycle()
        access = dict.fromkeys(_PROPS["databases"], True)
        with patch.object(instance, "query") as query:
            query.side_effect = [json.dumps(access), "", "", "0"]
            with instance.quiesce():
                pass
            expect_equal(query.call_count, 4)

    def test_partial_connection_block_failure_restores_access(self) -> None:
        """A SQL failure after one ALTER DATABASE must not strand either database."""
        instance = lifecycle()
        access = dict.fromkeys(_PROPS["databases"], True)
        with patch.object(instance, "query") as query:
            query.side_effect = [json.dumps(access), RuntimeError, ""]
            with pytest.raises(RuntimeError), instance.quiesce():
                pytest.fail("Gate yielded after failing to block connections")
            expect_equal(query.call_args.args[1], {"access": json.dumps(access)})

    def test_sessions_must_drain(self) -> None:
        """A remaining writer must abort the gate and restore access."""
        instance = lifecycle()
        access = dict.fromkeys(_PROPS["databases"], True)
        with patch.object(instance, "query") as query:
            query.side_effect = [json.dumps(access), "", "", "1", ""]
            with pytest.raises(RuntimeError, match="not drained"), instance.quiesce():
                pytest.fail("Gate yielded despite a remaining writer")
            expect_equal(query.call_args.args[1], {"access": json.dumps(access)})

    def test_sql_errors_and_timeout_are_checked(self) -> None:
        """Do not mistake failed psql commands for successful quiescence."""
        instance = lifecycle()
        for is_open, returncode, error in (
            (False, 1, RuntimeError),
            (True, 0, TimeoutError),
        ):
            process = MagicMock()
            process.is_open.return_value = is_open
            process.returncode = returncode
            process.read_stderr.return_value = "SQL failed"
            with (
                self.subTest(is_open=is_open),
                patch.object(
                    instance.executor,
                    "cluster",
                    return_value={
                        "status": {"currentPrimary": "postgres-3"},
                    },
                ),
                patch.object(postgres_exec, "stream", return_value=process) as run,
                pytest.raises(error),
            ):
                instance.query("SELECT 1;")
            expect_equal(run.call_args.args[1], "postgres-3")
            expect_equal(process.close.call_count, 1)

    def test_cutoff_creates_marker_before_switch(self) -> None:
        """The closed segment must contain the unique restore point."""
        instance = lifecycle()
        cutoff = target(4)
        with patch.object(instance, "query") as query:
            query.side_effect = [json.dumps(asdict(cutoff)), ""]
            expect_equal(instance.cutoff(), cutoff)
            expect_equal(query.call_args, call("SELECT pg_switch_wal();"))
            variables = query.call_args_list[0].args[1]
            name = variables["point_name"]
            expect_equal(name.startswith("pulumi_teardown_"), expected=True)

    def test_archive_timeout_aborts(self) -> None:
        """Never treat missing WAL at the deadline as verified."""
        archive = MagicMock()
        archive.missing_wals.return_value = [target(2).wal]
        with patch.object(postgres_lifecycle, "time") as clock:
            clock.monotonic.side_effect = [0, 2]
            with pytest.raises(TimeoutError):
                wait_for_archive(archive, base_backup(), target(4), 1)

    def test_initial_backup_failure_blocks_startup(self) -> None:
        """Application deployment must not proceed after a failed first base."""
        instance = lifecycle()
        instance.custom.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "postgres-initial-test"},
        }
        instance.custom.get_namespaced_custom_object.return_value = {
            "status": {"phase": "failed"},
        }
        with pytest.raises(RuntimeError, match="base backup failed"):
            instance.initial_backup(MagicMock())

    def test_initial_backup_timeout_blocks_startup(self) -> None:
        """A stuck first backup must not release application dependencies."""
        instance = lifecycle()
        instance.custom.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "postgres-initial-test"},
        }
        with patch.object(postgres_lifecycle, "time") as clock:
            clock.monotonic.side_effect = [0, 1000]
            with pytest.raises(TimeoutError):
                instance.initial_backup(MagicMock())

    def test_initial_backup_checks_archive_after_completion(self) -> None:
        """A completed CR alone does not establish initial recovery coverage."""
        instance = lifecycle()
        instance.custom.create_namespaced_custom_object.return_value = {
            "metadata": {"name": "postgres-initial-test"},
        }
        instance.custom.get_namespaced_custom_object.side_effect = [
            {"status": {"phase": "running"}},
            {"status": {"phase": "completed"}},
        ]
        archive = MagicMock()
        archive.backups.return_value = [base_backup()]
        archive.missing_wals.return_value = []
        with (
            patch.object(postgres_lifecycle, "time") as clock,
            patch.object(instance, "cutoff", return_value=target(4)),
        ):
            clock.monotonic.return_value = 0
            instance.initial_backup(archive)
        expect_equal(archive.missing_wals.call_count, 1)

    def test_resume_requires_checkpoint_for_same_connection(self) -> None:
        """An absent Cluster without proof must not release the VM dependency."""
        instance = lifecycle()
        instance.core.read_namespaced_config_map.side_effect = ApiException(status=404)
        with pytest.raises(RuntimeError, match="no verified"):
            instance.resume_teardown(MagicMock())
        instance.core.read_namespaced_config_map.side_effect = None
        instance.core.read_namespaced_config_map.return_value.data = {
            "connection_digest": "another-connection",
        }
        with pytest.raises(RuntimeError, match="does not belong"):
            instance.resume_teardown(MagicMock())

    def test_resume_rechecks_saved_archive(self) -> None:
        """A saved proof cannot hide subsequent deletion of backup objects."""
        instance = lifecycle()
        instance.core.read_namespaced_config_map.return_value.data = {
            "connection_digest": instance.connection_digest,
            "backup_id": base_backup().identifier,
            "target": json.dumps(asdict(target(4))),
        }
        archive = MagicMock()
        archive.backups.return_value = []
        with pytest.raises(RuntimeError, match="no longer available"):
            instance.resume_teardown(archive)
        archive.backups.return_value = [base_backup()]
        archive.missing_wals.return_value = []
        instance.resume_teardown(archive)
        expect_equal(archive.missing_wals.call_count, 1)


class ProviderGateTests(unittest.TestCase):
    """Check ordering at the Pulumi provider's destructive boundary."""

    def setUp(self) -> None:
        """Isolate provider operations from Kubernetes and S3."""
        self.instance = MagicMock()
        self.instance.cluster.return_value = {"metadata": {"name": "postgres"}}
        self.instance.quiesce.side_effect = nullcontext
        self.instance.cutoff.return_value = target(4)
        self.archive = MagicMock()
        self.archive.backups.return_value = [base_backup()]
        self.archive.missing_wals.return_value = []
        self.api = MagicMock()
        self.api.get_namespaced_custom_object.return_value = {
            **cluster_manifest(_PROPS, restore=False),
            "metadata": {"uid": "test-postgres", "resourceVersion": "1"},
        }
        patches = {
            "_api_client": MagicMock(),
            "PostgresLifecycle": MagicMock(return_value=self.instance),
            "_wait_backups_complete": MagicMock(),
            "_wait_pod_terminated": MagicMock(),
            "_wait_cluster_ready": MagicMock(),
            "_delete_if_exists": MagicMock(),
        }
        self.enterContext(patch.multiple(postgres_cluster, **patches))
        self.delete = patches["_delete_if_exists"]
        archive_patch = patch.object(
            postgres_cluster.PostgresArchive,
            "from_props",
            return_value=self.archive,
        )
        self.enterContext(archive_patch)
        api_patch = patch.object(
            postgres_cluster.kubernetes.client,
            "CustomObjectsApi",
            return_value=self.api,
        )
        self.enterContext(api_patch)

    def test_delete_verifies_before_destroying_without_new_base_backup(self) -> None:
        """Prove the order and absence of a teardown base-backup request."""
        events = []
        self.archive.missing_wals.side_effect = lambda *_: events.append("verify") or []
        self.instance.save_checkpoint.side_effect = lambda *_: events.append("proof")
        self.delete.side_effect = lambda _, plural, __: events.append(plural)
        Provider().delete("test", _PROPS)
        expect_equal(events, ["scheduledbackups", "verify", "proof", "clusters"])
        expect_equal(self.instance.initial_backup.call_count, 0)
        expect_equal(self.api.create_namespaced_custom_object.call_count, 0)

    def test_gate_failure_never_deletes_cluster(self) -> None:
        """Missing base, WAL failure, and command errors must all preserve the CR."""
        cases = ("base", "wal", "sql", "checkpoint")
        for case in cases:
            self.delete.reset_mock()
            self.archive.backups.return_value = [base_backup()]
            self.archive.missing_wals.side_effect = None
            self.instance.cutoff.side_effect = None
            self.instance.save_checkpoint.side_effect = None
            if case == "base":
                self.archive.backups.return_value = []
            elif case == "wal":
                self.archive.missing_wals.side_effect = TimeoutError
            elif case == "sql":
                self.instance.cutoff.side_effect = RuntimeError
            else:
                self.instance.save_checkpoint.side_effect = RuntimeError
            with self.subTest(case=case), pytest.raises((RuntimeError, TimeoutError)):
                Provider().delete("test", _PROPS)
            expect_equal(len(self.delete.call_args_list), 1)
            expect_equal(self.delete.call_args.args[1], "scheduledbackups")

    def test_absent_cluster_requires_resume_verification(self) -> None:
        """Do not bypass archival checks when retrying a partly completed destroy."""
        self.instance.cluster.return_value = None
        self.instance.resume_teardown.side_effect = RuntimeError
        with pytest.raises(RuntimeError):
            Provider().delete("test", _PROPS)
        expect_equal(self.delete.call_count, 0)

    def test_terminating_cluster_resumes_from_checkpoint(self) -> None:
        """A retry must not need SQL access to an already terminating primary."""
        self.instance.cluster.return_value = {
            "metadata": {"deletionTimestamp": "2026-10-09T12:00:00Z"},
        }
        Provider().delete("test", _PROPS)
        expect_equal(self.instance.resume_teardown.call_args, call(self.archive))
        expect_equal(self.instance.cutoff.call_count, 0)

    def test_first_creation_waits_for_initial_backup(self) -> None:
        """A fresh empty archive must get a base backup before create returns."""
        self.archive.backups.return_value = []
        self.archive.objects.return_value = {}
        self.instance.cluster.return_value = None
        result = Provider().create(_PROPS)
        expect_equal(self.instance.initial_backup.call_args, call(self.archive))
        expect_equal(result.outs["cluster_uid"], "test-postgres")

    def test_existing_configuration_mismatch_blocks_startup(self) -> None:
        """Do not reopen databases or run an initial backup against another archive."""
        conflict = ApiException(status=409)
        self.api.create_namespaced_custom_object.side_effect = conflict
        for field, value in (
            ("retentionPolicy", "30d"),
            ("barmanObjectStore", {}),
        ):
            live = cluster_manifest(_PROPS, restore=False)
            live["metadata"]["uid"] = "test-postgres"
            live["spec"]["backup"][field] = value
            self.api.get_namespaced_custom_object.return_value = live
            with self.subTest(field=field), pytest.raises(RuntimeError):
                Provider().create(_PROPS)
        expect_equal(self.instance.open_databases.call_count, 0)
        expect_equal(self.instance.initial_backup.call_count, 0)

    def test_delete_refuses_a_replaced_cluster_before_quiescing(self) -> None:
        """A same-name Cluster with a new UID must not enter destructive teardown."""
        self.instance.cluster.return_value = {"metadata": {"uid": "another-cluster"}}
        with pytest.raises(RuntimeError, match="identity changed"):
            Provider().delete("test", _PROPS | {"cluster_uid": "original-cluster"})
        expect_equal(self.delete.call_count, 0)
        expect_equal(self.instance.quiesce.call_count, 0)

    def test_restore_uses_explicit_backup_and_no_new_base(self) -> None:
        """Bootstrap recovery from the completed backup inspected by the provider."""
        self.api.create_namespaced_custom_object.side_effect = ApiException(status=409)
        props = _PROPS | {
            "s3_bucket": "bucket",
            "s3_endpoint": "https://s3.example.org",
        }
        Provider().create(props)
        manifest = self.api.create_namespaced_custom_object.call_args.args[4]
        expect_equal(
            manifest["spec"]["bootstrap"]["recovery"]["recoveryTarget"],
            {"backupID": base_backup().identifier},
        )
        expect_equal(self.delete.call_count, 0)
        expect_equal(self.instance.initial_backup.call_count, 0)

    def test_existing_archive_without_base_blocks_initdb(self) -> None:
        """Never initialize a new system over an unrecoverable WAL-only archive."""
        self.archive.backups.return_value = []
        self.archive.objects.return_value = {"postgres/wals/file": 1}
        self.instance.cluster.return_value = None
        with pytest.raises(RuntimeError, match="refusing initdb"):
            Provider().create(_PROPS)
        expect_equal(self.api.create_namespaced_custom_object.call_count, 0)

    def test_backup_and_pod_timeouts_fail_closed(self) -> None:
        """Both lifecycle waits must raise rather than warn and continue."""
        for helper in (wait_backups, wait_pods):
            with (
                self.subTest(helper=helper.__name__),
                patch.object(postgres_cluster, "time") as clock,
            ):
                clock.monotonic.side_effect = [0, 1000]
                with pytest.raises(TimeoutError):
                    helper(MagicMock())
