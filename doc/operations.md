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

## Hub updates and rebuilds

The hub uses a single-use Tailscale bootstrap key. Once the VM joins, that key is consumed;
the existing VM does not need it again. `pulumi up --refresh` observes an invalid key and
renews it without replacing the VM. Ordinary updates do not automatically refresh the key.

The VM ignores changes to the rendered `userData` so key renewal does not rebuild it.
A hash of the cloud-init template still triggers replacement when the script changes.

Replacing only the VM without observing or replacing an invalid key can cause Tailscale
bootstrap to fail. The VM creation hook prints a reminder during preview and update,
including an explicit recovery command with both resource URNs for the current stack.
The K3s installation error hook adds a similar hint on SSH connection failures while
preserving the original error and requesting no automatic retries. Neither hint confirms
that the key is invalid: check the runner's Tailscale connection and VM bootstrap logs too.
Use Pulumi CLI 3.268.0 or newer for these hooks. Validation used version 3.268.0.

For an intentional rebuild, explicitly replace both resources using their URNs from
`pulumi stack --show-urns`, or use the command printed by the hook:

```bash
pulumi preview --refresh --replace '<key-URN>' --replace '<server-URN>'
pulumi up --refresh --replace '<key-URN>' --replace '<server-URN>'
```

The normal dependency creates the fresh key before provisioning the new VM. If bootstrap
has already failed, replacing the key alone does not rerun cloud-init on that VM; replace
both resources on retry. A stack refresh observes state and an update performs the renewal;
refresh alone does not create a key. Review all dependent replacements and the normal
Postgres teardown requirements before rebuilding the hub.

## Updating Postgres configuration

`pulumi up` updates backup retention on the existing CNPG Cluster without recreating
Postgres. The provider patches only `spec.backup.retentionPolicy`, checks the value
read back from Kubernetes, and waits for a ready instance. A resource-version conflict,
API failure, or failed verification stops the update; retry after resolving the cause.
Retention controls cleanup after subsequent backups, so accepting the setting does not
mean old objects have already been deleted.

S3 key rotation updates the separately managed `postgres-backup-credentials` Secret.
The provider uses the new keys to list and read a completed base backup before accepting
them in its state. It does not create a backup or test S3 write/delete permissions. Keep
those permissions on the replacement keys and check archiving after deployment. If the
verification fails, the Secret may already have changed; Pulumi does not roll it back.
Restore working keys or correct their permissions before retrying.

Changing the archive bucket, S3 endpoint, or application database list on an existing
resource is rejected during input validation. Archive relocation needs a migration that
preserves the base backup and WAL chain. Database changes need explicit database and
role management; restoring the existing physical backup does not perform those changes.
Do not use forced replacement to bypass these restrictions.

Kubeconfig authentication or address changes are accepted only when the target CNPG
Cluster's Kubernetes UID matches the stored UID. Older state without a UID must establish
that both connections reach the same Cluster. Refresh and updates refuse to adopt an
unrelated Cluster with the same name.

`pulumi refresh` reads retention and archive location into outputs and inputs, so the next
preview can detect drift. Retention drift can be repaired by `pulumi up`. Archive location
or credential-reference drift requires inspection and explicit repair before continuing.
Refresh leaves database names and credential values unchanged; it does not inspect SQL
database contents or read the separately managed Secret. Only a Kubernetes 404 marks the
Cluster missing. Permission errors and connectivity failures stop refresh rather than
claiming the database is gone. A subsequent creation still uses the normal backup/recovery
checks.

`delete_before_replace=True` remains enabled because replacements share the fixed
Kubernetes name `infra/postgres`. It controls replacement order, not whether input changes
cause replacement. Supported changes update in place; replacement deletes and verifies
the recovery archive before creating the next Cluster.

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

The database policy provider discovers CNPG's current primary for every SQL command,
checks the command's exit status, and reads back the policy after setting or removing it.
SQL failures, command timeouts, invalid persisted YAML, and verification failures stop
the Pulumi operation. Removal is skipped only when a successful query confirms the table
or policy is absent. On deployment, a missing table is polled for up to five minutes.
Commands have a sixty-second execution limit after the exec connection is established;
table checks use the remaining polling time when it is shorter. SQL values travel over
stdin, and errors do not include potentially sensitive SQL output.

The provider compares the exact stored YAML before replacing the configuration row. If
another writer changed or deleted it after the read, the conditional write changes nothing;
the provider rereads and reapplies only the policy change to the latest settings. Creating
an absent row also leaves a concurrent insertion untouched. Both application and removal
allow at most five write attempts, then fail with a conflict error. Retry the Pulumi operation
when concurrent edits have finished. Write statements have a 45-second statement timeout
and a 10-second lock timeout; SQL errors and timeouts fail immediately rather than retrying.

Verification still checks the policy field after a successful write. This prevents our stale
snapshot from overwriting another writer's settings; another writer can still change the
row after our successful write or verification.

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

The MLflow test job uses its own uv project in `tests/jobs/`. Setup installs uv
0.13.0 and syncs the committed `uv.lock` into an isolated environment; `.python-version`
pins Python 3.14.8. Setup and execution use `--locked`, so an outdated or missing lockfile
fails rather than resolving new dependencies on the node. This affects only the test job.
The MLflow client version does not pin the deployed server's Helm chart or app version.

To update the job dependencies, edit `tests/jobs/pyproject.toml`, then regenerate and
review its lockfile separately from the infrastructure lockfile:

```bash
uv lock --project tests/jobs
uv sync --project tests/jobs --locked --no-dev
```

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
