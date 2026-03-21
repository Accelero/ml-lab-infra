"""Dynamic Pulumi resource that patches admin_policy in SkyPilot's postgres config.

SkyPilot's API server persists its full configuration as a YAML blob in postgres
(table: config_yaml, key: api_server_config). Helm chart values are overridden by
the database on startup, so admin_policy must be set directly in the DB.

On create/update: reads the current config, sets admin_policy, UPSERTs back.
On delete: reads the current config, removes admin_policy if present, writes back.
"""

import time

import kubernetes
import kubernetes.client
import kubernetes.config
import yaml
from kubernetes.stream import stream
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import CreateResult, Resource, ResourceProvider, UpdateResult

_NAMESPACE = "infra"
_POD = "postgres-1"
_DB = "skypilot_db"
_CONFIG_KEY = "api_server_config"
_POLL_INTERVAL = 10
_TIMEOUT = 300  # 5 minutes — table appears after SkyPilot first start


def _api_client(kubeconfig: str) -> kubernetes.client.ApiClient:
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(kubeconfig), client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


def _psql_exec(core_api: kubernetes.client.CoreV1Api, sql: str) -> str:
    return stream(
        core_api.connect_get_namespaced_pod_exec,
        _POD,
        _NAMESPACE,
        command=["psql", "-U", "postgres", "-d", _DB, "-t", "-A", "-c", sql],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _wait_for_table(core_api: kubernetes.client.CoreV1Api) -> None:
    """Poll until the config_yaml table exists. SkyPilot creates it on first start."""
    check_sql = (
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = 'config_yaml';"
    )
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        result = _psql_exec(core_api, check_sql)
        if result.strip() == "1":
            log.info("config_yaml table is ready.")
            return
        log.info("Waiting for config_yaml table to be created by SkyPilot...")
        time.sleep(_POLL_INTERVAL)
    msg = f"config_yaml table did not appear within {_TIMEOUT}s."
    raise TimeoutError(msg)


def _read_config(core_api: kubernetes.client.CoreV1Api) -> dict:
    """Read the current SkyPilot API server config. Returns {} if absent."""
    sql = f"SELECT value FROM config_yaml WHERE key = '{_CONFIG_KEY}';"  # noqa: S608
    result = _psql_exec(core_api, sql).strip()
    if not result:
        return {}
    return yaml.safe_load(result) or {}


def _write_config(core_api: kubernetes.client.CoreV1Api, config: dict) -> None:
    """UPSERT the config dict into postgres as YAML."""
    yaml_str = yaml.dump(config, default_flow_style=False)
    escaped = yaml_str.replace("'", "''")
    sql = (
        f"INSERT INTO config_yaml (key, value) VALUES ('{_CONFIG_KEY}', '{escaped}') "  # noqa: S608
        f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;"
    )
    _psql_exec(core_api, sql)


class _Provider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        client = _api_client(props["kubeconfig"])
        core_api = kubernetes.client.CoreV1Api(client)

        _wait_for_table(core_api)

        config = _read_config(core_api)
        config["admin_policy"] = props["admin_policy"]
        log.info(f"Setting admin_policy to '{props['admin_policy']}'...")
        _write_config(core_api, config)

        return CreateResult(id_="skypilot-admin-policy", outs=props)

    def update(self, _id: str, _olds: dict, news: dict) -> UpdateResult:
        client = _api_client(news["kubeconfig"])
        core_api = kubernetes.client.CoreV1Api(client)

        _wait_for_table(core_api)

        config = _read_config(core_api)
        config["admin_policy"] = news["admin_policy"]
        log.info(f"Re-applying admin_policy '{news['admin_policy']}'...")
        _write_config(core_api, config)

        return UpdateResult(outs=news)

    def delete(self, _id: str, props: dict) -> None:
        client = _api_client(props["kubeconfig"])
        core_api = kubernetes.client.CoreV1Api(client)

        try:
            _wait_for_table(core_api)
        except TimeoutError:
            log.warn(
                "config_yaml table not found during delete (pod may already be gone)."
                " Skipping admin_policy removal.",
            )
            return

        config = _read_config(core_api)
        if "admin_policy" in config:
            del config["admin_policy"]
            log.info("Removing admin_policy from SkyPilot config...")
            _write_config(core_api, config)
        else:
            log.info("admin_policy not present in config, nothing to remove.")


class SkyPilotAdminPolicy(Resource):
    """Patches admin_policy in SkyPilot's postgres-persisted config on every deploy."""

    def __init__(
        self,
        name: str,
        kubeconfig: Input[str],
        admin_policy: Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Initialise the SkyPilot admin policy patcher resource."""
        super().__init__(
            _Provider(),
            name,
            {
                "kubeconfig": kubeconfig,
                "admin_policy": admin_policy,
            },
            opts,
        )
