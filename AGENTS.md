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
- `infrastructure/hub_cluster.py`: K3s installation, secret kubeconfig output,
  Kubernetes provider.
- `infrastructure/tailscale.py`: tailnet ACL, settings, workload OAuth clients.
- `infrastructure/apps.py`: namespaces, network policies, Helm releases,
  credentials, Postgres and application wiring.
- `resources/`: dynamic providers for Postgres create/restore/teardown,
  SkyPilot's database-persisted admin policy, and Tailscale device cleanup.
- `scripts/`: cloud-init and SkyPilot policy/Tailscale setup. Policy files are
  mounted through a ConfigMap; their checksum triggers a SkyPilot rollout.
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
- Preserve Pulumi resource names, parents, providers, and dependency ordering.
  Renaming or reparenting resources can cause replacement; inspect the preview
  and provide aliases when retaining existing resource identity.
- Postgres creation uses completed S3 base backups or completes an initial base
  before deploying applications. Teardown takes no new base backup: it blocks
  application writes, switches WAL, and verifies complete S3 coverage before
  deletion. Preserve fail-closed waits, the retry checkpoint, and dependencies
  that keep S3 credentials available. Archive parsing and database operations
  live in `resources/postgres_archive.py` and `resources/postgres_lifecycle.py`.
- Use Pulumi secret config and keep secret values marked through outputs and
  dynamic resource properties. Never log or commit plaintext credentials,
  private keys, kubeconfigs, or decrypted stack state.
- Keep kubeconfig in memory. For shell access, capture the secret stack output
  in a Bash variable and pass it to `kubectl` via process substitution in the
  same invocation. Do not write it to disk or print it to tool output.
- Confirm the selected stack and Tailscale connectivity before live operations.
  Use `pulumi preview` to review infrastructure changes, accounting for network
  access during program evaluation. Run `pulumi up`, teardown, state edits, or
  resource replacement only within an explicitly authorized operational task.
  The ACL resource overwrites the tailnet policy, so its scope extends beyond
  this VM.

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
