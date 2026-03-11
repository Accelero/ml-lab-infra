"""Helm releases and Kubernetes resources for the hub cluster."""

import hashlib
from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random

from infrastructure.k3s import k8s_provider
from infrastructure.tailscale import (
    operator_oauth_client,
    skypilot_tailescale_oauth_client,
)
from resources import TailscaleDeviceCleanup

_backup_config = pulumi.Config("backup")
_mlflow_config = pulumi.Config("mlflow")
_runpod_config = pulumi.Config("runpod")
_tailnet = pulumi.Config("tailscale").require("tailnet")
runpod_api_key = _runpod_config.require_secret("apiKey")

_postgres_password_resource = random.RandomPassword(
    "postgres-password",
    length=32,
    special=False,
    opts=pulumi.ResourceOptions(additional_secret_outputs=["result"]),
)
postgres_password = _postgres_password_resource.result
postgres_data_path = "/var/lib/postgresql/data"

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
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)
ns_infra = k8s.core.v1.Namespace(
    "ns-infra",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="infra"),
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)
ns_app = k8s.core.v1.Namespace(
    "ns-app",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="app"),
    opts=pulumi.ResourceOptions(provider=k8s_provider),
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
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_tailscale]),
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
        provider=k8s_provider,
        depends_on=[tailscale_operator_secret],
    ),
)

tailscale_operator_ts_cleanup = TailscaleDeviceCleanup(
    "tailscale-operator-ts-cleanup",
    hostname="tailscale-operator",
    opts=pulumi.ResourceOptions(
        delete_before_replace=True,
        replacement_trigger=[tailscale_operator.id],
    ),
)

# ── Postgres: PV + PVC ────────────────────────────────────────────────────────────────

postgres_pv = k8s.core.v1.PersistentVolume(
    "hub-postgres-pv",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="hub-postgres-pv"),
    spec=k8s.core.v1.PersistentVolumeSpecArgs(
        capacity={"storage": "10Gi"},
        access_modes=["ReadWriteOnce"],
        persistent_volume_reclaim_policy="Retain",
        host_path=k8s.core.v1.HostPathVolumeSourceArgs(path=postgres_data_path),
        storage_class_name="",
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

postgres_pvc = k8s.core.v1.PersistentVolumeClaim(
    "hub-postgres-pvc",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="hub-postgres-pvc", namespace="infra"),
    spec=k8s.core.v1.PersistentVolumeClaimSpecArgs(
        access_modes=["ReadWriteOnce"],
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={"storage": "10Gi"},
        ),
        volume_name="hub-postgres-pv",
        storage_class_name="",
    ),
    opts=pulumi.ResourceOptions(
        provider=k8s_provider,
        depends_on=[postgres_pv, ns_infra],
    ),
)

# ── Postgres helm chart ───────────────────────────────────────────────────────────────

postgres_secret = k8s.core.v1.Secret(
    "postgres-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="postgres-secret", namespace="infra"),
    string_data={"postgres-password": postgres_password},
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_infra]),
)

postgres = k8s.helm.v3.Release(
    "postgres",
    name="postgres",
    chart="postgresql",
    namespace=ns_infra.metadata["name"],
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://charts.bitnami.com/bitnami",
    ),
    values={
        "primary": {
            "service": {"ports": {"postgresql": 5432}},
            "persistence": {
                "enabled": True,
                "existingClaim": postgres_pvc.metadata["name"],
            },
            "podSecurityContext": {
                "enabled": True,
                "fsGroup": 1001,
            },
            "containerSecurityContext": {"enabled": True},
            "initdb": {
                "scripts": {
                    "create_skypilot.sql": "CREATE DATABASE skypilot_db;",
                },
            },
        },
        "volumePermissions": {
            "enabled": True,
        },
        "global": {
            "postgresql": {
                "auth": {
                    "existingSecret": postgres_secret.metadata["name"],
                    "database": "mlflow_db",
                },
            },
        },
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider),
)

# ── Postgres S3 backup CronJob ────────────────────────────────────────────────────────

backup_k8s_secret = k8s.core.v1.Secret(
    "postgres-backup-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="postgres-backup-secret",
        namespace="infra",
    ),
    string_data={
        "POSTGRES_PASSWORD": postgres_password,
        "S3_ACCESS_KEY_ID": backup_s3_access_key,
        "S3_SECRET_ACCESS_KEY": backup_s3_secret_key,
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_infra]),
)

