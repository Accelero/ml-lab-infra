"""Cheap development-stack acceptance tests without renting a GPU."""

import sys
from typing import TYPE_CHECKING

import pytest
from support.mlflow_probe import verify_run, write_run
from support.process import command

if TYPE_CHECKING:
    import pathlib

    from mlflow.tracking import MlflowClient
    from support.stack import DevStack

pytestmark = [pytest.mark.live, pytest.mark.e2e]


def test_mlflow_round_trip(
    mlflow_client: MlflowClient,
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> None:
    """Write and read parameters, metric history, and actual S3 artifact contents."""
    probe = write_run(mlflow_client, experiment_id, tmp_path)
    verify_run(mlflow_client, probe, tmp_path / "download")


def test_skypilot_api(dev_stack: DevStack) -> None:
    """Query SkyPilot through its real Tailscale HTTPS API without provisioning."""
    endpoint = f"https://skypilot.{dev_stack.config('tailscale:tailnet')}"
    command(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "os.environ['SKYPILOT_API_SERVER_ENDPOINT']=sys.argv[1]; "
                "import sky; sky.get(sky.status())"
            ),
            endpoint,
        ],
        timeout=30,
    )
