# Copyright (c) 2026 David Schmid
"""Verify exact experiment artifact cleanup on setup and test failure."""

from unittest.mock import MagicMock

import pytest
from support.experiments import artifact_prefix, owned_experiment

from .helpers import expect_equal


@pytest.fixture
def clients() -> tuple[MagicMock, MagicMock]:
    """Model a server-assigned directory with no real MLflow or S3 requests."""
    client = MagicMock()
    client.create_experiment.return_value = "42"
    experiment = MagicMock()
    experiment.experiment_id = "42"
    experiment.artifact_location = "s3://dev-artifacts/42"
    experiment.lifecycle_stage = "active"
    client.get_experiment.return_value = experiment
    client.get_experiment_by_name.return_value = experiment
    bucket = MagicMock()
    bucket.name = "dev-artifacts"
    return client, bucket


def test_test_failure_removes_metadata_and_artifacts(clients: tuple) -> None:
    """Function teardown uses the exact ID even when the test raises."""
    client, bucket = clients

    def failing_test() -> None:
        with owned_experiment(client, bucket) as identifier:
            expect_equal(identifier, "42")
            msg = "test failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="test failed"):
        failing_test()
    client.delete_experiment.assert_called_once_with("42")
    bucket.clear_prefix.assert_called_once_with("42/")


def test_lost_create_response_recovers_the_unique_experiment(clients: tuple) -> None:
    """Setup failures before yield still locate and clean created metadata."""
    client, bucket = clients
    client.create_experiment.side_effect = RuntimeError("response lost")
    with (
        pytest.raises(RuntimeError, match="response lost"),
        owned_experiment(client, bucket),
    ):
        pytest.fail("Failed setup must not yield an experiment.")
    name = client.create_experiment.call_args.args[0]
    client.get_experiment_by_name.assert_called_once_with(name)
    client.delete_experiment.assert_called_once_with("42")
    bucket.clear_prefix.assert_called_once_with("42/")


def test_metadata_deletion_failure_still_cleans_artifacts(clients: tuple) -> None:
    """An API failure cannot skip the physical S3 cleanup attempt."""
    client, bucket = clients
    client.delete_experiment.side_effect = RuntimeError("delete failed")
    with (
        pytest.raises(RuntimeError, match="delete failed"),
        owned_experiment(client, bucket),
    ):
        pass
    bucket.clear_prefix.assert_called_once_with("42/")


@pytest.mark.parametrize(
    "location",
    [
        "s3://production/42",
        "s3://dev-artifacts/",
        "s3://dev-artifacts/43",
        "s3://dev-artifacts/../42",
        "s3://dev-artifacts/42?prefix=root",
        "mlflow-artifacts:/42",
    ],
)
def test_malformed_or_foreign_artifact_locations_refuse_deletion(location: str) -> None:
    """Server metadata cannot broaden cleanup to another bucket or experiment."""
    with pytest.raises(RuntimeError, match="expected test directory"):
        artifact_prefix("42", location, "dev-artifacts")
