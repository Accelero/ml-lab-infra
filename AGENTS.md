# Repository guidance

## Overview and navigation

This is a Python 3.14+ Pulumi project, managed with `uv`. It provisions a single
Hetzner VM running K3s, with MLflow and SkyPilot backed by CloudNativePG Postgres.
Service access is through Tailscale. Postgres backups and MLflow artifacts use
separate, externally provisioned S3-compatible buckets; SkyPilot dispatches GPU
jobs to RunPod.

- `__main__.py`: Pulumi entry point. Infrastructure modules register resources
  when imported; avoid importing them in isolated tests without Pulumi mocks.
- `infrastructure/hub_server.py`: VM, SSH identity, firewall, Tailscale bootstrap.
  `scripts/cloud-init.sh` joins the tailnet and configures swap.
- `infrastructure/hub_cluster.py`: K3s installation, secret kubeconfig output,
  Kubernetes provider. K3s binds to the Tailscale interface; the exported
  `hub_kubeconfig` replaces localhost with the server's Tailscale IP.
- `resources/k3s_version.py`: validated channel resolution. The project default
  tracks `stable` in `Pulumi.yaml`; stack config can override it. Keep the resolved
  version in the installation trigger and use `INSTALL_K3S_VERSION`.
- `infrastructure/tailscale.py`: tailnet ACL, settings, workload OAuth clients.
- `infrastructure/apps.py`: namespaces, network policies, Helm releases,
  credentials, Postgres and application wiring. `mlflowChartVersion` and
  `skypilotChartVersion` accept exact chart versions or `stable`; project defaults
  track stable charts. `resources/helm_version.py` validates pins and maps stable
  to Helm's non-prerelease constraint. Chart versions differ from app versions.
- `resources/postgres_cluster.py`: `PostgresCluster`, the dynamic provider for
  CNPG creation, recovery, updates, refresh, and verified teardown.
  `resources/postgres_config.py` validates inputs and reads managed configuration.
  `resources/postgres_exec.py` runs checked SQL on CNPG's current primary; share
  this executor with providers that need database access.
- `resources/skypilot_config.py`: `SkyPilotAdminPolicy`, which upserts the
  `admin_policy` field in `config_yaml` under `api_server_config`. SkyPilot's
  persisted config overrides Helm values, so update the database through this
  provider. Writes and deletion verify the resulting policy field. Preserve
  command failure handling, YAML mapping validation, and stdin parameter values.
- `resources/ts_device_cleanup.py`: `TailscaleDeviceCleanup`, which obtains an
  OAuth token and deregisters the matching device during teardown.
- `scripts/`: cloud-init and SkyPilot policy/Tailscale setup. Policy files are
  mounted through a ConfigMap; their checksum triggers a SkyPilot rollout.
  Extend `scripts/skypilot_policies.py` by adding a `sky.AdminPolicy` subclass
  to `SkyPilotAdminPolicy._policies`; `pulumi up` reapplies the policy.
- `tests/unit/`: isolated archive and lifecycle regression tests.
- `tests/`: live integration tests and GPU job payloads, with shared fixtures in
  `tests/conftest.py`.
- `doc/`: [architecture](doc/architecture.md), [configuration](doc/configuration.md),
  [setup](doc/setup.md), and [operations](doc/operations.md). Read the relevant
  guide before changing that area; verify behavior against the implementation.

## Development and required checks

Run commands from the repository root. Use `uv sync` to install dependencies and
`uv run` for Python tooling. `Pulumi.yaml` also uses the `uv` toolchain.

After every editing loop, run:

```bash
uv run ruff check .
uv run ruff format --check .
```

Ruff selects `ALL` in `pyproject.toml`. Fix every lint finding and formatting
failure before committing, including existing findings. Both commands must exit
successfully on the whole repository after the final edit. If checks cannot run
or failures remain, report the blocker and do not commit.

Use `uv run ruff check --fix .` for safe automatic fixes and `uv run ruff format .`
for formatting, then review the diff and rerun both checks. Fix remaining findings
in code. Do not weaken Ruff configuration, add exclusions or `noqa` directives,
or use unsafe fixes merely to obtain a passing check.

- Keep changes focused; preserve unrelated user edits.
- Add types to new or changed interfaces and keep external operations bounded
  with explicit error handling and timeouts.
