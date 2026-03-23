"""Helm releases and Kubernetes resources for the hub cluster."""

import hashlib
from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random

from infrastructure.hub_cluster import (
    hub_k8s_provider,
    hub_kubeconfig,
    hub_kubeconfig_cmd,
)
from infrastructure.tailscale import (
    operator_oauth_client,
    skypilot_ts_oauth_client,
)
from resources import (
    PostgresCluster,
    S3Config,
    SkyPilotAdminPolicy,
    TailscaleDeviceCleanup,
)

_backup_config = pulumi.Config("backup")
_mlflow_config = pulumi.Config("mlflow")
_runpod_config = pulumi.Config("runpod")
_tailnet = pulumi.Config("tailscale").require("tailnet")

SKYPILOT_DB = "skypilot_db"
MLFLOW_DB = "mlflow_db"
MLFLOW_USER = "mlflow"
SKYPILOT_USER = "skypilot"
runpod_api_key = _runpod_config.require_secret("apiKey")

_mlflow_postgres_password = random.RandomPassword(
    "mlflow-postgres-password",
    length=32,
    special=False,
    opts=pulumi.ResourceOptions(additional_secret_outputs=["result"]),
)
mlflow_user_password = _mlflow_postgres_password.result

_skypilot_postgres_password = random.RandomPassword(
    "skypilot-postgres-password",
    length=32,
    special=False,
    opts=pulumi.ResourceOptions(additional_secret_outputs=["result"]),
)
skypilot_user_password = _skypilot_postgres_password.result

backup_s3_endpoint = _backup_config.require("s3Endpoint")
backup_s3_bucket = _backup_config.require("s3BucketName")
backup_s3_access_key = _backup_config.require_secret("s3AccessKey")
backup_s3_secret_key = _backup_config.require_secret("s3SecretKey")

mlflow_s3_endpoint = _mlflow_config.require("s3Endpoint")
mlflow_s3_bucket = _mlflow_config.require("s3BucketName")
mlflow_s3_access_key = _mlflow_config.require_secret("s3AccessKey")
mlflow_s3_secret_key = _mlflow_config.require_secret("s3SecretKey")

# ── Namespaces ────────────────────────────────────────────────────────────────────────

ns_tailscale = k8s.core.v1.Namespace(
    "ns-tailscale",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="tailscale"),
    opts=pulumi.ResourceOptions(parent=hub_kubeconfig_cmd, provider=hub_k8s_provider),
)
ns_infra = k8s.core.v1.Namespace(
    "ns-infra",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="infra"),
    opts=pulumi.ResourceOptions(parent=hub_kubeconfig_cmd, provider=hub_k8s_provider),
)
ns_app = k8s.core.v1.Namespace(
    "ns-app",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="app"),
    opts=pulumi.ResourceOptions(parent=hub_kubeconfig_cmd, provider=hub_k8s_provider),
)

# ── Tailscale operator ────────────────────────────────────────────────────────────────

tailscale_operator_secret = k8s.core.v1.Secret(
    "tailscale-operator-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="operator-oauth",
        namespace="tailscale",
    ),
    string_data={
        "client_id": operator_oauth_client.id,
        "client_secret": operator_oauth_client.key,
    },
    opts=pulumi.ResourceOptions(
        parent=ns_tailscale,
        provider=hub_k8s_provider,
    ),
)

tailscale_operator = k8s.helm.v3.Release(
    "tailscale-operator",
    name="tailscale-operator",
    chart="tailscale-operator",
    namespace=ns_tailscale.metadata["name"],
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://pkgs.tailscale.com/helmcharts",
    ),
    values={
        "operatorConfig": {
            "hostname": "tailscale-operator",
        },
    },
    opts=pulumi.ResourceOptions(
        parent=ns_tailscale,
        provider=hub_k8s_provider,
        depends_on=[tailscale_operator_secret],
    ),
)

tailscale_operator_ts_cleanup = TailscaleDeviceCleanup(
    "tailscale-operator-ts-cleanup",
    hostname="tailscale-operator",
    opts=pulumi.ResourceOptions(
        parent=tailscale_operator,
        delete_before_replace=True,
        replacement_trigger=[tailscale_operator.id],
    ),
)

# ── CNPG operator ─────────────────────────────────────────────────────────────────────

ns_cnpg = k8s.core.v1.Namespace(
    "ns-cnpg",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="cnpg-system"),
    opts=pulumi.ResourceOptions(parent=hub_kubeconfig_cmd, provider=hub_k8s_provider),
)

cnpg_operator = k8s.helm.v3.Release(
    "cnpg-operator",
    name="cnpg",
    chart="cloudnative-pg",
    namespace=ns_cnpg.metadata["name"],
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://cloudnative-pg.github.io/charts",
    ),
    opts=pulumi.ResourceOptions(
        parent=ns_cnpg,
        provider=hub_k8s_provider,
    ),
)

