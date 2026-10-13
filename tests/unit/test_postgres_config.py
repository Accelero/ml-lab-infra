"""Exercise updates, identity checks, and Pulumi refresh without live services."""

import copy
import unittest
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException
from pulumi.output import UNKNOWN

from resources import postgres_cluster
from resources.postgres_cluster import _Provider as Provider
from resources.postgres_config import INPUT_FIELDS, check_inputs

from .helpers import expect_equal

_PROPS = {
    "kubeconfig": "test-connection",
    "databases": ["mlflow_db", "skypilot_db"],
    "s3_bucket": "bucket",
    "s3_endpoint": "https://s3.example.org",
    "s3_access_key": "test-access",
    "s3_secret_key": "test-secret",
    "retention_policy": "1w",
}


def cluster(retention: str = "1w", uid: str = "postgres-uid") -> dict:
    """Return a live CNPG response including fields an update must preserve."""
    return {
        "metadata": {"uid": uid, "resourceVersion": "7"},
        "spec": {
            "bootstrap": {"recovery": {"source": "backup-source"}},
            "storage": {"size": "10Gi"},
            "backup": {
                "retentionPolicy": retention,
                "barmanObjectStore": {
                    "destinationPath": "s3://bucket",
                    "endpointURL": "https://s3.example.org",
                    "wal": {"compression": "gzip"},
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
        },
    }


class InputTests(unittest.TestCase):
    """Keep previews deterministic and reject unsafe changes before API calls."""

    def test_unsupported_changes_are_rejected_in_check_and_update(self) -> None:
        """Bucket, endpoint, and databases require explicit migrations."""
        cases = {
            "s3_bucket": "new-bucket",
            "s3_endpoint": "https://new.example.org",
            "databases": ["another_db"],
        }
        with patch.object(postgres_cluster, "_api_client") as api_client:
            for field, value in cases.items():
                news = _PROPS | {field: value}
                with self.subTest(field=field):
                    result = Provider().check(_PROPS, news)
                    fields = [failure.property for failure in result.failures]
                    expect_equal(fields, [field])
                    with pytest.raises(ValueError, match=field):
                        Provider().update("test", _PROPS, news)
            expect_equal(api_client.call_count, 0)

    def test_first_creation_accepts_archive_and_database_configuration(self) -> None:
        """Migration restrictions only apply to existing resources."""
        expect_equal(check_inputs({}, _PROPS), [])

    def test_invalid_retention_and_database_names_are_rejected(self) -> None:
        """Prevent invalid duration values and unsafe bootstrap SQL identifiers."""
        cases = [
            ("retention_policy", value) for value in ("", "0d", "-1w", "30days", None)
        ]
        cases.extend(
            ("databases", value)
            for value in (
                [],
                ["postgres"],
                ["postgres_db"],
                ["template1"],
                ["name; DROP ROLE postgres"],
                ["_db"],
                ["same_db", "same_db"],
                ["same", "same_db"],
                ["x" * 64],
                [None],
                "mlflow_db",
            )
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                failures = check_inputs({}, _PROPS | {field: value})
                expect_equal([failure.property for failure in failures], [field])

    def test_preview_unknowns_defer_validation_until_resolved(self) -> None:
        """New infrastructure outputs must not make first-deployment preview fail."""
        news = dict.fromkeys(INPUT_FIELDS, UNKNOWN)
        expect_equal(check_inputs(_PROPS, news), [])

    def test_diff_detects_inputs_and_provider_changes_without_replacement(self) -> None:
        """Changes use update; output-only identity does not produce perpetual diffs."""
        old = _PROPS | {"cluster_uid": "postgres-uid", "__provider": "old"}
        expect_equal(Provider().diff("test", old, old).changes, expected=False)
        for field in (*INPUT_FIELDS, "__provider"):
            with self.subTest(field=field):
                result = Provider().diff("test", old, old | {field: UNKNOWN})
                expect_equal(result.changes, expected=True)
                expect_equal(result.replaces, None)
        result = Provider().diff("test", old, old | {"cluster_uid": UNKNOWN})
        expect_equal(result.changes, expected=False)


class ConfigurationTests(unittest.TestCase):
    """Do not acknowledge unperformed updates or silently adopt another cluster."""

    def setUp(self) -> None:
        """Supply isolated Kubernetes and S3 doubles."""
        self.api = MagicMock()
        self.api.get_namespaced_custom_object.return_value = cluster()
        self.old = _PROPS | {"cluster_uid": "postgres-uid"}
        self.archive = MagicMock()
        self.archive.backups.return_value = [MagicMock()]
        self.api_client = self.enterContext(
            patch.object(postgres_cluster, "_api_client"),
        )
        self.enterContext(
            patch.object(
                postgres_cluster.kubernetes.client,
                "CustomObjectsApi",
                return_value=self.api,
            ),
        )
        self.archive_factory = self.enterContext(
            patch.object(
                postgres_cluster.PostgresArchive,
                "from_props",
                return_value=self.archive,
            ),
        )
        self.ready = self.enterContext(
            patch.object(postgres_cluster, "_wait_cluster_ready"),
        )

    def test_retention_patch_preserves_bootstrap_and_archive(self) -> None:
        """Patch only retention, using resourceVersion to detect concurrent changes."""
        self.api.get_namespaced_custom_object.side_effect = [cluster(), cluster("30d")]
        news = _PROPS | {"retention_policy": "30d"}
        result = Provider().update("test", self.old, news)
        body = self.api.patch_namespaced_custom_object.call_args.args[5]
        expect_equal(
            body,
            {
                "metadata": {"resourceVersion": "7"},
                "spec": {"backup": {"retentionPolicy": "30d"}},
            },
        )
        expect_equal(result.outs["retention_policy"], "30d")
        expect_equal(result.outs["cluster_uid"], "postgres-uid")
        expect_equal(self.archive_factory.call_count, 0)
        expect_equal(self.ready.call_count, 1)

    def test_retry_after_successful_patch_does_not_patch_again(self) -> None:
        """A partially completed Pulumi update can safely resume."""
        self.api.get_namespaced_custom_object.return_value = cluster("30d")
        Provider().update("test", self.old, _PROPS | {"retention_policy": "30d"})
        expect_equal(self.api.patch_namespaced_custom_object.call_count, 0)

    def test_patch_failure_and_unaccepted_value_do_not_report_success(self) -> None:
        """Fail updates on conflicts, rejected patches, and readiness errors."""
        news = _PROPS | {"retention_policy": "30d"}
        for case in ("conflict", "unchanged", "not-ready"):
            self.api.patch_namespaced_custom_object.side_effect = None
            self.ready.side_effect = None
            self.api.get_namespaced_custom_object.side_effect = None
            self.api.get_namespaced_custom_object.return_value = cluster()
            error = RuntimeError
            if case == "conflict":
                conflict = ApiException(status=409)
                self.api.patch_namespaced_custom_object.side_effect = conflict
                error = ApiException
            elif case == "not-ready":
                reads = [cluster(), cluster("30d")]
                self.api.get_namespaced_custom_object.side_effect = reads
                self.ready.side_effect = TimeoutError
                error = TimeoutError
            with self.subTest(case=case), pytest.raises(error):
                Provider().update("test", self.old, news)

    def test_credential_rotation_reads_archive_without_requesting_backup(self) -> None:
        """The separately managed Secret changes; the provider verifies read access."""
        for field in ("s3_access_key", "s3_secret_key"):
            news = _PROPS | {field: "rotated-test-key"}
            with self.subTest(field=field):
                result = Provider().update("test", self.old, news)
                expect_equal(result.outs[field], "rotated-test-key")
                expect_equal(self.archive_factory.call_args.args[0], news)
        expect_equal(self.archive.backups.call_count, 2)
        expect_equal(self.api.create_namespaced_custom_object.call_count, 0)
        expect_equal(self.api.patch_namespaced_custom_object.call_count, 0)

    def test_failed_credential_verification_prevents_retention_patch(self) -> None:
        """Validate access before any mutation, even when retention also changes."""
        news = _PROPS | {"s3_access_key": "rotated", "retention_policy": "30d"}
        for inaccessible in (False, True):
            self.archive.backups.side_effect = RuntimeError if inaccessible else None
            self.archive.backups.return_value = []
            with self.subTest(inaccessible=inaccessible), pytest.raises(RuntimeError):
                Provider().update("test", self.old, news)
        expect_equal(self.api.patch_namespaced_custom_object.call_count, 0)

    def test_kubeconfig_rotation_requires_the_original_cluster_uid(self) -> None:
        """Endpoint and authentication changes are allowed for the same Cluster."""
        news = _PROPS | {"kubeconfig": "rotated-connection"}
        result = Provider().update("test", self.old, news)
        expect_equal(result.outs["kubeconfig"], news["kubeconfig"])
        self.api.get_namespaced_custom_object.return_value = cluster(uid="another-uid")
        with pytest.raises(RuntimeError, match="identity changed"):
            Provider().update("test", self.old, news)
        expect_equal(self.api.patch_namespaced_custom_object.call_count, 0)

    def test_legacy_state_checks_both_connections(self) -> None:
        """State created before UID tracking must prove both connections agree."""
        news = _PROPS | {"kubeconfig": "rotated-connection"}
        self.api.get_namespaced_custom_object.side_effect = [
            cluster(),
            cluster(),
            cluster(),
        ]
        result = Provider().update("test", _PROPS, news)
        expect_equal(result.outs["cluster_uid"], "postgres-uid")
        reads = [cluster(uid="another"), cluster()]
        self.api.get_namespaced_custom_object.side_effect = reads
        with pytest.raises(RuntimeError, match="identity changed"):
            Provider().update("test", _PROPS, news)

    def test_update_rejects_missing_terminating_and_drifted_clusters(self) -> None:
        """Do not change unrelated resources or acknowledge archive drift."""
        drifted = cluster()
        drifted["spec"]["backup"]["barmanObjectStore"]["destinationPath"] = "s3://other"
        terminating = cluster()
        terminating["metadata"]["deletionTimestamp"] = "2026-10-09T12:00:00Z"
        for live in (ApiException(status=404), terminating, drifted):
            self.api.get_namespaced_custom_object.side_effect = None
            if isinstance(live, ApiException):
                self.api.get_namespaced_custom_object.side_effect = live
            else:
                self.api.get_namespaced_custom_object.return_value = live
            news = _PROPS | {"retention_policy": "30d"}
            with self.subTest(live=live), pytest.raises(RuntimeError):
                Provider().update("test", self.old, news)
        expect_equal(self.api.patch_namespaced_custom_object.call_count, 0)

    def test_refresh_reports_retention_drift_and_updates_diff_inputs(self) -> None:
        """A subsequent up sees the refreshed value and repairs retention."""
        self.api.get_namespaced_custom_object.return_value = cluster("30d")
        result = Provider().read("test", self.old)
        expect_equal(result.id, "test")
        expect_equal(result.outs["retention_policy"], "30d")
        expect_equal(result.inputs["retention_policy"], "30d")
        expect_equal("cluster_uid" in result.inputs, expected=False)
        changes = Provider().diff("test", result.outs, _PROPS).changes
        expect_equal(changes, expected=True)

    def test_refresh_reports_archive_drift_and_blocks_implicit_migration(self) -> None:
        """Drift is visible in state instead of being silently accepted on up."""
        for field, live_field, value in (
            ("s3_bucket", "destinationPath", "s3://other"),
            ("s3_endpoint", "endpointURL", "https://other.example.org"),
        ):
            live = cluster()
            live["spec"]["backup"]["barmanObjectStore"][live_field] = value
            self.api.get_namespaced_custom_object.return_value = live
            with self.subTest(field=field):
                result = Provider().read("test", self.old)
                expect_equal(result.inputs[field], value.removeprefix("s3://"))
                checked = Provider().check(result.inputs, _PROPS)
                fields = [failure.property for failure in checked.failures]
                expect_equal(fields, [field])

    def test_refresh_distinguishes_absence_from_api_errors(self) -> None:
        """Only a 404 marks the resource missing; access failures preserve state."""
        self.api.get_namespaced_custom_object.side_effect = ApiException(status=404)
        result = Provider().read("test", self.old)
        expect_equal(result.id, "")
        expect_equal(result.outs, {})
        errors = (ApiException(status=403), ApiException(status=500), TimeoutError())
        for error in errors:
            self.api.get_namespaced_custom_object.side_effect = error
            with self.subTest(error=error), pytest.raises(type(error)):
                Provider().read("test", self.old)

    def test_refresh_rejects_identity_and_credential_reference_changes(self) -> None:
        """The provider cannot safely represent a different Cluster or Secret."""
        credentials = cluster()
        credentials["spec"]["backup"]["barmanObjectStore"]["s3Credentials"] = {}
        for live in (cluster(uid="another"), credentials):
            self.api.get_namespaced_custom_object.return_value = live
            with self.subTest(live=live), pytest.raises(RuntimeError):
                Provider().read("test", self.old)

    def test_refresh_and_update_do_not_mutate_their_input_dictionaries(self) -> None:
        """Keep outputs separate from Pulumi's input property bags."""
        original = copy.deepcopy(self.old)
        Provider().read("test", self.old)
        Provider().update("test", self.old, _PROPS)
        expect_equal(self.old, original)
        expect_equal(_PROPS.get("cluster_uid"), None)
