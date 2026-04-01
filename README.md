# Personal ML Lab

A Pulumi project that provisions a personal ML lab on Hetzner Cloud. Fixed cost ~€15/month;
GPU compute is pay-as-you-go via SkyPilot. Designed for personal use or small trusted teams.

## Why

Big cloud providers are expensive and add complexity that doesn't make sense at small scale. High egress
costs also undercut cheap GPU providers like RunPod or vast.ai, since your data and compute end up
in different ecosystems and you pay to move between them.

This stack sidesteps that: a small Hetzner VM acts as a persistent control plane with a SkyPilot
server dispatching GPU jobs to whichever provider is cheapest at the time. Spot instances, multi-node
training clusters, or a single dev GPU, all via the same interface. MLflow tracks experiments
with a Postgres backend. Artifacts and training data live in your own S3 buckets with a low-egress
provider. Postgres is backed by WAL-streamed S3 backups and auto-restores when the stack is brought up.

Networking runs over Tailscale. No service exposes a public port; everything is reachable only
from devices on your tailnet, including SkyPilot nodes which get Tailscale auto-injected.
Adding a collaborator means adding them to the tailnet.

When not in use, `pulumi down` tears everything down and archives state to S3. `pulumi up`
restores it. Only storage costs accrue when the stack is down.

## Cost

| Component | Cost |
| --- | --- |
| Hetzner CX33 (4 vCPU, 8 GB RAM) | €7.72/month |
| S3 storage (1 TB) | €6.49/month |
| **Fixed total** | **~€14.21/month** |
| GPU compute (SkyPilot) | pay-as-you-go |

Any S3-compatible provider works. [Hetzner Object Storage](https://www.hetzner.com/storage/object-storage) and [Backblaze B2](https://www.backblaze.com/cloud-storage) are two good options.

## Prerequisites

- [ ] [Hetzner Cloud](https://www.hetzner.com/cloud) account with a project and API token
- [ ] Two S3-compatible buckets (one for Postgres backups, one for MLflow artifacts)
- [ ] [Tailscale](https://tailscale.com) account; your local machine must be connected to the tailnet
- [ ] A cloud provider account for GPU jobs. [RunPod](https://runpod.io) is configured out of the
  box; any provider supported by SkyPilot can be used instead.
- [ ] [`pulumi` CLI](https://www.pulumi.com/docs/install/) and [`uv`](https://docs.astral.sh/uv/getting-started/installation/) installed

See [Setup](doc/setup.md) for installation instructions and how to obtain each credential.

## Quick start

```bash
git clone https://github.com/Accelero/ml-lab-infra.git
cd ml-lab-infra
uv sync
pulumi login
pulumi stack init dev
```

Configure the stack following [Setup](doc/setup.md), then deploy (make sure your machine is on your Tailscale tailnet before running):

```bash
pulumi up
```

First deploy takes about 5 minutes: provisions the Hetzner VM, installs K3s over Tailscale, and
deploys all Helm releases.

## Accessing services

Both services require Tailscale. Open in your browser:

- **MLflow**: `https://mlflow.<tailnet>.ts.net`
- **SkyPilot**: `https://skypilot.<tailnet>.ts.net`

```bash
sky api login --endpoint https://skypilot.<tailnet>.ts.net
```

## Teardown and restore

```bash
pulumi down   # archives final WAL to S3, then destroys all cloud resources
pulumi up     # detects the backup in S3 and restores automatically
```

Only S3 billing continues between teardown and restore. All experiment and job history is preserved.

## Documentation

- [Setup](doc/setup.md): tool installation, Tailscale OAuth client, Hetzner server config, S3 buckets
- [Configuration reference](doc/configuration.md): all config variables, sample stack file
- [Architecture](doc/architecture.md): layers, networking model, backup flow, SkyPilot Tailscale injection
- [Operations](doc/operations.md): deploy, teardown, restore, integration tests, manual backup
