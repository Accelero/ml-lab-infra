"""Custom Pulumi resource managing a CloudNativePG cluster.

On create: checks S3 for an existing backup; restores if found, otherwise runs initdb.
On delete: deletes the cluster and waits for the pod to fully terminate.
           CNPG's graceful shutdown does a final checkpoint, WAL switch, and archives
           the last segment before the pod exits.
"""

import dataclasses
import time

import boto3
import botocore.config
import kubernetes
import kubernetes.client
import kubernetes.config
import yaml
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import CreateResult, Resource, ResourceProvider, UpdateResult

_NAMESPACE = "infra"
_CLUSTER_NAME = "postgres"
_POLL_INTERVAL = 10
_TIMEOUT = 600  # 10 minutes
_GROUP = "postgresql.cnpg.io"
_VERSION = "v1"
_HTTP_NOT_FOUND = 404
_SCHEDULED_BACKUP_NAME = "postgres-scheduled-backup"


@dataclasses.dataclass
class S3Config:
    """S3-compatible object storage credentials and location."""

    endpoint: str
    bucket: str
    access_key: Input[str]
    secret_key: Input[str]


def _api_client(kubeconfig: str) -> kubernetes.client.ApiClient:
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(kubeconfig), client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


def _has_backup(props: dict) -> bool:
    """Return True if at least one base backup exists in S3."""
    s3 = boto3.client(
        "s3",
        endpoint_url=props["s3_endpoint"],
        aws_access_key_id=props["s3_access_key"],
        aws_secret_access_key=props["s3_secret_key"],
        config=botocore.config.Config(signature_version="s3v4"),
    )
    # CNPG barman stores base backups at:
    # {destinationPath}/{cluster-name}/base/{backup-label}/
    # destinationPath = s3://{bucket}/postgres  →  key prefix = postgres/{cluster}/base/
    prefix = f"{_CLUSTER_NAME}/base/"
    resp = s3.list_objects_v2(Bucket=props["s3_bucket"], Prefix=prefix, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def _s3_store(props: dict) -> dict:
    return {
        "destinationPath": f"s3://{props['s3_bucket']}",
        "endpointURL": props["s3_endpoint"],
        "s3Credentials": {
            "accessKeyId": {
                "name": "postgres-backup-credentials",
                "key": "ACCESS_KEY_ID",
            },
            "secretAccessKey": {
                "name": "postgres-backup-credentials",
                "key": "ACCESS_SECRET_KEY",
            },
        },
    }


def _cluster_manifest(props: dict, *, restore: bool) -> dict:
    store = _s3_store(props)
    backup_store = {**store, "wal": {"compression": "gzip"}}
    databases: list[str] = props["databases"]
    roles = [db.removesuffix("_db") for db in databases]

    managed_roles = [
        {
            "name": role,
            "ensure": "present",
            "login": True,
            "passwordSecret": {"name": f"{role}-postgres-credentials"},
        }
        for role in roles
    ]

    if restore:
        bootstrap = {"recovery": {"source": "backup-source"}}
        extra = {
            "externalClusters": [{
                "name": "backup-source",
                "barmanObjectStore": {**store, "serverName": _CLUSTER_NAME},
            }],
        }
    else:
        primary, *rest = databases
        primary_role = primary.removesuffix("_db")
        post_init_sql = []
        for db in rest:
            role = db.removesuffix("_db")
            post_init_sql.extend([
                f"CREATE ROLE {role} WITH LOGIN;",
                f"CREATE DATABASE {db} OWNER {role};",
            ])
        bootstrap = {
            "initdb": {
                "database": primary,
                "owner": primary_role,
                **({"postInitSQL": post_init_sql} if post_init_sql else {}),
            },
        }
        extra = {}

    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": "Cluster",
        "metadata": {"name": _CLUSTER_NAME, "namespace": _NAMESPACE},
        "spec": {
            "instances": 1,
            "bootstrap": bootstrap,
            "storage": {"size": "10Gi"},
            "backup": {"barmanObjectStore": backup_store, "retentionPolicy": "1w"},
            "managed": {"roles": managed_roles},
            **extra,
        },
    }


def _delete_if_exists(
    custom_api: kubernetes.client.CustomObjectsApi,
    plural: str,
    name: str,
) -> None:
    """Delete a namespaced custom object, ignoring 404."""
    try:
        custom_api.delete_namespaced_custom_object(
            _GROUP, _VERSION, _NAMESPACE, plural, name,
        )
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != _HTTP_NOT_FOUND:
            raise


