# Copyright (c) 2026 David Schmid
"""Draft destructive recovery acceptance for a disposable development stack."""

from typing import TYPE_CHECKING

import pytest
from support.mlflow_probe import verify_run, write_run

from resources.kubernetes_client import kubeconfig_client
from resources.postgres_exec import PostgresExec
from resources.skypilot_config import _read_config as read_config

if TYPE_CHECKING:
    import pathlib

    from mlflow.tracking import MlflowClient
    from support.stack import DevStack

pytestmark = [
    pytest.mark.live,
    pytest.mark.e2e,
    pytest.mark.lifecycle,
    pytest.mark.timeout(5400),
]


def test_destroy_recreate_preserves_application_data(
    dev_stack: DevStack,
    mlflow_client: MlflowClient,
    experiment_id: str,
    tmp_path: pathlib.Path,
) -> None:
    """Recover MLflow data and the persisted policy, then accept fresh writes."""
    probe = write_run(mlflow_client, experiment_id, tmp_path)
    with kubeconfig_client(dev_stack.output("hub_kubeconfig")) as client:
        executor = PostgresExec(client)
        identity = executor.query(
            "SELECT (pg_control_system()).system_identifier::text;",
        )
        settings, _raw = read_config(executor)
        policy = settings.get("admin_policy")
    restored = False
    try:
        dev_stack.pulumi("preview", "--non-interactive", timeout=600)
        dev_stack.pulumi("destroy", "--yes", "--non-interactive", timeout=1200)
        dev_stack.pulumi("up", "--yes", "--refresh", "--non-interactive", timeout=1800)
        restored = True
        with kubeconfig_client(dev_stack.output("hub_kubeconfig")) as client:
            executor = PostgresExec(client)
            recovered_identity = executor.query(
                "SELECT (pg_control_system()).system_identifier::text;",
            )
            recovered_settings, _raw = read_config(executor)
        if (
            recovered_identity != identity
            or recovered_settings.get("admin_policy") != policy
        ):
            pytest.fail(
                "Postgres identity or the application policy was not recovered.",
            )
        verify_run(mlflow_client, probe, tmp_path / "recovered")
        fresh = write_run(mlflow_client, experiment_id, tmp_path)
        verify_run(mlflow_client, fresh, tmp_path / "fresh")
        dev_stack.pulumi(
            "preview",
            "--expect-no-changes",
            "--non-interactive",
            timeout=600,
        )
    finally:
        if not restored:
            dev_stack.pulumi(
                "up",
                "--yes",
                "--refresh",
                "--non-interactive",
                timeout=1800,
            )
