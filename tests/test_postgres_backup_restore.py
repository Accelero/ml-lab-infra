# Copyright (c) 2026 David Schmid
# ruff: noqa: S101, INP001  # assert is idiomatic in pytest; tests/ is not a package
"""Verify CNPG restores sentinel data from an S3 backup."""

from __future__ import annotations

import boto3
import botocore.config
import kubernetes.client
from kubernetes.stream import stream

_NAMESPACE = "infra"
_PRIMARY_CLUSTER = "postgres"


def test_backup_objects_in_s3(
    backup_s3_config: dict[str, str],
    sentinel_and_backup: tuple[str, str],  # noqa: ARG001
) -> None:
    """Base backup objects must be present in S3 under postgres/base/."""
    s3 = boto3.client(
        "s3",
        endpoint_url=backup_s3_config["endpoint"],
        aws_access_key_id=backup_s3_config["access_key"],
        aws_secret_access_key=backup_s3_config["secret_key"],
        config=botocore.config.Config(signature_version="s3v4"),
    )
    resp = s3.list_objects_v2(
        Bucket=backup_s3_config["bucket"],
        Prefix=f"{_PRIMARY_CLUSTER}/base/",
        MaxKeys=1,
    )
    assert resp.get("KeyCount", 0) > 0, (
        f"No backup objects found under"
        f" s3://{backup_s3_config['bucket']}/{_PRIMARY_CLUSTER}/base/"
    )


def test_restore_contains_sentinel_data(
    k8s_client: kubernetes.client.ApiClient,
    recovery_cluster: str,
    sentinel_and_backup: tuple[str, str],
) -> None:
    """The sentinel row written before the backup must exist in the recovery cluster."""
    _, sentinel_uuid = sentinel_and_backup
    core_api = kubernetes.client.CoreV1Api(k8s_client)

    select_sql = (
        f"SELECT val FROM backup_restore_sentinel WHERE val = '{sentinel_uuid}';"  # noqa: S608
    )
    output = stream(
        core_api.connect_get_namespaced_pod_exec,
        recovery_cluster,
        _NAMESPACE,
        command=[
            "psql",
            "-U",
            "postgres",
            "-d",
            "mlflow_db",
            "-t",
            "-A",
            "-c",
            select_sql,
        ],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )

    assert sentinel_uuid in output, (
        f"Sentinel UUID '{sentinel_uuid}' not found in recovery cluster.\n"
        f"psql output: {output!r}"
    )