- Add meaningful regression coverage for behavior changes. Use isolated tests
  with mocked external APIs where practical.
- Update the relevant documentation when config or operational behavior changes.
- Avoid em dashes and en dashes as sentence separators in prose and comments.
- Before committing, run `git diff --check` and review the staged diff for
  unrelated changes and secrets. Report checks run and any untested behavior.

## Infrastructure and secrets

- Preserve Tailscale service access and least-privilege controls. The Hetzner
  firewall currently allows inbound UDP 41641; Kubernetes policies deny ingress
  by default in `app` and `infra`. They do not currently deny egress.
- Tailnet grants currently allow the operator to reach `tag:k8s` and the
  internet, `tag:k8s` peers to communicate, the hub to reach Kubernetes and the
  operator, and SkyPilot's provisioning identity to reach the hub and proxies.
  Workers use `tag:skypilot-node` and can reach only `tag:mlflow` and
  `tag:skypilot-api` on TCP 443. Both ingress proxies retain `tag:k8s` for member
  and service access; admins can reach everything. Other grants allow all
  protocols/ports. Preserve the policy tests in `infrastructure/tailscale.py` and
  the operator's dependency on the ACL. Public S3 access uses internet egress.
- Preserve Pulumi resource names, parents, providers, and dependency ordering.
  Renaming or reparenting resources can cause replacement; inspect the preview
  and provide aliases when retaining existing resource identity.
- Postgres creation uses completed S3 base backups or completes an initial base
  before deploying applications. Teardown takes no new base backup: it blocks
  application writes, switches WAL, and verifies complete S3 coverage before
  deletion. Preserve fail-closed waits, the retry checkpoint, and dependencies
  that keep S3 credentials available. Archive parsing and database operations
  live in `resources/postgres_archive.py` and `resources/postgres_lifecycle.py`.
  WAL compression is gzip; scheduled base backups run daily at 03:00 UTC.
  Retention updates patch the live Cluster. Reject implicit archive relocation or
  database-list changes; do not substitute automatic replacement for a migration.
  Preserve Cluster UID checks, secret outputs, and deletion-before-replacement
  ordering for the fixed `infra/postgres` name. Refresh observes retention and
  archive location; database contents and Secret values remain separately managed.
- Use Pulumi secret config and keep secret values marked through outputs and
  dynamic resource properties. Never log or commit plaintext credentials,
  private keys, kubeconfigs, or decrypted stack state.
  Stack secrets are encrypted in `Pulumi.<stack>.yaml`. Config namespaces are
  `tailscale`, `hcloud`, `hub_server`, `backup`, `mlflow`, and `runpod`.
- Keep kubeconfig in memory. For shell access, capture the secret stack output
  in a Bash variable and pass it to `kubectl` via process substitution in the
  same invocation. Do not write it to disk or print it to tool output.
- Confirm the selected stack and Tailscale connectivity before live operations.
  Use `pulumi preview` to review infrastructure changes, accounting for network
  access during program evaluation. Run `pulumi up`, teardown, state edits, or
  resource replacement only within an explicitly authorized operational task.
  The ACL resource overwrites the tailnet policy, so its scope extends beyond
  this VM.

For authorized cluster inspection, keep kubeconfig in one shell invocation:

```bash
KC=$(pulumi stack output hub_kubeconfig --show-secrets) && \
kubectl --kubeconfig <(printf '%s' "$KC") get pods -A
```

## Tests

Run isolated tests for provider changes:

```bash
uv run python -m pytest tests/unit -q
```

The integration suite requires a deployed stack, Pulumi credentials/config, and
Tailscale connectivity. It mutates live databases, creates backups and recovery
clusters, and can incur GPU charges. Run it only when live testing is authorized:

```bash
uv run pytest tests/ -v
```

Choose relevant files for targeted validation: `test_postgres_backup_restore.py`
checks backup/recovery, `test_skypilot_admin_policy.py` temporarily modifies the
SkyPilot config row, and `test_skypilot_mlflow.py` launches a paid RunPod GPU job.
Ensure cleanup completes and report skipped live checks. A documentation-only
change does not require live infrastructure tests.
Use a test file path or `-k '<test_name>'` to select relevant tests.
