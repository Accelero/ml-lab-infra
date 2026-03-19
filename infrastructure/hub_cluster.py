"""Cluster installation and kubeconfig export for the hub server."""

import http.client

import pulumi
import pulumi_command as command
import pulumi_kubernetes as k8s

from infrastructure.hub_server import hub_server, hub_ssh_private_key


def _resolve_k3s_version(version: str) -> str:
    """Resolve a channel name like 'stable' to a pinned release version."""
    if not version.startswith("v"):
        conn = http.client.HTTPSConnection("update.k3s.io")
        conn.request("GET", f"/v1-release/channels/{version}")
        resp = conn.getresponse()
        location = resp.getheader("Location", "")
        return location.rstrip("/").rsplit("/", 1)[-1]
    return version


# Set `k3sVersion` in stack config to pin a version or use a channel name.
_config = pulumi.Config()
k3s_version = _resolve_k3s_version(_config.get("k3sVersion") or "stable")
_tailnet = pulumi.Config("tailscale").require("tailnet")

_conn = command.remote.ConnectionArgs(
    host=hub_server.name.apply(lambda name: f"{name}.{_tailnet}"),
    user="root",
    private_key=hub_ssh_private_key,
    dial_error_limit=20,  # retry for ~5 min while Tailscale registers the node
)

# Install k3s bound to the Tailscale interface.
hub_cluster_install = command.remote.Command(
    "hub-cluster-install",
    connection=_conn,
    create=(
        "TS_IP=$(tailscale ip -4) && "
        f'curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL="{k3s_version}" '
        'INSTALL_K3S_EXEC="server" sh -s - '
        "--flannel-iface=tailscale0 "
        "--node-ip=$TS_IP "
        "--node-external-ip=$TS_IP "
        "--bind-address=$TS_IP && "
        "until kubectl get nodes 2>/dev/null | grep -q ' Ready'; do sleep 2; done"
    ),
    triggers=[k3s_version, hub_server.id],
    opts=pulumi.ResourceOptions(parent=hub_server),
)

# Fetch the kubeconfig and patch 127.0.0.1 → Tailscale IP so it's
# reachable from outside the server.
hub_kubeconfig_cmd = command.remote.Command(
    "hub-kubeconfig",
    connection=_conn,
    create=(
        "TS_IP=$(tailscale ip -4) && "
        'sed "s/127.0.0.1/$TS_IP/g" /etc/rancher/k3s/k3s.yaml'
    ),
    triggers=[hub_cluster_install.id],
    opts=pulumi.ResourceOptions(
        parent=hub_cluster_install,
        additional_secret_outputs=["stdout"],
    ),
)

hub_kubeconfig = hub_kubeconfig_cmd.stdout
pulumi.export("hub_kubeconfig", hub_kubeconfig)

hub_k8s_provider = k8s.Provider(
    "hub-k8s-provider",
    kubeconfig=hub_kubeconfig,
)
