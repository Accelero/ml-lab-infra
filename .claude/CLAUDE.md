# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

- Don't use em dashes or en dashes as sentence separators in prose or comments. Use a period or restructure the sentence instead.
- Run `ruff check --select ALL` at the end of every editing loop. Fix warnings by changing code, not by suppressing them.
- To run kubectl commands, keep the kubeconfig in memory — never write it to disk. Fetch it into a shell variable and pass it via process substitution, chaining all kubectl calls in a single Bash invocation:

  ```bash
  KC=$(pulumi stack output hub_kubeconfig --show-secrets) && \
  kubectl --kubeconfig <(echo "$KC") get pods
  ```

## Commands

**Package management:** this project uses `uv`. Install dependencies with `uv sync`.

**Linting:**
```bash
uv run ruff check --select ALL
uv run ruff format --check
```

**Tests** (integration tests, require a live cluster):
```bash
uv run pytest                         # all tests
uv run pytest tests/test_postgres_backup_restore.py  # single file
uv run pytest -k "test_name"          # single test
```

**Infrastructure:**

```bash
pulumi up    # deploy / apply changes
pulumi down  # teardown (triggers Postgres WAL archival before removing cluster)
```

## Architecture

This is a Pulumi (Python) project that provisions a personal ML lab on Hetzner Cloud. The stack brings up a single K3s node accessible over Tailscale, then deploys MLflow and SkyPilot onto it backed by CloudNative PG (CNPG) Postgres.

### Layer order (each depends on the previous)

1. **`infrastructure/hub_server.py`** — Hetzner VM, ED25519 SSH key, Tailscale auth key, firewall. The cloud-init script (`scripts/cloud-init.sh`) bootstraps Tailscale and swap.
2. **`infrastructure/hub_cluster.py`** — Installs K3s over SSH, bound to the Tailscale interface. Fetches and patches the kubeconfig (localhost → Tailscale IP). The patched kubeconfig is exported as a Pulumi stack output.
3. **`infrastructure/tailscale.py`** — Creates Tailscale OAuth clients (for the k8s operator and SkyPilot) and sets the ACL policy that governs which tags can reach which ports.
4. **`infrastructure/apps.py`** — All Helm releases and Kubernetes manifests: namespaces, network policies, CNPG operator, Postgres cluster, MLflow, SkyPilot.

### Custom Pulumi dynamic resources (`resources/`)

These implement stateful operations that Pulumi's built-in providers don't cover:

- **`postgres_cluster.py` — `PostgresCluster`**: On create, checks S3 for an existing barman backup; bootstraps via recovery if found, initdb otherwise. On delete, waits for CNPG to complete WAL archival before allowing Pulumi to proceed. Handles orphaned/failed clusters by deleting and retrying.
- **`skypilot_config.py` — `SkyPilotAdminPolicy`**: Polls until SkyPilot has created the `config_yaml` table in Postgres, then UPSERTs the admin policy directly into that table and forces a pod rollout via a checksum annotation.
- **`ts_device_cleanup.py` — `TailscaleDeviceCleanup`**: Exchanges OAuth credentials for an API token, then removes the node from the tailnet on `pulumi down`.

### Networking / access model

Tailscale ACLs (configured in `infrastructure/tailscale.py`) enforce least-privilege between tagged devices:
- `k8s-operator` → k8s API + internet (for HTTPS cert issuance)
- `k8s` ↔ `k8s` (pod/service mesh)
- `hub-server` → k8s + operator
- `skypilot-server` → hub-server + k8s
- `skypilot-nodes` → k8s
- `admins` → everything
- `members` → k8s services only

Kubernetes network policies mirror this with default-deny + explicit allow rules.

### Postgres backup / restore

CNPG streams WAL to an S3-compatible bucket (barman, gzip). Scheduled full backups run at 03:00 UTC; `pulumi down` triggers a final archival and waits for pod termination before proceeding. On the next `pulumi up`, `PostgresCluster.create()` detects the backup and restores automatically.

### SkyPilot admin policy

`scripts/skypilot_policies.py` defines policy classes (subclass `AdminPolicy` to add new policies). The active policy is assembled into a YAML string and persisted in Postgres by `SkyPilotAdminPolicy`. Changing the policy in code and running `pulumi up` re-applies it without manual DB access.

### Stack config

Secrets (Tailscale OAuth, Hetzner token, S3 credentials, RunPod API key) live in `Pulumi.*.yaml` encrypted by the Pulumi stack passphrase. Namespace prefixes used in config:

- `tailscale:` — tailnet, oauthClientId/Secret
- `hcloud:` — token
- `hub_server:` — serverType, location
- `backup:` / `mlflow:` — S3 endpoint, bucket, access/secret keys
- `runpod:` — apiKey
