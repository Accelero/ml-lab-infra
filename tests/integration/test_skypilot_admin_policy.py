# Copyright (c) 2026 David Schmid
# ruff: noqa: S101  # assert is idiomatic in pytest
"""Verify policy updates preserve other settings using the production SQL helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from resources.postgres_exec import PostgresExec
from resources.skypilot_config import _change_policy as change_policy
from resources.skypilot_config import _read_config as read_config

if TYPE_CHECKING:
    import kubernetes.client

_SENTINEL_POLICY = "test_module.SentinelPolicy"
pytestmark = [pytest.mark.integration, pytest.mark.live]


def test_admin_policy_patch(
    k8s_client: kubernetes.client.ApiClient,
    original_skypilot_config: dict[str, object],
) -> None:
    """Patching admin_policy must not alter any other config key."""
    executor = PostgresExec(k8s_client)

    change_policy(executor, _SENTINEL_POLICY)
    result, _value = read_config(executor)

    assert result.get("admin_policy") == _SENTINEL_POLICY

    result_without_policy = {k: v for k, v in result.items() if k != "admin_policy"}
    original_without_policy = {
        k: v for k, v in original_skypilot_config.items() if k != "admin_policy"
    }
    assert result_without_policy == original_without_policy, (
        "Unrelated SkyPilot settings changed."
    )