def _wait_backups_complete(custom_api: kubernetes.client.CustomObjectsApi) -> None:
    """Wait for any in-progress Backup CRs to reach a terminal phase."""
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        items = custom_api.list_namespaced_custom_object(
            _GROUP, _VERSION, _NAMESPACE, "backups",
        ).get("items", [])
        in_progress = [
            b["metadata"]["name"]
            for b in items
            if b.get("status", {}).get("phase") not in {"completed", "failed"}
        ]
        if not in_progress:
            log.info("All backups reached a terminal phase.")
            return
        log.info(f"Waiting for backups to finish: {in_progress}")
        time.sleep(_POLL_INTERVAL)
    log.warn(f"Backups still in progress after {_TIMEOUT}s, proceeding anyway.")



def _wait_cluster_ready(custom_api: kubernetes.client.CustomObjectsApi) -> None:
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        obj = custom_api.get_namespaced_custom_object(
            _GROUP, _VERSION, _NAMESPACE, "clusters", _CLUSTER_NAME,
        )
        if obj.get("status", {}).get("readyInstances", 0) > 0:
            log.info(f"Postgres cluster '{_CLUSTER_NAME}' is ready.")
            return
        log.info("Waiting for cluster to become ready...")
    msg = f"Cluster '{_CLUSTER_NAME}' not ready after {_TIMEOUT}s."
    raise TimeoutError(msg)


def _wait_pod_terminated(core_api: kubernetes.client.CoreV1Api) -> None:
    """Wait for cluster pods to exit before secrets are deleted.

    Ensures WAL archiving completes: barman must finish uploading the final
    WAL segment before Pulumi removes the S3 credentials secret.
    """
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        pods = core_api.list_namespaced_pod(
            _NAMESPACE,
            label_selector=f"cnpg.io/cluster={_CLUSTER_NAME}",
        )
        if not pods.items:
            log.info("Cluster pod terminated. Final WAL archived.")
            return
        log.info("Waiting for cluster pod to terminate...")
    log.warn(f"Cluster pod did not terminate within {_TIMEOUT}s, proceeding anyway.")


class _Provider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        client = _api_client(props["kubeconfig"])
        custom_api = kubernetes.client.CustomObjectsApi(client)

        restore = _has_backup(props)
        log.info(f"Bootstrapping cluster via {'recovery' if restore else 'initdb'}...")

        manifest = _cluster_manifest(props, restore=restore)
        deadline = time.monotonic() + 120
        while True:
            try:
                custom_api.create_namespaced_custom_object(
                    _GROUP, _VERSION, _NAMESPACE, "clusters", manifest,
                )
                break
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == _HTTP_NOT_FOUND and time.monotonic() < deadline:
                    log.info("CNPG CRDs not registered yet, retrying in 10s...")
                    time.sleep(10)
                else:
                    raise

        _wait_cluster_ready(custom_api)
        return CreateResult(id_="postgres-cluster", outs=props)

    def update(self, _id: str, _olds: dict, news: dict) -> UpdateResult:
        return UpdateResult(outs=news)

    def delete(self, _id: str, props: dict) -> None:
        client = _api_client(props["kubeconfig"])
        custom_api = kubernetes.client.CustomObjectsApi(client)
        core_api = kubernetes.client.CoreV1Api(client)

        # Stop scheduled backups so none can start while we're tearing down.
        log.info("Deleting scheduled backup CR...")
        _delete_if_exists(custom_api, "scheduledbackups", _SCHEDULED_BACKUP_NAME)

        # Wait for any backup already in flight to reach a terminal state.
        _wait_backups_complete(custom_api)

        log.info(
            f"Deleting cluster '{_CLUSTER_NAME}'."
            " CNPG will flush and archive final WAL on shutdown...",
        )
        custom_api.delete_namespaced_custom_object(
            _GROUP, _VERSION, _NAMESPACE, "clusters", _CLUSTER_NAME,
        )

        # Wait for pod to exit before returning. Prevents Pulumi from deleting the
        # S3 credentials secret while barman is still uploading the final WAL segment.
        _wait_pod_terminated(core_api)


class PostgresCluster(Resource):
    """CNPG Postgres cluster: restores from S3 on create, flushes WAL on delete."""

    def __init__(
        self,
        name: str,
        kubeconfig: Input[str],
        s3: S3Config,
        databases: list[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Initialise the CloudNativePG cluster resource."""
        super().__init__(
            _Provider(),
            name,
            {
                "kubeconfig": kubeconfig,
                "s3_endpoint": s3.endpoint,
                "s3_bucket": s3.bucket,
                "s3_access_key": s3.access_key,
                "s3_secret_key": s3.secret_key,
                "databases": databases,
            },
            opts,
        )
