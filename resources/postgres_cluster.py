"""Restore CloudNativePG from S3 and verify WAL coverage before teardown."""

import dataclasses
import time

import kubernetes
import kubernetes.client
import kubernetes.config
import yaml
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import (
    CheckResult,
    CreateResult,
    DiffResult,
    ReadResult,
    Resource,
    ResourceProvider,
    UpdateResult,
)

from resources.postgres_archive import PostgresArchive
from resources.postgres_config import (
    INPUT_FIELDS,
    check_inputs,
    cluster_uid,
    observed_properties,
    require_archive_location,
    require_valid_update,
)
from resources.postgres_lifecycle import (
    PostgresLifecycle,
    select_backup,
    wait_for_archive,
)

_NAMESPACE = "infra"
_CLUSTER_NAME = "postgres"
_POLL_INTERVAL = 10
_TIMEOUT = 600  # 10 minutes
_GROUP = "postgresql.cnpg.io"
_VERSION = "v1"
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409
_SCHEDULED_BACKUP_NAME = "postgres-scheduled-backup"
_REQUEST_TIMEOUT = (5, 30)


@dataclasses.dataclass
class S3Config:
    """S3-compatible object storage credentials and location."""

    endpoint: str
    bucket: str
    access_key: Input[str]
    secret_key: Input[str]
    retention_policy: str


def _api_client(kubeconfig: str) -> kubernetes.client.ApiClient:
    cfg = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(kubeconfig),
        client_configuration=cfg,
    )
    return kubernetes.client.ApiClient(configuration=cfg)


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


