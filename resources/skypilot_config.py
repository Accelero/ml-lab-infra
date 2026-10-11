# Copyright (c) 2026 David Schmid
"""Update SkyPilot's database-persisted admin policy and verify the result."""

import json
import time

import kubernetes.client
import kubernetes.config
import yaml
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import CreateResult, Resource, ResourceProvider, UpdateResult

from resources.postgres_exec import PostgresExec

_DB = "skypilot_db"
_CONFIG_KEY = "api_server_config"
_POLL_INTERVAL = 10
_TIMEOUT = 300
_QUERY_TIMEOUT = 60
_WRITE_ATTEMPTS = 5
_TABLE_EXISTS = (
    "SELECT 1 FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_name = 'config_yaml';"
)
_READ_CONFIG = (
    "SELECT json_build_object('value', value)::text "
    "FROM public.config_yaml WHERE key = :'config_key';"
)
_WRITE_CONFIG = (
    "SET statement_timeout = '45s'; SET lock_timeout = '10s'; "
    "UPDATE public.config_yaml SET value = :'config_value' "
    "WHERE key = :'config_key' AND value = :'previous_value' RETURNING 1;"
)
_INSERT_CONFIG = (
    "SET statement_timeout = '45s'; SET lock_timeout = '10s'; "
    "INSERT INTO public.config_yaml (key, value) "
    "VALUES (:'config_key', :'config_value') "
    "ON CONFLICT (key) DO NOTHING RETURNING 1;"
)


def _api_client(kubeconfig: str) -> kubernetes.client.ApiClient:
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(kubeconfig),
        client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


def _table_exists(executor: PostgresExec, timeout: float = _QUERY_TIMEOUT) -> bool:
    result = executor.query(_TABLE_EXISTS, database=_DB, timeout=timeout)
    if result not in {"", "1"}:
        msg = "Unexpected response while checking the SkyPilot config table."
        raise RuntimeError(msg)
    return result == "1"


def _wait_for_table(executor: PostgresExec) -> None:
    """Wait for SkyPilot's schema, propagating command failures immediately."""
    deadline = time.monotonic() + _TIMEOUT
    while (remaining := deadline - time.monotonic()) > 0:
        if _table_exists(executor, min(_QUERY_TIMEOUT, remaining)):
            return
        log.info("Waiting for SkyPilot's config_yaml table...")
        time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))
    msg = f"config_yaml table did not appear within {_TIMEOUT}s."
    raise TimeoutError(msg)


def _read_config(executor: PostgresExec) -> tuple[dict[str, object], str | None]:
    """Preserve exact YAML for comparison and distinguish an absent row."""
    result = executor.query(_READ_CONFIG, {"config_key": _CONFIG_KEY}, database=_DB)
    if not result:
        return {}, None
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        msg = "Unexpected response while reading SkyPilot's configuration."
        raise RuntimeError(msg) from None
    if not isinstance(payload, dict) or set(payload) != {"value"}:
        msg = "Unexpected response while reading SkyPilot's configuration."
        raise RuntimeError(msg)
    value = payload["value"]
    if not isinstance(value, str):
        msg = "SkyPilot's persisted configuration must contain YAML text."
        raise TypeError(msg)
    try:
        config = yaml.safe_load(value)
    except yaml.YAMLError:
        msg = "SkyPilot's persisted configuration is invalid YAML."
        raise RuntimeError(msg) from None
    if not isinstance(config, dict):
        msg = "SkyPilot's persisted configuration must be a mapping."
        raise TypeError(msg)
    return config, value


def _write_config(
    executor: PostgresExec,
    config: dict[str, object],
    previous_value: str | None,
) -> bool:
    """Write only the observed value, or insert only when the row is absent."""
    variables = {
        "config_key": _CONFIG_KEY,
        "config_value": yaml.safe_dump(config),
    }
    if previous_value is None:
        sql = _INSERT_CONFIG
    else:
        sql = _WRITE_CONFIG
        variables["previous_value"] = previous_value
    result = executor.query(sql, variables, database=_DB)
    if result not in {"", "1"}:
        msg = "Unexpected response while writing SkyPilot's configuration."
        raise RuntimeError(msg)
    return result == "1"


def _change_policy(executor: PostgresExec, policy: str | None) -> None:
    """Retry stale writes while preserving other settings and verifying success."""
    for _attempt in range(_WRITE_ATTEMPTS):
        config, previous_value = _read_config(executor)
        if policy is None:
            if "admin_policy" not in config:
                return
            del config["admin_policy"]
        else:
            config["admin_policy"] = policy
        if not _write_config(executor, config, previous_value):
            continue
        observed, _value = _read_config(executor)
        if policy is None:
            if "admin_policy" in observed:
                msg = "SkyPilot admin policy removal could not be verified."
                raise RuntimeError(msg)
        elif observed.get("admin_policy") != policy:
            msg = "SkyPilot admin policy update could not be verified."
            raise RuntimeError(msg)
        return
    msg = (
        f"SkyPilot's configuration changed during all {_WRITE_ATTEMPTS} write "
        "attempts; policy change aborted. Retry when concurrent edits have finished."
    )
    raise RuntimeError(msg)


def _apply_policy(props: dict) -> None:
    policy = props["admin_policy"]
    if not isinstance(policy, str) or not policy.strip():
        msg = "SkyPilot admin policy must be a nonempty class path."
        raise ValueError(msg)
    with _api_client(props["kubeconfig"]) as client:
        executor = PostgresExec(client)
        _wait_for_table(executor)
        _change_policy(executor, policy)


class _Provider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        _apply_policy(props)
        return CreateResult(id_="skypilot-admin-policy", outs=props)

    def update(self, _id: str, _olds: dict, news: dict) -> UpdateResult:
        _apply_policy(news)
        return UpdateResult(outs=news)

    def delete(self, _id: str, props: dict) -> None:
        with _api_client(props["kubeconfig"]) as client:
            executor = PostgresExec(client)
            if not _table_exists(executor):
                log.info("SkyPilot config table is absent; no policy to remove.")
                return
            _change_policy(executor, None)


class SkyPilotAdminPolicy(Resource):
    """Manage the admin_policy field in SkyPilot's persisted configuration."""

    def __init__(
        self,
        name: str,
        kubeconfig: Input[str],
        admin_policy: Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Initialise the SkyPilot admin policy resource."""
        opts = ResourceOptions.merge(
            opts,
            ResourceOptions(additional_secret_outputs=["kubeconfig"]),
        )
        super().__init__(
            _Provider(),
            name,
            {
                "kubeconfig": kubeconfig,
                "admin_policy": admin_policy,
            },
            opts,
        )