postgres_backup = k8s.batch.v1.CronJob(
    "postgres-s3-backup",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="postgres-s3-backup",
        namespace="infra",
    ),
    spec=k8s.batch.v1.CronJobSpecArgs(
        schedule="0 3 * * *",
        job_template=k8s.batch.v1.JobTemplateSpecArgs(
            spec=k8s.batch.v1.JobSpecArgs(
                template=k8s.core.v1.PodTemplateSpecArgs(
                    spec=k8s.core.v1.PodSpecArgs(
                        restart_policy="OnFailure",
                        containers=[
                            k8s.core.v1.ContainerArgs(
                                name="backup",
                                image="eeshugerman/postgres-backup-s3:16",
                                env_from=[
                                    k8s.core.v1.EnvFromSourceArgs(
                                        secret_ref=k8s.core.v1.SecretEnvSourceArgs(
                                            name="postgres-backup-secret",
                                        ),
                                    ),
                                ],
                                env=[
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_HOST",
                                        value="postgres-postgresql.infra.svc.cluster.local",
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_USER",
                                        value="postgres",
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="POSTGRES_DATABASE",
                                        value="all",
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="S3_ENDPOINT",
                                        value=backup_s3_endpoint,
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="S3_BUCKET",
                                        value=backup_s3_bucket,
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="S3_PREFIX",
                                        value="postgres",
                                    ),
                                    k8s.core.v1.EnvVarArgs(
                                        name="BACKUP_KEEP_DAYS",
                                        value="7",
                                    ),
                                ],
                            ),
                        ],
                    ),
                ),
            ),
        ),
    ),
    opts=pulumi.ResourceOptions(
        provider=k8s_provider,
        depends_on=[postgres, backup_k8s_secret],
    ),
)

# ── MLflow ────────────────────────────────────────────────────────────────────────────

mlflow_s3_k8s_secret = k8s.core.v1.Secret(
    "mlflow-s3-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-s3-secret", namespace="app"),
    string_data={
        "access-key-id": mlflow_s3_access_key,
        "secret-access-key": mlflow_s3_secret_key,
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

mlflow_postgres_k8s_secret = k8s.core.v1.Secret(
    "mlflow-db-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-db-secret", namespace="app"),
    string_data={
        "username": "postgres",
        "password": postgres_password,
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
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
                "host": "postgres-postgresql.infra.svc.cluster.local",
                "port": 5432,
                "database": "mlflow_db",
            },
            "existingDatabaseSecret": {
                "name": mlflow_postgres_k8s_secret.metadata["name"],
                "usernameKey": "username",
                "passwordKey": "password",
            },
        },
        "artifactRoot": {
            "s3": {
                "enabled": True,
                "bucket": mlflow_s3_bucket,
                "existingSecret": {
                    "name": mlflow_s3_k8s_secret.metadata["name"],
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
        provider=k8s_provider,
        depends_on=[postgres, tailscale_operator],
        replace_with=[mlflow_s3_k8s_secret],
        delete_before_replace=True,
    ),
)

# ── SkyPilot API ──────────────────────────────────────────────────────────────────────

skypilot_postgres_k8s_secret = k8s.core.v1.Secret(
    "skypilot-postgres-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-postgres-secret",
        namespace="app",
    ),
    string_data=postgres_password.apply(
        lambda pwd: {
            "connection_string": f"postgresql://postgres:{pwd}@postgres-postgresql.infra.svc.cluster.local:5432/skypilot_db",
        },
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

# RunPod credentials
runpod_credentials_k8s_secret = k8s.core.v1.Secret(
    "runpod-credentials-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="runpod-credentials", namespace="app"),
    string_data={"api_key": runpod_api_key},
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

# Tailscale OAuth client for the admin policy. The policy calls the
# Tailscale API at job-submission time to generate a fresh one-time auth key.
skypilot_tailscale_k8s_secret = k8s.core.v1.Secret(
    "skypilot-tailscale-k8s-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="skypilot-tailscale-secret",
        namespace="app",
    ),
    string_data={
        "oauth_client_id": skypilot_tailescale_oauth_client.id,
        "oauth_client_secret": skypilot_tailescale_oauth_client.key,
    },
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
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
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
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
            "dbConnectionSecretName": skypilot_postgres_k8s_secret.metadata["name"],
            "podAnnotations": {
                "checksum/skypilot-policies": hashlib.sha256(
                    (
                        Path("scripts/skypilot_policies.py").read_text()
                        + Path("scripts/tailscale_setup.sh").read_text()
                    ).encode()
                ).hexdigest(),
            },  # trigger update of the API server whenever the policy ConfigMap changes
            "extraEnvs": [
                {
                    "name": "TAILSCALE_OAUTH_CLIENT_ID",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": skypilot_tailscale_k8s_secret.metadata["name"],
                            "key": "oauth_client_id",
                        },
                    },
                },
                {
                    "name": "TAILSCALE_OAUTH_CLIENT_SECRET",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": skypilot_tailscale_k8s_secret.metadata["name"],
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
            "runpodSecretName": runpod_credentials_k8s_secret.metadata["name"],
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
        provider=k8s_provider,
        depends_on=[
            postgres,
            tailscale_operator,
        ],
        replace_with=[
            # skypilot_policy_configmap,
            runpod_credentials_k8s_secret,
            skypilot_tailscale_k8s_secret,
        ],
        delete_before_replace=True,
    ),
)