# ── Postgres (CloudNativePG) ──────────────────────────────────────────────────────────

mlflow_role_secret = k8s.core.v1.Secret(
    "mlflow-role-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="mlflow-postgres-credentials",
        namespace="infra",
    ),
    string_data={
        "username": MLFLOW_USER,
        "password": mlflow_user_password,
    },
    opts=pulumi.ResourceOptions(parent=ns_infra, provider=hub_k8s_provider),
)

skypilot_role_secret = k8s.core.v1.Secret(
    "skypilot-role-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-postgres-credentials",
        namespace="infra",
    ),
    string_data={
        "username": SKYPILOT_USER,
        "password": skypilot_user_password,
    },
    opts=pulumi.ResourceOptions(parent=ns_infra, provider=hub_k8s_provider),
)

postgres_backup_secret = k8s.core.v1.Secret(
    "postgres-backup-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="postgres-backup-credentials",
        namespace="infra",
    ),
    string_data={
        "ACCESS_KEY_ID": backup_s3_access_key,
        "ACCESS_SECRET_KEY": backup_s3_secret_key,
    },
    opts=pulumi.ResourceOptions(parent=ns_infra, provider=hub_k8s_provider),
)

postgres = PostgresCluster(
    "postgres-cluster",
    kubeconfig=hub_kubeconfig,
    s3=S3Config(
        endpoint=backup_s3_endpoint,
        bucket=backup_s3_bucket,
        access_key=backup_s3_access_key,
        secret_key=backup_s3_secret_key,
    ),
    databases=[MLFLOW_DB, SKYPILOT_DB],
    opts=pulumi.ResourceOptions(
        parent=ns_infra,
        depends_on=[
            cnpg_operator,
            mlflow_role_secret,
            skypilot_role_secret,
            postgres_backup_secret,
        ],
        delete_before_replace=True,
    ),
)

postgres_scheduled_backup = k8s.apiextensions.CustomResource(
    "postgres-scheduled-backup",
    api_version="postgresql.cnpg.io/v1",
    kind="ScheduledBackup",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="postgres-scheduled-backup",
        namespace="infra",
    ),
    spec={
        "schedule": "0 0 3 * * *",
        "backupOwnerReference": "cluster",
        "cluster": {"name": "postgres"},
    },
    opts=pulumi.ResourceOptions(
        parent=postgres,
        provider=hub_k8s_provider,
    ),
)

# ── MLflow ────────────────────────────────────────────────────────────────────────────

mlflow_s3_secret = k8s.core.v1.Secret(
    "mlflow-s3-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-s3-secret", namespace="app"),
    string_data={
        "access-key-id": mlflow_s3_access_key,
        "secret-access-key": mlflow_s3_secret_key,
    },
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

mlflow_role_secret = k8s.core.v1.Secret(
    "mlflow-postgres-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-db-secret", namespace="app"),
    string_data={
        "username": MLFLOW_USER,
        "password": mlflow_user_password,
    },
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

mlflow = k8s.helm.v3.Release(
    "mlflow",
    name="mlflow",
    chart="mlflow",
    namespace=ns_app.metadata["name"],
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://community-charts.github.io/helm-charts",
    ),
    values={
        "backendStore": {
            "postgres": {
                "enabled": True,
                "host": "postgres-rw.infra.svc.cluster.local",
                "port": 5432,
                "database": MLFLOW_DB,
            },
            "existingDatabaseSecret": {
                "name": mlflow_role_secret.metadata["name"],
                "usernameKey": "username",
                "passwordKey": "password",
            },
        },
        "artifactRoot": {
            "s3": {
                "enabled": True,
                "bucket": mlflow_s3_bucket,
                "existingSecret": {
                    "name": mlflow_s3_secret.metadata["name"],
                    "keyOfAccessKeyId": "access-key-id",
                    "keyOfSecretAccessKey": "secret-access-key",
                },
            },
        },
        "extraEnvVars": {
            "MLFLOW_S3_ENDPOINT_URL": mlflow_s3_endpoint,
        },
        "log": {
            "enabled": False,
        },  # must be disabled to use uvicorn and allow usage of extraArgs allowedHosts
        "extraArgs": {"allowedHosts": f"*.{_tailnet}"},
        "ingress": {
            "enabled": True,
            "className": "tailscale",
            "hosts": [
                {
                    "host": "mlflow-test",
                    "paths": [{"path": "/", "pathType": "ImplementationSpecific"}],
                },
            ],
            "tls": [{"hosts": ["mlflow-test"], "secretName": "mlflow-tls"}],
        },
    },
    opts=pulumi.ResourceOptions(
        parent=ns_app,
        provider=hub_k8s_provider,
        depends_on=[postgres, tailscale_operator],
        replace_with=[mlflow_s3_secret],
        delete_before_replace=True,
    ),
)

