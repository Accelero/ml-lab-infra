# Configuration Reference

All configuration lives in your Pulumi stack file (`Pulumi.<stack>.yaml`) and is set via
`pulumi config set`. Secrets are encrypted at rest by Pulumi using your stack passphrase or Pulumi
Cloud's managed encryption.

See [Setup](setup.md) for how to obtain each credential and create the required external resources.

## Config variable reference

### `hcloud`

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `hcloud:token` | yes | (required) | Hetzner Cloud API token. Create one under **Security > API Tokens** in your Hetzner Cloud project. |

### `hub_server`

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `hub_server:serverType` | no | `cx33` | Hetzner server type. CX33 provides 4 vCPU and 8 GB RAM. |
| `hub_server:location` | no | `fsn1` | Hetzner datacenter location. `fsn1` is Falkenstein, Germany. |

To use a different server type or location:

```bash
pulumi config set hub_server:serverType cx43
pulumi config set hub_server:location nbg1
```

### `tailscale`

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `tailscale:tailnet` | no | (required) | Your Tailscale tailnet name, e.g. `yourname.ts.net`. |
| `tailscale:oauthClientId` | no | (required) | OAuth client ID from the Tailscale admin console. |
| `tailscale:oauthClientSecret` | yes | (required) | OAuth client secret. |

### `backup`

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `backup:s3Endpoint` | no | (required) | S3 endpoint URL, e.g. `https://fsn1.your-objectstorage.com`. |
| `backup:s3BucketName` | no | (required) | S3 bucket name for Postgres WAL backups. |
| `backup:s3AccessKey` | yes | (required) | S3 access key ID. |
| `backup:s3SecretKey` | yes | (required) | S3 secret access key. |
| `backup:retentionPolicy` | no | `1w` | Barman retention policy. Accepts duration strings like `1w`, `30d`, `2w`. |

Retention changes apply in place through `pulumi up`. On an existing Postgres resource,
bucket and endpoint changes require an explicit archive migration and are rejected by
the provider. Key rotation updates the Kubernetes Secret and verifies archive read
access. See [Postgres configuration updates](operations.md#updating-postgres-configuration)
for verification limits, drift handling, and migration restrictions.

### `mlflow`

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `mlflow:s3Endpoint` | no | (required) | S3 endpoint URL for MLflow artifact storage. |
| `mlflow:s3BucketName` | no | (required) | S3 bucket name for MLflow artifacts. |
| `mlflow:s3AccessKey` | yes | (required) | S3 access key ID. |
| `mlflow:s3SecretKey` | yes | (required) | S3 secret access key. |

### `runpod`

SkyPilot supports many cloud providers. RunPod is configured out of the box; consult the
[SkyPilot docs](https://skypilot.readthedocs.io/en/latest/getting-started/installation.html) to
use a different provider instead.

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `runpod:apiKey` | yes | (required) | RunPod API key. Found at `https://www.runpod.io/console/user/settings` under **API Keys**. |

### Global (optional)

| Key | Secret | Default | Description |
| --- | --- | --- | --- |
| `k3sVersion` | no | `v1.37.1+k3s1` | Project default pinned in `Pulumi.yaml`. Accepts an exact release or an official channel (`stable`, `latest`, `testing`, or a minor channel such as `v1.37`). Channels resolve during Pulumi evaluation and their resolved release participates in the installation trigger. Stack configuration overrides the project default. |

Pinned versions require no channel lookup and remain unchanged until configuration is edited.
Channel resolution has a ten-second request timeout and rejects unexpected HTTP responses or
redirects. The installer always receives the resolved release through `INSTALL_K3S_VERSION`.
The `k3s_version` stack output reports the selected release; it does not independently verify the
version running on the node.

To pin an existing stack that overrides the project default:

```bash
pulumi config set k3sVersion v1.37.1+k3s1
```

Use `pulumi preview` before upgrading. For an existing cluster, move through consecutive minor
versions and check operator and chart compatibility. The project pin is the newest non-prerelease
as of 2026-10-09; it is not a floating `latest` channel.

## Sample Pulumi.dev.yaml

The following shows what a complete stack config file looks like after running all `pulumi config
set` commands. Pulumi writes this file automatically. Secrets are encrypted at rest and safe to
commit, though check the config before pushing to public repositories.

```yaml
config:
  # Hetzner
  hcloud:token:
    secure: <encrypted-by-pulumi>
  hub_server:serverType: cx33   # optional; default shown
  hub_server:location: fsn1     # optional; default shown

  # K3s version (optional; project default is pinned)
  # ml-lab-infra:k3sVersion: v1.37.1+k3s1

  # Tailscale
  tailscale:tailnet: yourname.ts.net
  tailscale:oauthClientId: tskey-client-xxxxxxxxxxxxxxxxxxxx
  tailscale:oauthClientSecret:
    secure: <encrypted-by-pulumi>

  # Postgres backup bucket
  backup:s3Endpoint: https://fsn1.your-objectstorage.com
  backup:s3BucketName: my-ml-backups
  backup:s3AccessKey:
    secure: <encrypted-by-pulumi>
  backup:s3SecretKey:
    secure: <encrypted-by-pulumi>
  backup:retentionPolicy: 1w

  # MLflow artifact bucket
  mlflow:s3Endpoint: https://fsn1.your-objectstorage.com
  mlflow:s3BucketName: my-ml-artifacts
  mlflow:s3AccessKey:
    secure: <encrypted-by-pulumi>
  mlflow:s3SecretKey:
    secure: <encrypted-by-pulumi>

  # RunPod
  runpod:apiKey:
    secure: <encrypted-by-pulumi>
```
