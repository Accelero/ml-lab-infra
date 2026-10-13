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
Cleanup requests have a ten-second timeout and close their HTTP responses. Failures
produce warnings without external error contents; they do not block Pulumi teardown.
Check the tailnet manually when cleanup reports a failure.

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

Tests use pytest and separate unit, integration, and e2e directories. Plain
`uv run pytest` runs units and the local HTTP integration tests; Docker, live
stack, GPU, and destructive lifecycle tests skip before their fixtures execute.
CI runs units and local integrations, including Docker, without live credentials.

Isolated lifecycle, archive, and cleanup regressions require no deployed infrastructure:

```bash
uv run python -m pytest tests/unit -q
```

Local integrations exercise the production SQL against PostgreSQL 17.9 and the
RunPod client/watchdog against a local HTTP server. The PostgreSQL container is
uniquely named, has no published ports or external network, and is removed in a
finalizer. Docker must be available; its image is downloaded if absent.

```bash
uv run pytest tests/integration --run-docker -q
uv run pytest --run-docker --cov --cov-report=term-missing
```

Local tests cover conditional policy updates, a real competing row lock,
malformed YAML, database connection gates, checked SQL failure, statement
timeouts, watchdog lease expiry, late pods, parent-process death, and provider
deletion failures. Coverage measures production modules, including deployment
modules that are not exercised by local tests; it is not an e2e coverage score.

### Development-stack prerequisites

Live tests require a configured, disposable development stack with its own
database archive and artifact buckets, a separate Hetzner project, and an isolated
tailnet. They must not share production state or archives. Authenticate Pulumi and
connect the runner to Tailscale first. Every Pulumi invocation includes `--stack NAME`;
preflight resolves that stack and requires the local Tailscale client to be
running. Kubeconfigs and credentials stay in memory.

Supplying `--dev-stack NAME` opts into live integration tests. E2e additionally
requires `--run-e2e`. GPU and stack destruction each require their own option:

```bash
uv run pytest tests/integration -m live --dev-stack dev -v
uv run pytest tests/e2e/test_applications.py --dev-stack dev --run-e2e -v
uv run pytest tests/e2e/test_skypilot_mlflow.py --dev-stack dev --run-e2e --run-gpu -v
uv run pytest tests/e2e/test_stack_lifecycle.py --dev-stack dev --run-e2e --run-lifecycle -v
```

### Fixture scopes and fresh managed runs

`dev_stack` is session-scoped. Without `--manage-stack`, it uses an already
deployed development stack and leaves that deployment running. Kubernetes clients,
backup probes, recovery clusters, policy snapshots, MLflow experiments, and GPU
invocations are function-scoped. Their teardown completes before the stack's
session teardown. Each test reads the current kubeconfig so clients do not survive
a destroy/recreate cycle. Live tests reject pytest-xdist parallel execution because
the fixed stack and service identities are shared.

To let pytest build and destroy the whole stack once per run, initialize/configure
the dev stack and pre-create its two empty dedicated buckets, then opt in:

```bash
uv run pytest tests/integration tests/e2e --dev-stack dev --manage-stack --run-e2e --run-gpu --run-lifecycle -v
```

Omit `--run-gpu` or `--run-lifecycle` to skip those tests. The managed fixture:

1. Refuses stacks with deployed resources or pending operations, and buckets with
   current objects, old versions, delete markers, or incomplete multipart uploads.
2. Claims each empty bucket with a unique ownership marker using a conditional
   PUT, previews, then deploys once. Cleanup is registered before deployment.
3. Keeps the database archive intact across the recovery test's intermediate
   destroy/recreate operation.
4. After dependent fixtures finish, destroys the stack and checks its exported
   state is empty before purging either bucket.
5. Deletes owned objects, all versions and delete markers, and incomplete uploads;
   removes the ownership marker last; then verifies both buckets are empty.

The externally created bucket containers and the configured Pulumi stack remain
available for the next run. Managed mode refuses an existing deployment; it never
empties an active stack's archive at startup. If final stack deletion fails, the
archive and ownership markers remain available for recovery. Bucket cleanup errors
fail teardown, and both buckets get a cleanup attempt. `cleanup.json` under pytest's
`stack-session*` temporary directory records the stack, bucket names, ownership ID,
and whether cleanup was verified, without credentials or stack state.

Preview, deployment, and destruction have 600, 1800, and 1200 second command limits.
S3 cleanup has a 600 second loop deadline per bucket, with 5 second connect and
15 second read limits and one SDK attempt per request. Managed live tests apply
pytest's function timeout to test bodies throughout the session; fixture operations retain their own
explicit deadlines so a short assertion timeout cannot interrupt session teardown.
GPU fixture provisioning and termination remain bounded by the independent lease.
The session fixture cannot survive loss of the runner host; the journal supports
manual recovery, and the GPU watchdog still handles pytest process death.

Storage credentials for these tests need version listing/deletion and multipart
listing/abort permissions as well as normal object access. S3-compatible providers
must support conditional PUT and the version/multipart APIs. See
[S3 version inventories](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html)
and [version deletion](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObjects.html).
Provider compatibility still needs development-stack validation.

### Live integration and draft e2e coverage

