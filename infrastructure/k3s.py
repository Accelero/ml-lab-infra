"""K3s installation and kubeconfig export for the hub server."""

import pulumi
import pulumi_command as command
import pulumi_kubernetes as k8s

from infrastructure.hub_server import hub_server, hub_ssh_private_key

# Set `k3sVersion` in stack config to pin a specific release channel
_config = pulumi.Config()
k3s_version = _config.get("k3sVersion") or "stable"
_tailnet = pulumi.Config("tailscale").require("tailnet")

_conn = command.remote.ConnectionArgs(
    host=hub_server.name.apply(lambda name: f"{name}.{_tailnet}"),
    user="root",
    private_key=hub_ssh_private_key,
    dial_error_limit=60,  # retry for ~5 min while Tailscale registers the node
)

# Install k3s bound to the Tailscale interface.
k3s_install = command.remote.Command(
    "k3s-install",
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
    opts=pulumi.ResourceOptions(depends_on=[hub_server]),
)

# Fetch the kubeconfig and patch 127.0.0.1 → Tailscale IP so it's
# reachable from outside the server.
k3s_kubeconfig_cmd = command.remote.Command(
    "k3s-kubeconfig",
    connection=_conn,
    create=(
        "TS_IP=$(tailscale ip -4) && "
        'sed "s/127.0.0.1/$TS_IP/g" /etc/rancher/k3s/k3s.yaml'
    ),
    triggers=[k3s_install.id],
    opts=pulumi.ResourceOptions(
        depends_on=[k3s_install],
        additional_secret_outputs=["stdout"],
    ),
)

k3s_kubeconfig: pulumi.Output[str] = k3s_kubeconfig_cmd.stdout.apply(
    pulumi.Output.secret,
)
pulumi.export("k3s_kubeconfig", k3s_kubeconfig)

k8s_provider = k8s.Provider(
    "hub-k8s",
    kubeconfig=k3s_kubeconfig,
)
