# Copyright (c) 2026 David Schmid
# ruff: noqa: S101, INP001  # assert is idiomatic in pytest
"""Verify policy updates preserve other settings using the production SQL helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from resources.postgres_exec import PostgresExec
from resources.skypilot_config import _read_config as read_config
from resources.skypilot_config import _write_config as write_config

if TYPE_CHECKING:
    import kubernetes.client

_SENTINEL_POLICY = "test_module.SentinelPolicy"


def test_admin_policy_patch(
    k8s_client: kubernetes.client.ApiClient,
    original_skypilot_config: dict,
) -> None:
    """Patching admin_policy must not alter any other config key."""
    executor = PostgresExec(k8s_client)

    modified = {**original_skypilot_config, "admin_policy": _SENTINEL_POLICY}
    write_config(executor, modified)

    result = read_config(executor)

    assert result.get("admin_policy") == _SENTINEL_POLICY, (
        f"Expected admin_policy '{_SENTINEL_POLICY}',"
        f" got {result.get('admin_policy')!r}"
    )

    result_without_policy = {k: v for k, v in result.items() if k != "admin_policy"}
    original_without_policy = {
        k: v for k, v in original_skypilot_config.items() if k != "admin_policy"
    }
    assert result_without_policy == original_without_policy, (
        "Unexpected changes to config keys other than admin_policy.\n"
        f"Diff: result={result_without_policy!r}, original={original_without_policy!r}"
    )
