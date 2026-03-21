# ruff: noqa: S101  # assert is idiomatic in pytest
"""Integration test: SkyPilot admin_policy patch in postgres config_yaml.

Steps:
1. Read and snapshot the current config_yaml row from skypilot_db.
2. Write a modified config with admin_policy set to a sentinel value.
3. Read the config back and assert:
   a. admin_policy equals the sentinel value.
   b. All other keys are unchanged relative to the original snapshot.
4. Teardown: restore the original config_yaml row.
"""

from __future__ import annotations

import copy
import pathlib
import shutil
import subprocess
from typing import TYPE_CHECKING

import kubernetes
import kubernetes.client
import kubernetes.config
import pytest
import yaml
from kubernetes.stream import stream

if TYPE_CHECKING:
    from collections.abc import Generator

_NAMESPACE = "infra"
_PRIMARY_POD = "postgres-1"
_DB = "skypilot_db"
_CONFIG_KEY = "api_server_config"
_SENTINEL_POLICY = "test_module.SentinelPolicy"

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _pulumi_stack_output(key: str) -> str:
    result = subprocess.run(  # noqa: S603
        [shutil.which("pulumi") or "pulumi", "stack", "output", key, "--show-secrets"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


def _build_k8s_client() -> kubernetes.client.ApiClient:
    raw = _pulumi_stack_output("hub_kubeconfig")
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(raw),
        client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


def _psql_exec(core_api: kubernetes.client.CoreV1Api, sql: str) -> str:
    return stream(
        core_api.connect_get_namespaced_pod_exec,
        _PRIMARY_POD,
        _NAMESPACE,
        command=["psql", "-U", "postgres", "-d", _DB, "-t", "-A", "-c", sql],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _read_config(core_api: kubernetes.client.CoreV1Api) -> dict:
    sql = f"SELECT value FROM config_yaml WHERE key = '{_CONFIG_KEY}';"  # noqa: S608
    result = _psql_exec(core_api, sql).strip()
    if not result:
        return {}
    return yaml.safe_load(result) or {}


def _write_config(core_api: kubernetes.client.CoreV1Api, config: dict) -> None:
    yaml_str = yaml.dump(config, default_flow_style=False)
    escaped = yaml_str.replace("'", "''")
    sql = (
        f"INSERT INTO config_yaml (key, value) VALUES ('{_CONFIG_KEY}', '{escaped}') "  # noqa: S608
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    )
    _psql_exec(core_api, sql)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def k8s_client() -> kubernetes.client.ApiClient:
    """Build a kubernetes ApiClient from the Pulumi stack kubeconfig (in memory)."""
    return _build_k8s_client()


@pytest.fixture(scope="module")
def original_config(
    k8s_client: kubernetes.client.ApiClient,
) -> Generator[dict]:
    """Snapshot the current config_yaml row and restore it on teardown."""
    core_api = kubernetes.client.CoreV1Api(k8s_client)
    snapshot = copy.deepcopy(_read_config(core_api))

    try:
        yield snapshot
    finally:
        _write_config(core_api, snapshot)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_admin_policy_patch_only_changes_admin_policy(
    k8s_client: kubernetes.client.ApiClient,
    original_config: dict,
) -> None:
    """Patching admin_policy must not alter any other config key."""
    core_api = kubernetes.client.CoreV1Api(k8s_client)

    modified = {**original_config, "admin_policy": _SENTINEL_POLICY}
    _write_config(core_api, modified)

    result = _read_config(core_api)

    assert result.get("admin_policy") == _SENTINEL_POLICY, (
        f"Expected admin_policy '{_SENTINEL_POLICY}',"
        f" got {result.get('admin_policy')!r}"
    )

    result_without_policy = {k: v for k, v in result.items() if k != "admin_policy"}
    original_without_policy = {
        k: v for k, v in original_config.items() if k != "admin_policy"
    }
    assert result_without_policy == original_without_policy, (
        "Unexpected changes to config keys other than admin_policy.\n"
        f"Diff: result={result_without_policy!r}, original={original_without_policy!r}"
    )
