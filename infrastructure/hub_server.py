"""The hub server configuration."""

from pathlib import Path

import pulumi
import pulumi_hcloud as hcloud
import pulumi_tls as tls
import pulumi_tailscale as tailscale
from resources import TailscaleDeviceCleanup

# Generate an SSH key pair for the hub server
hub_ssh_key = tls.PrivateKey(
    "hub-ssh-private-key",
    algorithm="ED25519",
)
hub_ssh_private_key = hub_ssh_key.private_key_openssh
hub_ssh_public_key = hub_ssh_key.public_key_openssh

hub_hcloud_ssh_key = hcloud.SshKey(
    "hub-ssh-key",
    name="hub-ssh-key",
    public_key=hub_ssh_public_key,
)

pulumi.export("hub_ssh_private_key", hub_ssh_private_key)
pulumi.export("hub_ssh_public_key", hub_ssh_public_key)

# Generate a Tailscale auth key for the hub server
hub_ts_auth_key = tailscale.TailnetKey(
    "hub-auth-key",
    reusable=False,
    ephemeral=False,
    preauthorized=True,
    expiry=3600,  # in seconds
    tags=["tag:hub-server"],
    description="Provisioning key for hub server",
    recreate_if_invalid="always",
)

# Create Firewall for the hub server
hub_firewall = hcloud.Firewall(
    "hub-firewall",
    name="hub-firewall",
    # Allow Tailscale UDP for peer communication
    rules=[
        {
            "direction": "in",
            "protocol": "udp",
            "port": "41641",
            "source_ips": ["0.0.0.0/0", "::/0"],
        },
    ],
)

# Create the hub server
with Path("scripts/cloud-init.sh").open("r") as f:
    script_body = f.read()

user_data = hub_ts_auth_key.key.apply(
    lambda key: script_body.replace("${TAILSCALE_AUTH_KEY}", key),
)

hub_server = hcloud.Server(
    "hub-server",
    name="hub-server",
    image="debian-13",
    server_type="cx23",
    location="nbg1",
    ssh_keys=[hub_hcloud_ssh_key.id],
    firewall_ids=[hub_firewall.id],
    public_nets=[
        {
            "ipv4_enabled": True,
            "ipv6_enabled": True,
        },
    ],
    user_data=user_data,
)

hub_server_ts_cleanup = TailscaleDeviceCleanup(
    "hub-server-ts-cleanup",
    hostname=hub_server.name,
    opts=pulumi.ResourceOptions(
        delete_before_replace=True,
        replacement_trigger=[hub_server.id],
    ),
)
