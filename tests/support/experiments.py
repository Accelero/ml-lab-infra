# Copyright (c) 2026 David Schmid
"""Own one MLflow experiment and physically remove its S3 artifacts."""

import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Generator

    from mlflow.tracking import MlflowClient

    from support.storage import Bucket


def artifact_prefix(identifier: str, location: str, bucket: str) -> str:
    """Verify the server assigned the exact experiment's directory in this bucket."""
    parsed = urlsplit(location)
    prefix = parsed.path.lstrip("/").rstrip("/")
    if (
        parsed.scheme != "s3"
        or parsed.netloc != bucket
        or parsed.query
        or parsed.fragment
        or not identifier.isdecimal()
        or prefix.split("/")[-1] != identifier
        or any(part in {"", ".", ".."} for part in prefix.split("/"))
    ):
        msg = "MLflow experiment artifacts are outside their expected test directory."
        raise RuntimeError(msg)
    return f"{prefix}/"


@dataclass
class ExperimentOwner:
    """Recover the experiment by unique name if its create response was lost."""

    client: MlflowClient
    bucket: Bucket
    name: str
    identifier: str | None = None

    def cleanup(self) -> None:
        """Attempt artifact deletion even if metadata deletion fails."""
        experiment = (
            self.client.get_experiment(self.identifier)
            if self.identifier is not None
            else self.client.get_experiment_by_name(self.name)
        )
        if experiment is None:
            return
        prefix = artifact_prefix(
            experiment.experiment_id,
            experiment.artifact_location,
            self.bucket.name,
        )
        try:
            if experiment.lifecycle_stage != "deleted":
                self.client.delete_experiment(experiment.experiment_id)
        finally:
            self.bucket.clear_prefix(prefix)


@contextmanager
def owned_experiment(client: MlflowClient, bucket: Bucket) -> Generator[str]:
    """Register cleanup before creating metadata or accepting any artifact writes."""
    owner = ExperimentOwner(client, bucket, f"acceptance-{uuid.uuid4().hex}")
    with ExitStack() as cleanup:
        cleanup.callback(owner.cleanup)
        owner.identifier = client.create_experiment(owner.name)
        experiment = client.get_experiment(owner.identifier)
        artifact_prefix(owner.identifier, experiment.artifact_location, bucket.name)
        yield owner.identifier
