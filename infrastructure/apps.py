"""Helm releases and Kubernetes resources for the hub cluster apps."""

import pulumi
import pulumi_kubernetes as k8s
import pulumi_random as random

from infrastructure.k3s import k8s_provider
from infrastructure.tailscale import operator_oauth_client

_backup_config = pulumi.Config("backup")
_mlflow_config = pulumi.Config("mlflow")
_tailnet = pulumi.Config("tailscale").require("tailnet")

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
    string_data=pulumi.Output.all(
        operator_oauth_client.id,
        operator_oauth_client.key,
    ).apply(
        lambda args: {
            "client_id": args[0],
            "client_secret": args[1],
        },
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_tailscale]),
)

tailscale_operator = k8s.helm.v3.Release(
    "tailscale-operator",
    name="tailscale-operator",
    chart="tailscale-operator",
    namespace="tailscale",
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
    string_data=postgres_password.apply(lambda pwd: {"postgres-password": pwd}),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_infra]),
)

postgres = k8s.helm.v3.Release(
    "postgres",
    name="postgres",
    chart="postgresql",
    namespace="infra",
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://charts.bitnami.com/bitnami",
    ),
    values={
        "primary": {
            "service": {"ports": {"postgresql": 5432}},
            "persistence": {
                "enabled": True,
                "existingClaim": "hub-postgres-pvc",
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
                    "existingSecret": "postgres-secret",
                    "database": "mlflow_db",
                },
            },
        },
    },
    opts=pulumi.ResourceOptions(
        provider=k8s_provider,
        depends_on=[postgres_pvc, postgres_secret],
    ),
)

# ── Postgres S3 backup CronJob ────────────────────────────────────────────────────────

backup_secret = k8s.core.v1.Secret(
    "postgres-backup-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name="postgres-backup-secret",
        namespace="infra",
    ),
    string_data=pulumi.Output.all(
        postgres_password,
        backup_s3_access_key,
        backup_s3_secret_key,
    ).apply(
        lambda args: {
            "POSTGRES_PASSWORD": args[0],
            "S3_ACCESS_KEY_ID": args[1],
            "S3_SECRET_ACCESS_KEY": args[2],
        },
    ),
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
        depends_on=[postgres, backup_secret],
    ),
)

# ── MLflow ────────────────────────────────────────────────────────────────────────────

mlflow_s3_secret = k8s.core.v1.Secret(
    "mlflow-s3-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-s3-secret", namespace="app"),
    string_data=pulumi.Output.all(mlflow_s3_access_key, mlflow_s3_secret_key).apply(
        lambda args: {"access-key-id": args[0], "secret-access-key": args[1]},
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

mlflow_db_secret = k8s.core.v1.Secret(
    "mlflow-db-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="mlflow-db-secret", namespace="app"),
    string_data=postgres_password.apply(
        lambda pwd: {"username": "postgres", "password": pwd},
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

mlflow = k8s.helm.v3.Release(
    "mlflow",
    name="mlflow",
    chart="mlflow",
    namespace="app",
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
                "name": "mlflow-db-secret",
                "usernameKey": "username",
                "passwordKey": "password",
            },
        },
        "artifactRoot": {
            "s3": {
                "enabled": True,
                "bucket": mlflow_s3_bucket,
                "existingSecret": {
                    "name": "mlflow-s3-secret",
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
                    "host": "mlflow",
                    "paths": [{"path": "/", "pathType": "ImplementationSpecific"}],
                },
            ],
            "tls": [{"hosts": ["mlflow"], "secretName": "mlflow-tls"}],
        },
    },
    opts=pulumi.ResourceOptions(
        provider=k8s_provider,
        depends_on=[
            postgres,
            ns_app,
            tailscale_operator,
            mlflow_s3_secret,
            mlflow_db_secret,
        ],
    ),
)

# ── SkyPilot API ──────────────────────────────────────────────────────────────

skypilot_db_secret = k8s.core.v1.Secret(
    "skypilot-db-secret",
    metadata=k8s.meta.v1.ObjectMetaArgs(name="skypilot-db-secret", namespace="app"),
    string_data=postgres_password.apply(
        lambda pwd: {
            "connection_string": f"postgresql://postgres:{pwd}@postgres-postgresql.infra.svc.cluster.local:5432/skypilot_db",
        },
    ),
    opts=pulumi.ResourceOptions(provider=k8s_provider, depends_on=[ns_app]),
)

skypilot = k8s.helm.v3.Release(
    "skypilot-api",
    name="skypilot-api",
    chart="skypilot",
    namespace="app",
    repository_opts=k8s.helm.v3.RepositoryOptsArgs(
        repo="https://helm.skypilot.co",
    ),
    values={
        "apiService": {
            "skipResourceCheck": True,
            "dbConnectionSecretName": "skypilot-db-secret",
            "resources": {
                "requests": {"cpu": "500m", "memory": "500Mi"},
                # "limits": {"cpu": "500m", "memory": "1Gi"},
            },
        },
        "ingress-nginx": {"enabled": False},
        "ingress": {
            "enabled": True,
            "ingressClassName": "tailscale",
            "host": "skypilot",
            "path": "/",
            "tls": {"enabled": True, "secretName": "skypilot-tls"},
        },
    },
    opts=pulumi.ResourceOptions(
        provider=k8s_provider,
        depends_on=[postgres, ns_app, tailscale_operator, skypilot_db_secret],
    ),
)
