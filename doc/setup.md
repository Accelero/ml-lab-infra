# Setup

Steps to install the required tools and obtain the credentials and external resources the stack needs before running `pulumi up`.

## Install tools

### Pulumi

```bash
curl -fsSL https://get.pulumi.com | sh
```

Or via a package manager:

```bash
brew install pulumi          # macOS
winget install pulumi        # Windows
```

After installing, log in to Pulumi Cloud (free for individuals — handles stack state and secret encryption):

```bash
pulumi login
```

See the [Pulumi installation docs](https://www.pulumi.com/docs/install/) for all options.

### uv

`uv` is used to manage the Python environment and run tests.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Or via a package manager:

```bash
brew install uv              # macOS
winget install astral-sh.uv  # Windows
```

See the [uv installation docs](https://docs.astral.sh/uv/getting-started/installation/) for all options.

## Hetzner Cloud

1. Create a project in the [Hetzner Cloud Console](https://console.hetzner.cloud)
2. Go to **Security > API Tokens** inside the project
3. Click **Generate API Token**, select **Read & Write**, and copy the token
4. Set it in the stack:

```bash
pulumi config set --secret hcloud:token <your-api-token>
```

### Server type and location

The defaults are `cx33` (4 vCPU, 8 GB RAM, €7.72/month) in `fsn1` (Falkenstein, Germany). To use
a different type or location:

```bash
pulumi config set hub_server:serverType cx33   # 4 vCPU, 8 GB RAM
pulumi config set hub_server:location fsn1     # Falkenstein
```

You can change the server type or location at any time by updating the config and running
`pulumi up`. Pulumi will replace the server, so make sure `pulumi down` has been run first to
preserve your data in S3 before the replacement. See the
[Hetzner server types](https://www.hetzner.com/cloud) page for available options.

## Tailscale OAuth client

The stack needs a Tailscale OAuth client to manage devices on your tailnet. A separate OAuth client for the Kubernetes operator and SkyPilot is provisioned automatically by Pulumi itself.

1. Go to `https://login.tailscale.com/admin/settings/oauth`
2. Click `+Credential`
3. Choose `OAuth`
4. Enable the following scopes:
   - `auth_keys` (write): create the hub-server auth key and keys for provisioned OAuth clients
   - `devices:core` (write): deregister devices on teardown
   - `oauth_keys` (write): create the K8s operator and SkyPilot OAuth clients
   - `acls` (write): manage the tailnet ACL policy
   - `settings` (write): enable HTTPS on the tailnet
5. Save the client ID and secret. Set them in the stack:

```bash
pulumi config set tailscale:oauthClientId <client-id>
pulumi config set --secret tailscale:oauthClientSecret <client-secret>
```

This credential authenticates the `pulumi_tailscale` provider for all Tailscale resources in the
stack (ACL policy, OAuth clients, tailnet settings) and is also used by `TailscaleDeviceCleanup`
to deregister devices on teardown. Verify scope names against the Tailscale OAuth docs if the
provider returns permission errors on first deploy.

### Tailnet name

Your tailnet name is shown in the Tailscale admin console under **DNS > Tailnet DNS name**. It looks
like `yourname.ts.net` for personal accounts or `yourorg.com` for business accounts.

```bash
pulumi config set tailscale:tailnet yourname.ts.net
```

## S3 bucket setup

Two buckets with separate credentials are strongly recommended: one for Postgres WAL backups, one for MLflow artifacts. Training jobs will need S3 credentials and write to artifact buckets from RunPod GPU nodes, and should not have access to your backups.

### Hetzner Object Storage

Hetzner Object Storage is the natural pairing: same datacenter as the VM, no egress fees between
Hetzner resources, and low latency. The plan is €6.49/month for the first TB regardless of the number of buckets.

1. Go to your Hetzner Cloud project, select **Object Storage**
2. Create two buckets (e.g., `my-ml-backups` and `my-ml-artifacts`) in the same location as your
   server (`fsn1`, `nbg1`, or `hel1`)
3. By default S3 access keys on Hetzner have access to all buckets of the same project. See the
   [Hetzner S3 credentials docs](https://docs.hetzner.com/storage/object-storage/faq/s3-credentials/#how-do-i-restrict-access-per-key) on how to scope access keys to specific buckets.
4. The endpoint follows the pattern `https://<location>.your-objectstorage.com`

### Backblaze B2

Backblaze B2 is another good option. Create one application key per bucket scoped to that bucket
only. Use the S3-compatible endpoint for your region.

## RunPod

RunPod is the default GPU provider. SkyPilot dispatches jobs to RunPod on demand and terminates
the instances when the job completes, so you only pay for actual compute time.

1. Create an account at [runpod.io](https://www.runpod.io)
2. Add a payment method under **Billing**. RunPod uses a prepaid credit system; add enough credits
   for your expected GPU usage.
3. Go to **Settings > API Keys** and click **+ API Key**
4. Set it in the stack:

```bash
pulumi config set --secret runpod:apiKey <your-api-key>
```

To use a different cloud provider instead, consult the
[SkyPilot provider docs](https://skypilot.readthedocs.io/en/latest/getting-started/installation.html)
and set the appropriate credentials.
