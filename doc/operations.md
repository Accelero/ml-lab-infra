# Operations

## Initial deploy

Before running `pulumi up`, make sure your local machine is connected to your Tailscale tailnet.
Pulumi reaches the K3s cluster through Tailscale after the server joins, and will fail to deploy
Helm charts if you are not on the tailnet.

```bash
tailscale status   # confirm you are connected
pulumi up
```

What happens during a first deploy (~5 minutes):

1. Hetzner SSH key created, server provisioned, cloud-init runs (Sever joins tailnet)
2. K3s installed over SSH on the Tailscale interface
3. Tailscale operator, CNPG, MLflow, and SkyPilot Helm releases deployed
4. Postgres cluster created (initdb on first deploy; recovery from S3 if a backup exists)
5. SkyPilot admin policy upserted into Postgres

## Teardown

```bash
pulumi down
```

Resources are destroyed in reverse dependency order. The Postgres cluster deletion blocks while CloudNativePG archives the final WAL segment to S3. This is intentional:
`pulumi down` does not return until the data is safely in S3.

Tailscale devices (`hub-server` and `tailscale-operator`) are deregistered from the tailnet
automatically.

## Restore after teardown

```bash
pulumi up
```

No flags or manual steps needed. The `PostgresCluster` resource detects the backup in S3 and
bootstraps via recovery. MLflow experiment history and SkyPilot job history are preserved.

## Accessing services

Both services require Tailscale:

```bash
tailscale status   # confirm you are on the tailnet
```

Open in your browser:

- MLflow: `https://mlflow.<tailnet>.ts.net`
- SkyPilot: `https://skypilot.<tailnet>.ts.net`

Configure the SkyPilot CLI to use your server:

```bash
sky api login --endpoint https://skypilot.<tailnet>.ts.net
sky check   # verify cloud credentials are configured
```

## Updating the SkyPilot admin policy

Edit `scripts/skypilot_policies.py` then run:

```bash
pulumi up
```

The SkyPilot pod has an annotation (`checksum/skypilot-policies`) computed from the policy file
contents. Any change to the files causes a rolling pod restart, which picks up the updated policy.

To add a new sub-policy:

1. Subclass `sky.AdminPolicy` in `scripts/skypilot_policies.py`
2. Implement `validate_and_mutate(cls, user_request) -> sky.MutatedUserRequest`
3. Append the class to `SkyPilotAdminPolicy._policies`
4. Run `pulumi up`

## Running the integration tests

Tests hit a live deployed stack. They read credentials from Pulumi stack outputs and config at
runtime, so the stack must be deployed before running tests.

```bash
uv sync
uv run pytest tests/ -v
```

Individual test files:

```bash
uv run pytest tests/test_postgres_backup_restore.py -v
uv run pytest tests/test_skypilot_admin_policy.py -v
uv run pytest tests/test_skypilot_mlflow.py -v        # requires RunPod credits
```

What each test covers:

- **`test_postgres_backup_restore.py`**: writes a sentinel row, triggers a CNPG `Backup` CR,
  verifies backup objects exist in S3, spins up a temporary recovery cluster, and confirms the
  sentinel row survived the backup/restore cycle.
- **`test_skypilot_admin_policy.py`**: connects to Postgres directly and verifies the
  `admin_policy` key is set correctly in SkyPilot's `config_yaml` table.
- **`test_skypilot_mlflow.py`**: submits a SkyPilot managed job to a RunPod GPU node. The job logs
  hyperparameters, metrics, and a model artifact to MLflow. The test verifies the run appears in
  MLflow with the expected data.

## Fetching the kubeconfig

Direct cluster access is useful for inspecting logs, running tests, or triggering manual backups.
The kubeconfig is a secret Pulumi stack output and points at the server's Tailscale IP, so your
machine must be on the tailnet.

```bash
KC=$(pulumi stack output hub_kubeconfig --show-secrets) && \
kubectl --kubeconfig <(echo "$KC") get nodes
```

To persist it locally:

```bash
pulumi stack output hub_kubeconfig --show-secrets > ~/.kube/ml-lab.yaml
export KUBECONFIG=~/.kube/ml-lab.yaml
```

## Triggering a manual backup

The scheduled backup runs at 03:00 UTC. To trigger one on demand:

```bash
KC=$(pulumi stack output hub_kubeconfig --show-secrets) && \
kubectl --kubeconfig <(echo "$KC") apply -n infra -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: manual-backup
spec:
  method: barmanObjectStore
  cluster:
    name: postgres
EOF
```

Watch the backup status:

```bash
KC=$(pulumi stack output hub_kubeconfig --show-secrets) && \
kubectl --kubeconfig <(echo "$KC") get backups -n infra -w
```
