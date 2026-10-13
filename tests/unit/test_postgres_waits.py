"""Exercise readiness, backup transitions, and teardown checkpoints."""

import json
import unittest
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

from resources import postgres_cluster
from resources.postgres_cluster import _wait_backups_complete as wait_backups
from resources.postgres_cluster import _wait_cluster_ready as wait_ready
from resources.postgres_cluster import _wait_pod_terminated as wait_pods

from .helpers import expect_equal
from .test_postgres_archive import base_backup, target
from .test_postgres_lifecycle import lifecycle


class ReadinessTests(unittest.TestCase):
    """Cover transition paths rather than only an already-expired deadline."""

    def test_cluster_waits_until_ready(self) -> None:
        """A pending primary cannot release application dependencies."""
        api = MagicMock()
        api.get_namespaced_custom_object.side_effect = [
            {"status": {"readyInstances": 0}},
            {"status": {"readyInstances": 1}},
        ]
        with patch.object(postgres_cluster.time, "sleep") as sleep:
            wait_ready(api)
        sleep.assert_called_once_with(10)
        expect_equal(api.get_namespaced_custom_object.call_count, 2)

    def test_terminating_cluster_and_api_errors_abort_readiness(self) -> None:
        """Deletion and authorization failures must not be treated as pending."""
        api = MagicMock()
        api.get_namespaced_custom_object.return_value = {
            "metadata": {"deletionTimestamp": "now"},
        }
        with pytest.raises(RuntimeError, match="being deleted"):
            wait_ready(api)
        api.get_namespaced_custom_object.side_effect = ApiException(status=403)
        with pytest.raises(ApiException):
            wait_ready(api)

    def test_readiness_timeout_fails_closed(self) -> None:
        """A permanently unready cluster cannot report success."""
        with patch.object(postgres_cluster, "time") as clock:
            clock.monotonic.side_effect = [0, 601]
            with pytest.raises(TimeoutError):
                wait_ready(MagicMock())

    def test_active_backup_failure_aborts_teardown(self) -> None:
        """An observed running backup failing blocks deletion."""
        api = MagicMock()
        api.list_namespaced_custom_object.side_effect = [
            {
                "items": [
                    {"metadata": {"name": "active"}, "status": {"phase": "running"}},
                ],
            },
            {
                "items": [
                    {"metadata": {"name": "active"}, "status": {"phase": "failed"}},
                ],
            },
        ]
        with (
            patch.object(postgres_cluster.time, "sleep"),
            pytest.raises(RuntimeError, match="Active Postgres backups failed"),
        ):
            wait_backups(api)

    def test_historical_failure_does_not_block_completed_backups(self) -> None:
        """An old failed backup is distinct from a newly failing active one."""
        api = MagicMock()
        api.list_namespaced_custom_object.return_value = {
            "items": [{"metadata": {"name": "old"}, "status": {"phase": "failed"}}],
        }
        wait_backups(api)

    def test_pod_wait_does_not_finish_until_all_pods_are_gone(self) -> None:
        """A delete request alone does not release the VM dependency."""
        api = MagicMock()
        api.list_namespaced_pod.side_effect = [
            MagicMock(items=[MagicMock()]),
            MagicMock(items=[]),
        ]
        with patch.object(postgres_cluster.time, "sleep"):
            wait_pods(api)
        expect_equal(api.list_namespaced_pod.call_count, 2)


class CheckpointTests(unittest.TestCase):
    """Verify the durable teardown proof's Kubernetes operations."""

    def test_saved_checkpoint_contains_connection_backup_and_cutoff(self) -> None:
        """Retries retain the exact archive range that was verified."""
        instance = lifecycle()
        instance.save_checkpoint(base_backup(), target(4))
        body = instance.core.create_namespaced_config_map.call_args.args[1]
        expect_equal(body["data"]["connection_digest"], instance.connection_digest)
        expect_equal(body["data"]["backup_id"], base_backup().identifier)
        expect_equal(json.loads(body["data"]["target"]), asdict(target(4)))

    def test_conflict_patches_but_permission_failure_aborts(self) -> None:
        """Only an existing checkpoint permits the patch fallback."""
        instance = lifecycle()
        instance.core.create_namespaced_config_map.side_effect = ApiException(
            status=409,
        )
        instance.save_checkpoint(base_backup(), target(4))
        instance.core.patch_namespaced_config_map.assert_called_once()
        instance.core.patch_namespaced_config_map.reset_mock()
        instance.core.create_namespaced_config_map.side_effect = ApiException(
            status=403,
        )
        with pytest.raises(ApiException):
            instance.save_checkpoint(base_backup(), target(4))
        instance.core.patch_namespaced_config_map.assert_not_called()

    def test_checkpoint_patch_failure_cannot_report_success(self) -> None:
        """A failed persistence operation cannot release deletion."""
        instance = lifecycle()
        instance.core.create_namespaced_config_map.side_effect = ApiException(
            status=409,
        )
        instance.core.patch_namespaced_config_map.side_effect = ApiException(status=500)
        with pytest.raises(ApiException):
            instance.save_checkpoint(base_backup(), target(4))

    def test_checkpoint_clear_ignores_only_absence(self) -> None:
        """Permission errors cannot leave an old proof silently in place."""
        instance = lifecycle()
        instance.core.delete_namespaced_config_map.side_effect = ApiException(
            status=404,
        )
        instance.clear_checkpoint()
        instance.core.delete_namespaced_config_map.side_effect = ApiException(
            status=403,
        )
        with pytest.raises(ApiException):
            instance.clear_checkpoint()

    def test_unknown_database_connection_state_aborts_before_quiescing(self) -> None:
        """Missing databases or invalid booleans cannot establish a write cutoff."""
        for access in ({}, {"mlflow_db": "true", "skypilot_db": True}):
            with self.subTest(access=access):
                instance = lifecycle()
                with patch.object(
                    instance,
                    "query",
                    return_value=json.dumps(access),
                ) as query:
                    with (
                        pytest.raises(RuntimeError, match="connection state"),
                        instance.quiesce(),
                    ):
                        pytest.fail("Invalid state released the gate")
                    query.assert_called_once()
