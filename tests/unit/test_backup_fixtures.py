# Copyright (c) 2026 David Schmid
"""Verify live backup fixtures release resources after partial setup failures."""

import unittest
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException
from support import backups
from support.backups import backup_probe, restored_cluster

from .helpers import expect_equal
from .test_postgres_archive import target

S3 = {
    "endpoint": "https://s3.example.org",
    "bucket": "test-bucket",
    "access_key": "test-key",
    "secret_key": "test-secret",
}


class BackupFixtureTests(unittest.TestCase):
    """Exercise setup exceptions before the context manager yields."""

    def test_failed_backup_wait_removes_backup_and_sentinel_table(self) -> None:
        """A failed backup cannot leave live test objects behind."""
        executor = MagicMock()
        api = MagicMock()
        with (
            patch.object(backups, "PostgresExec", return_value=executor),
            patch.object(
                backups.kubernetes.client,
                "CustomObjectsApi",
                return_value=api,
            ),
            patch.object(backups, "wait_for_object", side_effect=TimeoutError),
            pytest.raises(TimeoutError),
            backup_probe(MagicMock(), S3),
        ):
            pytest.fail("Backup unexpectedly succeeded")
        api.delete_namespaced_custom_object.assert_called_once()
        expect_equal(
            "DROP TABLE IF EXISTS" in executor.query.call_args.args[0],
            expected=True,
        )
        created_name = api.create_namespaced_custom_object.call_args.args[4][
            "metadata"
        ]["name"]
        deleted_name = api.delete_namespaced_custom_object.call_args.args[4]
        expect_equal(deleted_name, created_name)

    def test_creation_timeout_still_attempts_owned_object_cleanup(self) -> None:
        """A lost create response may have created the remote resource."""
        api = MagicMock()
        api.create_namespaced_custom_object.side_effect = TimeoutError
        with (
            patch.object(backups, "PostgresExec"),
            patch.object(
                backups.kubernetes.client,
                "CustomObjectsApi",
                return_value=api,
            ),
            pytest.raises(TimeoutError),
            backup_probe(MagicMock(), S3),
        ):
            pytest.fail("Backup unexpectedly succeeded")
        api.delete_namespaced_custom_object.assert_called_once()

    def test_failed_recovery_setup_deletes_cluster_and_waits_for_pods(self) -> None:
        """The recovery fixture cleans up before yield as well as afterward."""
        api = MagicMock()
        core = MagicMock()
        api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        core.list_namespaced_pod.return_value.items = []
        probe = backups.BackupProbe(
            "backup-test",
            "backup-id",
            "table",
            "before",
            "after",
            target(4),
        )
        with (
            patch.object(
                backups.kubernetes.client,
                "CustomObjectsApi",
                return_value=api,
            ),
            patch.object(backups.kubernetes.client, "CoreV1Api", return_value=core),
            patch.object(backups, "wait_for_object", side_effect=TimeoutError),
            pytest.raises(TimeoutError),
            restored_cluster(MagicMock(), S3, probe),
        ):
            pytest.fail("Recovery unexpectedly succeeded")
        api.delete_namespaced_custom_object.assert_called_once()
        core.list_namespaced_pod.assert_called_once()
        manifest = api.create_namespaced_custom_object.call_args.args[4]
        expect_equal(
            manifest["spec"]["bootstrap"]["recovery"]["recoveryTarget"],
            {"backupID": "backup-id"},
        )

    def test_recovery_cleanup_timeout_is_reported(self) -> None:
        """A successful delete response alone does not prove recovery pod removal."""
        client = MagicMock()
        probe = backups.BackupProbe(
            "backup-test",
            "backup-id",
            "table",
            "before",
            "after",
            target(4),
        )
        with (
            patch.object(backups, "wait_for_object"),
            patch.object(backups, "_delete_recovery", side_effect=TimeoutError),
            pytest.raises(TimeoutError),
            restored_cluster(client, S3, probe),
        ):
            pass

    def test_cleanup_ignores_only_kubernetes_absence(self) -> None:
        """Authorization errors must not be mistaken for successful removal."""
        api = MagicMock()
        api.delete_namespaced_custom_object.side_effect = ApiException(status=404)
        backups.delete_object(api, "clusters", "owned")
        api.delete_namespaced_custom_object.side_effect = ApiException(status=403)
        with pytest.raises(ApiException):
            backups.delete_object(api, "clusters", "owned")
