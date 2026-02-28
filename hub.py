"""The hub server configuration."""

import textwrap

import pulumi
import pulumi_hcloud as hcloud
import pulumi_tls as tls
import pulumi_command as command
from pathlib import Path
from tailscale import hub_auth_key

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

with Path("cloud-init.sh").open("r") as f:
    script_body = f.read()

user_data = hub_auth_key.key.apply(
    lambda key: script_body.replace("${TAILSCALE_AUTH_KEY}", key),
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
    user_data=user_data,
)

pyinfra_run = command.local.Command(
    "pyinfra-setup",
    # This triggers ONLY after the server is up and the IP is known
    create=pulumi.Output.all(
        hub_server.ipv4_address,
        hub_ssh_private_key.private_key_openssh,
    ).apply(
        lambda args: (
            f"python3 -c \"from my_pyinfra_file import setup_servers; setup_servers('{args[0]}', '{args[1]}')\""
        )
    ),
    # Ensure this runs after the firewall is open and server is ready
    opts=pulumi.ResourceOptions(depends_on=[hub_server, hub_firewall]),
)
