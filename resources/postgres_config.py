"""Validate Postgres inputs and observe the configuration owned by Pulumi."""

import re

from pulumi.dynamic import CheckFailure
from pulumi.output import contains_unknowns

INPUT_FIELDS = (
    "kubeconfig",
    "s3_endpoint",
    "s3_bucket",
    "s3_access_key",
    "s3_secret_key",
    "databases",
    "retention_policy",
)
_MIGRATIONS = {
    "s3_bucket": "Changing the archive bucket requires an explicit archive migration.",
    "s3_endpoint": "Changing the archive endpoint requires an archive migration.",
    "databases": "Changing databases requires explicit database and role management.",
}
_RETENTION = re.compile(r"[1-9][0-9]*[dwmy]")
_DATABASE = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RESERVED_DATABASES = {"postgres", "template0", "template1"}


def check_inputs(olds: dict, news: dict) -> list[CheckFailure]:
    """Reject unsupported changes without network access, including in preview."""
    failures = []
    retention = news.get("retention_policy", "1w")
    if not contains_unknowns(retention) and (
        not isinstance(retention, str) or not _RETENTION.fullmatch(retention)
    ):
        failures.append(
            CheckFailure("retention_policy", "Use a positive duration, e.g. 30d."),
        )
    databases = news.get("databases")
    if not contains_unknowns(databases) and (
        not isinstance(databases, list)
        or not databases
        or any(
            not isinstance(name, str)
            or not _DATABASE.fullmatch(name)
            or name in _RESERVED_DATABASES
            or name.removesuffix("_db") in _RESERVED_DATABASES
            or not name.removesuffix("_db")
            for name in databases
        )
        or len(set(databases)) != len(databases)
        or len({name.removesuffix("_db") for name in databases}) != len(databases)
    ):
        failures.append(
            CheckFailure("databases", "Use distinct application databases and roles."),
        )
    for field, reason in _MIGRATIONS.items():
        if (
            field in olds
            and not contains_unknowns(news.get(field))
            and olds[field] != news.get(field)
        ):
            failures.append(CheckFailure(field, reason))
    return failures


def require_valid_update(olds: dict, news: dict) -> None:
    """Enforce the same rules when update is called without a preceding check."""
    failures = check_inputs(olds, news)
    if failures:
        msg = " ".join(f"{failure.property}: {failure.reason}" for failure in failures)
        raise ValueError(msg)


def cluster_uid(cluster: dict, expected: str = "") -> str:
    """Refuse to adopt a different Cluster under the same Kubernetes name."""
    uid = cluster.get("metadata", {}).get("uid")
    if not isinstance(uid, str) or not uid:
        msg = "Postgres Cluster has no Kubernetes UID."
        raise RuntimeError(msg)
    if expected and uid != expected:
        msg = "Postgres Cluster identity changed; refusing configuration operations."
        raise RuntimeError(msg)
    if cluster.get("metadata", {}).get("deletionTimestamp"):
        msg = "Postgres Cluster is being deleted; refusing configuration operations."
        raise RuntimeError(msg)
    return uid


def observed_properties(cluster: dict, props: dict) -> dict:
    """Read mutable retention and archive location without exposing credentials."""
    backup = cluster.get("spec", {}).get("backup", {})
    store = backup.get("barmanObjectStore", {})
    destination = store.get("destinationPath", "")
    if not destination.startswith("s3://") or not store.get("endpointURL"):
        msg = "Postgres backup configuration no longer matches the supported archive."
        raise RuntimeError(msg)
    expected_credentials = {
        "accessKeyId": {"name": "postgres-backup-credentials", "key": "ACCESS_KEY_ID"},
        "secretAccessKey": {
            "name": "postgres-backup-credentials",
            "key": "ACCESS_SECRET_KEY",
        },
    }
    if store.get("s3Credentials") != expected_credentials:
        msg = "Postgres backup credential references changed; repair them first."
        raise RuntimeError(msg)
    return props | {
        "cluster_uid": cluster_uid(cluster, props.get("cluster_uid", "")),
        "retention_policy": backup.get("retentionPolicy", ""),
        "s3_bucket": destination.removeprefix("s3://"),
        "s3_endpoint": store["endpointURL"],
    }


def require_archive_location(cluster: dict, props: dict) -> dict:
    """Prevent updates from acknowledging a different live archive destination."""
    observed = observed_properties(cluster, props)
    if any(observed[field] != props[field] for field in ("s3_bucket", "s3_endpoint")):
        msg = "Live Postgres archive location drifted; repair it before updating."
        raise RuntimeError(msg)
    return observed