def _cluster_manifest(props: dict, *, restore: bool, backup_id: str = "") -> dict:
    store = _s3_store(props)
    backup_store = {**store, "wal": {"compression": "gzip"}}
    retention_policy = props.get("retention_policy") or "1w"
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
        bootstrap = {
            "recovery": {
                "source": "backup-source",
                "recoveryTarget": {"backupID": backup_id},
            },
        }
        extra = {
            "externalClusters": [
                {
                    "name": "backup-source",
                    "barmanObjectStore": {
                        **store,
                        "serverName": _CLUSTER_NAME,
                        "wal": {"maxParallel": 8},
                    },
                },
            ],
        }
    else:
        primary, *rest = databases
        primary_role = primary.removesuffix("_db")
        post_init_sql = []
        for db in rest:
            role = db.removesuffix("_db")
            post_init_sql.extend(
                [
                    f"CREATE ROLE {role} WITH LOGIN;",
                    f"CREATE DATABASE {db} OWNER {role};",
                ],
            )
        bootstrap = {
            "initdb": {
                "database": primary,
                "owner": primary_role,
                **({"postInitSQL": post_init_sql} if post_init_sql else {}),
            },
        }
        extra = {}

    annotations = {"cnpg.io/skipEmptyWalArchiveCheck": "enabled"} if restore else {}
    return {
        "apiVersion": f"{_GROUP}/{_VERSION}",
        "kind": "Cluster",
        "metadata": {
            "name": _CLUSTER_NAME,
            "namespace": _NAMESPACE,
            "annotations": annotations,
        },
        "spec": {
            "instances": 1,
            "bootstrap": bootstrap,
            "storage": {"size": "10Gi"},
            "backup": {
                "barmanObjectStore": backup_store,
                "retentionPolicy": retention_policy,
            },
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
            _GROUP,
            _VERSION,
            _NAMESPACE,
            plural,
            name,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != _HTTP_NOT_FOUND:
            raise


def _get_cluster(custom_api: kubernetes.client.CustomObjectsApi) -> dict | None:
    try:
        return custom_api.get_namespaced_custom_object(
            _GROUP,
            _VERSION,
            _NAMESPACE,
            "clusters",
            _CLUSTER_NAME,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != _HTTP_NOT_FOUND:
            raise
    return None


def _require_cluster(custom_api: kubernetes.client.CustomObjectsApi) -> dict:
    cluster = _get_cluster(custom_api)
    if cluster is None:
        msg = "Postgres Cluster is missing; run pulumi refresh before recreating it."
        raise RuntimeError(msg)
    return cluster


def _wait_backups_complete(custom_api: kubernetes.client.CustomObjectsApi) -> None:
    """Wait for active backups; fail on a new failure or timeout."""
    deadline = time.monotonic() + _TIMEOUT
    watched = set()
    while time.monotonic() < deadline:
        items = custom_api.list_namespaced_custom_object(
            _GROUP,
            _VERSION,
            _NAMESPACE,
            "backups",
            _request_timeout=_REQUEST_TIMEOUT,
        ).get("items", [])
        in_progress = [
            b["metadata"]["name"]
            for b in items
            if b.get("status", {}).get("phase") not in {"completed", "failed"}
        ]
        failed = [
            backup["metadata"]["name"]
            for backup in items
            if backup.get("status", {}).get("phase") == "failed"
            and backup["metadata"]["name"] in watched
        ]
        if failed:
            msg = f"Active Postgres backups failed: {failed}. Refusing teardown."
            raise RuntimeError(msg)
        watched.update(in_progress)
        if not in_progress:
            log.info("All backups reached a terminal phase.")
            return
        log.info(f"Waiting for backups to finish: {in_progress}")
        time.sleep(_POLL_INTERVAL)
    msg = "Postgres backups remain in progress; refusing teardown."
    raise TimeoutError(msg)


def _wait_cluster_ready(custom_api: kubernetes.client.CustomObjectsApi) -> None:
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        obj = custom_api.get_namespaced_custom_object(
            _GROUP,
            _VERSION,
            _NAMESPACE,
            "clusters",
            _CLUSTER_NAME,
            _request_timeout=_REQUEST_TIMEOUT,
        )
        if obj.get("metadata", {}).get("deletionTimestamp"):
            msg = "Existing Postgres cluster is being deleted; refusing startup."
            raise RuntimeError(msg)
        if obj.get("status", {}).get("readyInstances", 0) > 0:
            log.info(f"Postgres cluster '{_CLUSTER_NAME}' is ready.")
            return
        log.info("Waiting for cluster to become ready...")
        time.sleep(_POLL_INTERVAL)
    msg = f"Cluster '{_CLUSTER_NAME}' not ready after {_TIMEOUT}s."
    raise TimeoutError(msg)


def _wait_pod_terminated(core_api: kubernetes.client.CoreV1Api) -> None:
    """Keep infrastructure available until every Postgres pod has exited."""
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        pods = core_api.list_namespaced_pod(
            _NAMESPACE,
            label_selector=f"cnpg.io/cluster={_CLUSTER_NAME}",
            _request_timeout=_REQUEST_TIMEOUT,
        )
        if not pods.items:
            log.info("Postgres cluster pods terminated.")
            return
        log.info("Waiting for cluster pod to terminate...")
    msg = "Postgres pods have not terminated; refusing further teardown."
    raise TimeoutError(msg)


class _Provider(ResourceProvider):
    def check(self, olds: dict, news: dict) -> CheckResult:
        return CheckResult(inputs=news, failures=check_inputs(olds, news))

    def diff(self, _id: str, olds: dict, news: dict) -> DiffResult:
        fields = (*INPUT_FIELDS, "__provider")
        return DiffResult(changes=any(olds.get(key) != news.get(key) for key in fields))

    def create(self, props: dict) -> CreateResult:
        require_valid_update({}, props)
        client = _api_client(props["kubeconfig"])
        custom_api = kubernetes.client.CustomObjectsApi(client)
        lifecycle = PostgresLifecycle(client, props)
        archive = PostgresArchive.from_props(props)
        deadline = time.monotonic() + _TIMEOUT
        backups = archive.backups(deadline)
        restore = bool(backups)
        if (
            not restore
            and lifecycle.cluster() is None
            and archive.objects("postgres/", deadline)
        ):
            msg = "Archive contains data but no completed base backup; refusing initdb."
            raise RuntimeError(msg)
        log.info(f"Bootstrapping cluster via {'recovery' if restore else 'initdb'}...")

        manifest = _cluster_manifest(
            props,
            restore=restore,
            backup_id=backups[0].identifier if backups else "",
        )
        crd_deadline = time.monotonic() + 120
        while True:
            try:
                custom_api.create_namespaced_custom_object(
                    _GROUP,
                    _VERSION,
                    _NAMESPACE,
                    "clusters",
                    manifest,
                    _request_timeout=_REQUEST_TIMEOUT,
                )
                break
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == _HTTP_CONFLICT:
                    log.info("Existing Postgres cluster found; waiting for readiness.")
                    break
                if e.status == _HTTP_NOT_FOUND and time.monotonic() < crd_deadline:
                    log.info("CNPG CRDs not registered yet, retrying in 10s...")
                    time.sleep(10)
                else:
                    raise

        _wait_cluster_ready(custom_api)
        cluster = _require_cluster(custom_api)
        outs = require_archive_location(cluster, props)
        if outs["retention_policy"] != (props.get("retention_policy") or "1w"):
            msg = "Existing Postgres retention differs from the requested policy."
            raise RuntimeError(msg)
        lifecycle.clear_checkpoint()
        lifecycle.open_databases()
        if not restore:
            lifecycle.initial_backup(archive)
        return CreateResult(id_="postgres-cluster", outs=outs)

    def update(self, _id: str, olds: dict, news: dict) -> UpdateResult:
        require_valid_update(olds, news)
        with _api_client(news["kubeconfig"]) as client:
            custom_api = kubernetes.client.CustomObjectsApi(client)
            cluster = _require_cluster(custom_api)
            expected_uid = olds.get("cluster_uid", "")
            if not expected_uid and olds["kubeconfig"] != news["kubeconfig"]:
                with _api_client(olds["kubeconfig"]) as old_client:
                    old_api = kubernetes.client.CustomObjectsApi(old_client)
                    expected_uid = cluster_uid(_require_cluster(old_api))
            uid = cluster_uid(cluster, expected_uid)
            require_archive_location(cluster, news)
            if any(
                olds.get(key) != news[key] for key in ("s3_access_key", "s3_secret_key")
            ):
                archive = PostgresArchive.from_props(news)
                if not archive.backups(time.monotonic() + _TIMEOUT):
                    msg = "New S3 credentials cannot read a completed base backup."
                    raise RuntimeError(msg)
            retention = news["retention_policy"]
            if cluster["spec"]["backup"].get("retentionPolicy") != retention:
                version = cluster["metadata"]["resourceVersion"]
                custom_api.patch_namespaced_custom_object(
                    _GROUP,
                    _VERSION,
                    _NAMESPACE,
                    "clusters",
                    _CLUSTER_NAME,
                    {
                        "metadata": {"resourceVersion": version},
                        "spec": {"backup": {"retentionPolicy": retention}},
                    },
                    _request_timeout=_REQUEST_TIMEOUT,
                )
            _wait_cluster_ready(custom_api)
            observed = require_archive_location(
                _require_cluster(custom_api),
                news | {"cluster_uid": uid},
            )
            if observed["retention_policy"] != retention:
                msg = "Postgres retention update was not accepted."
                raise RuntimeError(msg)
            return UpdateResult(outs=observed)

    def read(self, id_: str, props: dict) -> ReadResult:
        with _api_client(props["kubeconfig"]) as client:
            custom_api = kubernetes.client.CustomObjectsApi(client)
            cluster = _get_cluster(custom_api)
            if cluster is None:
                return ReadResult(id_="", outs={})
            observed = observed_properties(cluster, props)
            inputs = {field: observed[field] for field in INPUT_FIELDS}
            return ReadResult(id_=id_, outs=observed, inputs=inputs)

    def delete(self, _id: str, props: dict) -> None:
        client = _api_client(props["kubeconfig"])
        custom_api = kubernetes.client.CustomObjectsApi(client)
        core_api = kubernetes.client.CoreV1Api(client)
        lifecycle = PostgresLifecycle(client, props)
        archive = PostgresArchive.from_props(props)

        cluster = lifecycle.cluster()
        if cluster is None or cluster.get("metadata", {}).get("deletionTimestamp"):
            lifecycle.resume_teardown(archive)
            _wait_pod_terminated(core_api)
            return

        if props.get("cluster_uid"):
            cluster_uid(cluster, props["cluster_uid"])

        log.info("Deleting scheduled backup CR...")
        _delete_if_exists(custom_api, "scheduledbackups", _SCHEDULED_BACKUP_NAME)
        _wait_backups_complete(custom_api)

        lifecycle.clear_checkpoint()
        with lifecycle.quiesce():
            deadline = time.monotonic() + _TIMEOUT
            target = lifecycle.cutoff()
            backup = select_backup(archive, target, deadline)
            wait_for_archive(archive, backup, target, deadline)
            lifecycle.save_checkpoint(backup, target)

        log.info("Recovery archive verified; deleting Postgres cluster.")
        _delete_if_exists(custom_api, "clusters", _CLUSTER_NAME)
        _wait_pod_terminated(core_api)


class PostgresCluster(Resource):
    """CNPG cluster with an initial base backup and a WAL-verified teardown."""

    def __init__(
        self,
        name: str,
        kubeconfig: Input[str],
        s3: S3Config,
        databases: list[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Initialise the CloudNativePG cluster resource."""
        opts = ResourceOptions.merge(
            opts,
            ResourceOptions(
                additional_secret_outputs=[
                    "kubeconfig",
                    "s3_access_key",
                    "s3_secret_key",
                ],
            ),
        )
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
                "retention_policy": s3.retention_policy,
                "cluster_uid": None,
            },
            opts,
        )
