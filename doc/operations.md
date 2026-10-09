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

1. Hetzner SSH key created, server provisioned, cloud-init runs (server joins tailnet)
2. K3s installed over SSH on the Tailscale interface
3. Tailscale operator and CNPG deployed
4. Postgres created or recovered from a completed S3 base backup. A fresh cluster completes an
   initial base backup and verifies its WAL archive before applications deploy.
5. MLflow and SkyPilot deployed; SkyPilot admin policy upserted into Postgres

## Teardown

```bash
pulumi down
```

Resources are destroyed in reverse dependency order. Postgres teardown takes no new base backup.
It drains application sessions, blocks new connections, creates a restore point, switches WAL,
and checks S3 for a completed base backup plus every required WAL segment through that cutoff.
Only then does it delete the Cluster and wait for its pods to exit.

The archive gate, active backup wait, and pod wait each have a ten-minute deadline. Timeout or
verification failure stops teardown; the provider never proceeds with missing archive data.
Fix S3 access, archiving, or backup problems and rerun `pulumi down`. A failed archive gate
restores database connection settings, although dependent applications may already be removed.

If Cluster deletion fails after the gate succeeds, application connections remain disabled.
Retry teardown to complete deletion. If returning the live cluster to service instead, reopen
its application databases through the `postgres` administration database before redeploying
applications. Do not remove the `postgres-teardown-checkpoint` ConfigMap while a deletion is
in progress; it lets retries recheck the archive when the primary is already gone.

Tailscale devices (`hub-server` and `tailscale-operator`) are deregistered from the tailnet
automatically.

## Restore after teardown

```bash
pulumi up
```

No flags or manual steps needed. The `PostgresCluster` resource detects the backup in S3 and
bootstraps via recovery, then reopens application connections. MLflow experiment history and
SkyPilot job history are preserved when the archive remains intact.

Apply this provider change with `pulumi up` before relying on it for an existing stack's next
teardown. If that stack lacks a completed base backup, trigger and complete a manual backup
before teardown; teardown will refuse to create one for you.

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

## Running tests

Isolated lifecycle and archive tests require no deployed infrastructure:

```bash
uv run python -m pytest tests/unit -q
```

Run the live restore test periodically to verify backup contents, since archive inventory
checks cannot detect corruption.

Integration tests hit a live deployed stack. They read credentials from Pulumi stack outputs and config at
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