# ── SkyPilot API ──────────────────────────────────────────────────────────────────────

skypilot_role_secret = k8s.core.v1.Secret(
    "skypilot-postgres-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-postgres-secret",
        namespace="app",
    ),
    string_data={
        "connection_string": pulumi.Output.format(
            "postgresql://{}:{}@postgres-rw.infra.svc.cluster.local:5432/{}",
            SKYPILOT_USER,
            skypilot_user_password,
            SKYPILOT_DB,
        ),
    },
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

skypilot_runpod_secret = k8s.core.v1.Secret(
    "skypilot-runpod-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="skypilot-runpod-secret", namespace="app"),
    string_data={"api_key": runpod_api_key},
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

# Tailscale OAuth client for the admin policy. The policy calls the
# Tailscale API at job-submission time to generate a fresh one-time auth key.
skypilot_tailscale_secret = k8s.core.v1.Secret(
    "skypilot-tailscale-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-tailscale-secret",
        namespace="app",
    ),
    string_data={
        "oauth_client_id": skypilot_ts_oauth_client.id,
        "oauth_client_secret": skypilot_ts_oauth_client.key,
    },
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

# Admin policy as ConfigMap so the policy is a real file in the cluster and
# mounted read-only into the SkyPilot API server at /sky-policies/.
skypilot_policy_configmap = k8s.core.v1.ConfigMap(
    "skypilot-policies-configmap",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-policies",
        namespace="app",
        annotations={"pulumi.com/patchForce": "true"},
    ),
    data={
        "skypilot_policies.py": Path("scripts/skypilot_policies.py").read_text(),
        "tailscale_setup.sh": Path("scripts/tailscale_setup.sh").read_text(),
    },
    opts=pulumi.ResourceOptions(parent=ns_app, provider=hub_k8s_provider),
)

skypilot = k8s.helm.v3.Release(
    "skypilot",
    name="skypilot",
    chart="skypilot",
    namespace=ns_app.metadata["name"],
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://helm.skypilot.co",
    ),
    values={
        "apiService": {
            "skipResourceCheck": True,
            "dbConnectionSecretName": skypilot_role_secret.metadata["name"],
            "podAnnotations": {
                "checksum/skypilot-policies": hashlib.sha256(
                    (
                        Path("scripts/skypilot_policies.py").read_text()
                        + Path("scripts/tailscale_setup.sh").read_text()
                    ).encode(),
                ).hexdigest(),
            },  # trigger update of the API server whenever the policy ConfigMap changes
            "extraEnvs": [
                {
                    "name": "TAILSCALE_OAUTH_CLIENT_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": skypilot_tailscale_secret.metadata["name"],
                            "key": "oauth_client_id",
                        },
                    },
                },
                {
                    "name": "TAILSCALE_OAUTH_CLIENT_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": skypilot_tailscale_secret.metadata["name"],
                            "key": "oauth_client_secret",
                        },
                    },
                },
                {
                    "name": "TAILSCALE_TAILNET",
                    "value": _tailnet,
                },
                {
                    "name": "PYTHONPATH",
                    "value": "/sky-policies",
                },
            ],
            "extraVolumes": [
                {
                    "name": "sky-policies",
                    "configMap": {"name": "skypilot-policies"},
                },
            ],
            "extraVolumeMounts": [
                {
                    "name": "sky-policies",
                    "mountPath": "/sky-policies",
                    "readOnly": True,
                },
            ],
            "resources": {
                "requests": {"cpu": "500m", "memory": "500Mi"},
                "limits": {"cpu": "1", "memory": "2Gi"},
            },
        },
        "runpodCredentials": {
            "enabled": True,
            "runpodSecretName": skypilot_runpod_secret.metadata["name"],
        },
        "ingress-nginx": {"enabled": False},
        "ingress": {
            "enabled": True,
            "ingressClassName": "tailscale",
            "host": "skypilot-test",
            "path": "/",
            "tls": {"enabled": True, "secretName": "skypilot-tls"},
        },
    },
    opts=pulumi.ResourceOptions(
        parent=ns_app,
        provider=hub_k8s_provider,
        depends_on=[
            postgres,
            tailscale_operator,
        ],
        replace_with=[
            # skypilot_policy_configmap,
            skypilot_runpod_secret,
            skypilot_tailscale_secret,
        ],
        delete_before_replace=True,
    ),
)

skypilot_admin_policy = SkyPilotAdminPolicy(
    "skypilot-admin-policy",
    kubeconfig=hub_kubeconfig,
    admin_policy="skypilot_policies.SkyPilotAdminPolicy",
    opts=pulumi.ResourceOptions(
        parent=skypilot,
        depends_on=[skypilot],
    ),
)
