# Copyright (c) 2026 David Schmid
"""Write and read application data through the MLflow client API."""

import pathlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mlflow.tracking import MlflowClient


@dataclass(frozen=True)
class RunProbe:
    """Remember an exact run ID and expected artifact bytes."""

    run_id: str
    content: str


def write_run(
    client: MlflowClient,
    experiment: str,
    directory: pathlib.Path,
) -> RunProbe:
    """Log metric history and a small unique artifact, marking failures explicitly."""
    run = client.create_run(experiment)
    identifier = run.info.run_id
    content = uuid.uuid4().hex
    artifact = directory / "probe.txt"
    artifact.write_text(content)
    try:
        client.log_param(identifier, "test_value", content)
        for step in range(3):
            client.log_metric(identifier, "loss", 1 / (step + 1), step=step)
        client.log_artifact(identifier, str(artifact), artifact_path="probe")
        client.set_terminated(identifier, status="FINISHED")
    except BaseException:
        client.set_terminated(identifier, status="FAILED")
        raise
    return RunProbe(identifier, content)


def verify_run(client: MlflowClient, probe: RunProbe, directory: pathlib.Path) -> None:
    """Verify status, parameter, every metric step, and downloaded artifact contents."""
    run = client.get_run(probe.run_id)
    if (
        run.info.status != "FINISHED"
        or run.data.params.get("test_value") != probe.content
    ):
        msg = "MLflow did not preserve the exact test run."
        raise RuntimeError(msg)
    history = client.get_metric_history(probe.run_id, "loss")
    if sorted((entry.step, entry.value) for entry in history) != [
        (step, 1 / (step + 1)) for step in range(3)
    ]:
        msg = "MLflow did not preserve metric history."
        raise RuntimeError(msg)
    directory.mkdir(exist_ok=True)
    artifact = pathlib.Path(
        client.download_artifacts(
            probe.run_id,
            "probe/probe.txt",
            dst_path=str(directory),
        ),
    )
    if artifact.read_text() != probe.content:
        msg = "MLflow artifact contents did not match."
        raise RuntimeError(msg)
