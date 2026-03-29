# ruff: noqa: S101, INP001  # assert is idiomatic in pytest
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

import kubernetes.client
import yaml
from kubernetes.stream import stream
from psycopg2.extensions import adapt as _pg_adapt

_NAMESPACE = "infra"
_PRIMARY_POD = "postgres-1"
_SKYPILOT_DB = "skypilot_db"
_CONFIG_KEY = "api_server_config"
_SENTINEL_POLICY = "test_module.SentinelPolicy"


def _psql_exec(core_api: kubernetes.client.CoreV1Api, sql: str) -> str:
    return stream(
        core_api.connect_get_namespaced_pod_exec,
        _PRIMARY_POD,
        _NAMESPACE,
        command=["psql", "-U", "postgres", "-d", _SKYPILOT_DB, "-t", "-A", "-c", sql],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _pg_quote(value: str) -> str:
    q = _pg_adapt(value)
    q.encoding = "utf-8"
    return q.getquoted().decode()


def _read_config(core_api: kubernetes.client.CoreV1Api) -> dict:
    sql = f"SELECT value FROM config_yaml WHERE key = '{_CONFIG_KEY}';"  # noqa: S608
    result = _psql_exec(core_api, sql).strip()
    if not result:
        return {}
    return yaml.safe_load(result) or {}


def _write_config(core_api: kubernetes.client.CoreV1Api, config: dict) -> None:
    yaml_str = yaml.dump(config, default_flow_style=False)
    key_q, val_q = _pg_quote(_CONFIG_KEY), _pg_quote(yaml_str)
    sql = (
        f"INSERT INTO config_yaml (key, value) VALUES ({key_q}, {val_q}) "  # noqa: S608
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    )
    _psql_exec(core_api, sql)


def test_admin_policy_patch(
    k8s_client: kubernetes.client.ApiClient,
    original_skypilot_config: dict,
) -> None:
    """Patching admin_policy must not alter any other config key."""
    core_api = kubernetes.client.CoreV1Api(k8s_client)

    modified = {**original_skypilot_config, "admin_policy": _SENTINEL_POLICY}
    _write_config(core_api, modified)

    result = _read_config(core_api)

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
