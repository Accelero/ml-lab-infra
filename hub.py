"""The hub server configuration."""

import base64
import json
import urllib.request
from pathlib import Path

import pulumi
import pulumi_command as command
import pulumi_hcloud as hcloud
import pulumi_tailscale as tailscale
import pulumi_tls as tls

from tailscale import hub_auth_key

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
    "hub-server",
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

import pulumi
import pulumi.dynamic
import base64
import json
import urllib.request
import asyncio


class _TailscaleNodeWatcherProvider(pulumi.dynamic.ResourceProvider):
    def _ensure_loop(self):
        """
        Force-initializes an event loop for the current worker thread.
        This handles the Pulumi 'no current event loop' error in ThreadPoolExecutors.
        """
        try:
            # Check if a loop already exists and is working
            asyncio.get_event_loop()
        except RuntimeError:
            # If no loop exists, create a new one
            loop = asyncio.new_event_loop()
            # Set it as the loop for this specific thread
            asyncio.set_event_loop(loop)

    def create(self, props):
        self._ensure_loop()
        # Ensure we return 'props' as 'outs' so they persist for the delete phase
        return pulumi.dynamic.CreateResult(id_="ts-cleanup", outs=props)

    def delete(self, id, props):
        self._ensure_loop()

        # Pull properties (from the 'outs' saved during create)
        client_id = props.get("clientId")
        client_secret = props.get("clientSecret")
        tailnet = props.get("tailnet")
        target_hostname = props.get("hostname")

        if not all([client_id, client_secret, tailnet, target_hostname]):
            return

        try:
            # 1. OAuth
            auth = f"{client_id}:{client_secret}"
            b64 = base64.b64encode(auth.encode()).decode()
            token_req = urllib.request.Request(
                "https://api.tailscale.com/api/v2/oauth/token",
                data=b"grant_type=client_credentials",
                headers={
                    "Authorization": f"Basic {b64}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(token_req) as resp:
                token = json.load(resp)["access_token"]

            # 2. List
            dev_req = urllib.request.Request(
                f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(dev_req) as resp:
                devices = json.load(resp).get("devices", [])

            # 3. Delete matching node
            for d in devices:
                if d.get("name", "").split(".")[0] == target_hostname:
                    dev_id = d.get("id")
                    del_req = urllib.request.Request(
                        f"https://api.tailscale.com/api/v2/device/{dev_id}",
                        method="DELETE",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    urllib.request.urlopen(del_req)
                    pulumi.log.info(f"Tailscale node '{target_hostname}' removed.")
                    break
        except Exception as e:
            # Prevent cleanup failures from blocking infrastructure destruction
            pulumi.log.warn(f"Tailscale cleanup encountered an error: {e}")


class TailscaleNodeWatcher(pulumi.dynamic.Resource):
    def __init__(self, name, hostname, opts=None):
        config = pulumi.Config("tailscale")

        # Pack everything into props for the provider
        props = {
            "clientId": config.require("oauthClientId"),
            "clientSecret": config.require_secret("oauthClientSecret"),
            "tailnet": config.require("tailnet"),
            "hostname": hostname,
        }

        super().__init__(_TailscaleNodeWatcherProvider(), name, props, opts)


tailscale_watcher = TailscaleNodeWatcher(
    "tailscale-node-watcher",
    hostname=hub_server.name,
    opts=pulumi.ResourceOptions(parent=hub_server),
)