- **`integration/test_postgres_backup_restore.py`** writes unique sentinels before
  and after a new base backup, verifies its archive and WAL, restores that exact
  backup into a temporary CNPG cluster, and checks both rows and database identity.
  Cleanup is registered before setup, uses checked SQL on the current primary,
  and waits for the recovery Cluster and pods to disappear. Archive objects remain
  subject to normal retention. Run this periodically: inventory checks alone do
  not detect backup corruption.
- **`integration/test_skypilot_admin_policy.py`** verifies the configured policy
  and conditional database updates while restoring only the original policy field.
- **`e2e/test_applications.py`** checks the deployed SkyPilot API and a complete
  MLflow parameter, metric-history, and artifact-content round trip over Tailscale.
  The shared MLflow fixture waits for HTTPS ingress readiness for up to 120 seconds
  of retries, with a ten-second request timeout, before creating test metadata.
- **`e2e/test_skypilot_mlflow.py`** provisions one cheap RunPod GPU through SkyPilot,
  checks that CUDA sees exactly one GPU, and writes MLflow parameters, metrics, and
  an artifact from the worker. It checks the exact invocation's finished run and
  downloaded bytes, and verifies hardware release directly through RunPod. The GPU
  fixture finishes hardware cleanup before returning the invocation to assertions.
- **`e2e/test_stack_lifecycle.py`** writes application data, previews, destroys,
  and recreates the development stack. It checks recovered PostgreSQL identity,
  persisted admin policy, MLflow data/artifacts, fresh writes, and a preview without
  changes. Failed recreation triggers one bounded restoration attempt. It is
  destructive and can incur VM costs even without GPU tests.

The new live fixtures and e2e tests are drafts pending validation on a development
stack. The local suite does not prove deployed chart compatibility, tailnet access,
RunPod availability, cloud cleanup, or full recovery behavior. Managed mode starts
from an empty archive. Managed-job history recovery and negative network-policy
checks remain future e2e additions. MLflow experiment fixtures soft-delete metadata
and physically remove the exact experiment's S3 artifact directory, including old
versions and unfinished uploads. Unexpected artifact locations fail closed. A
managed run removes all test database metadata with final stack destruction.

### GPU budget and cleanup

The job requests one GPU from the candidate list in `tests/jobs/mlflow_train.yaml`,
a 20 GiB disk, and `max_hourly_cost: 0.50` (USD/hour). Its `infra` setting restricts
provisioning to Iceland's `EUR-IS-1` data center (`runpod/IS/EUR-IS-1`); SkyPilot
does not fall back to another country or data center. The candidates use SkyPilot
accelerator names, which its provisioner maps to RunPod GPU type IDs. SkyPilot
filters out candidates whose catalog prices exceed the cap.
Setup and workload limits are 240 and 120 seconds. No provisioning retries are
requested; SkyPilot idle termination is also enabled. See the
[SkyPilot resource budget field](https://docs.skypilot.ai/en/latest/reference/yaml-spec.html#resources-max-hourly-cost).

An independent watchdog arms before launch. Its default lease is 600 seconds for
provisioning plus execution, then up to 180 seconds for cleanup. Lease validation
rejects longer runtime and rates above $0.50/hour; the test uses $0.50/hour. The
watchdog checks the provider's reported price and rejects unknown/over-budget
prices, multiple GPUs or pods, and network volumes. The hourly price is a rate
limit, not a total invoice cap; provider billing and storage charges still apply.

Each invocation has a unique `mltest-<16 hex characters>` SkyPilot cluster name.
The watchdog matches only that cluster's head/worker naming pattern and remembers
observed IDs even if renamed. Cleanup cancels the exact outstanding SkyPilot
request, requests `sky.down`, and repeatedly calls the RunPod
[pod deletion API](https://docs.runpod.io/api-reference/pods/DELETE/pods/podId).
It checks fresh [pod inventories](https://docs.runpod.io/api-reference/pods/GET/pods)
until owned pods have been absent for 60 seconds, catching late provisioning
results. An HTTP delete success alone does not pass the test. Unrelated pods are
left untouched. Credentials reach subprocesses through stdin and are omitted from
the cleanup journal and command failure output.

The watchdog continues after pytest timeout or parent-process exit. It writes
`runpod-cleanup.json` in the test's temporary directory with observed pod IDs,
cleanup status, and a redacted failure reason. An unverified release fails the
test. Worker failures also write `sky-worker-error.json` or
`sky-worker-cleanup-error.json` with the failure phase, exception class, and a
categorized reason. These summaries omit raw error text and request data.
No local process can guarantee release during a provider outage or loss of
the runner host; in that case, use the journal's IDs to terminate the pods through
RunPod directly and verify their absence. During initial development-stack
validation, confirm the installed SkyPilot version's pod naming and cancellation
behavior, and check the RunPod console after both a successful job and a forced
timeout. A dedicated test RunPod account/key keeps recovery from affecting other
workloads.

Repeated managed runs also recreate the Tailscale HTTPS proxies and their
certificate caches. Let’s Encrypt limits new certificates for the same hostname
set to five per seven days. If proxy logs report an ACME rate limit, wait until
the reported retry time before rebuilding the same service identities. HTTPS
readiness retries cannot bypass this limit, and the GPU fixture cannot start
until MLflow is reachable. See [Let’s Encrypt certificate limits](https://letsencrypt.org/docs/rate-limits/#new-certificates-per-exact-set-of-identifiers).

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
