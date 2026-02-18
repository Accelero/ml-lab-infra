"""The hub server configuration."""

import pulumi
import pulumi_hcloud as hcloud
import pulumi_tls as tls


# Generate an SSH key pair for the hub server
hub_ssh_private_key = tls.PrivateKey(
    "hub-ssh-private-key",
    algorithm="ED25519",
)

hub_ssh_public_key = hub_ssh_private_key.public_key_openssh

hub_hcloud_ssh_key = hcloud.SshKey(
    "hub-ssh-key",
    name="hub-ssh-key",
    public_key=hub_ssh_public_key,
)

pulumi.export("hub_ssh_private_key", hub_ssh_private_key.private_key_pem)
pulumi.export("hub_ssh_public_key", hub_ssh_public_key)

# Create the hub server
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

hub_server = hcloud.Server(
    "hub",
    name="hub",
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
)
