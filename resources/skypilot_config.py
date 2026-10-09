"""Update SkyPilot's database-persisted admin policy and verify the result."""

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
_TABLE_EXISTS = (
    "SELECT 1 FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_name = 'config_yaml';"
)
_READ_CONFIG = "SELECT value FROM public.config_yaml WHERE key = :'config_key';"
_WRITE_CONFIG = (
    "INSERT INTO public.config_yaml (key, value) "
    "VALUES (:'config_key', :'config_value') "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
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


def _read_config(executor: PostgresExec) -> dict:
    """Distinguish an absent config row from failed SQL or invalid YAML."""
    result = executor.query(_READ_CONFIG, {"config_key": _CONFIG_KEY}, database=_DB)
    if not result:
        return {}
    try:
        config = yaml.safe_load(result)
    except yaml.YAMLError:
        msg = "SkyPilot's persisted configuration is invalid YAML."
        raise RuntimeError(msg) from None
    if not isinstance(config, dict):
        msg = "SkyPilot's persisted configuration must be a mapping."
        raise TypeError(msg)
    return config


def _write_config(executor: PostgresExec, config: dict) -> None:
    variables = {
        "config_key": _CONFIG_KEY,
        "config_value": yaml.safe_dump(config),
    }
    executor.query(_WRITE_CONFIG, variables, database=_DB)


def _apply_policy(props: dict) -> None:
    policy = props["admin_policy"]
    if not isinstance(policy, str) or not policy.strip():
        msg = "SkyPilot admin policy must be a nonempty class path."
        raise ValueError(msg)
    with _api_client(props["kubeconfig"]) as client:
        executor = PostgresExec(client)
        _wait_for_table(executor)
        config = _read_config(executor)
        config["admin_policy"] = policy
        _write_config(executor, config)
        if _read_config(executor).get("admin_policy") != policy:
            msg = "SkyPilot admin policy update could not be verified."
            raise RuntimeError(msg)


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
            config = _read_config(executor)
            if "admin_policy" not in config:
                return
            del config["admin_policy"]
            _write_config(executor, config)
            if "admin_policy" in _read_config(executor):
                msg = "SkyPilot admin policy removal could not be verified."
                raise RuntimeError(msg)


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
