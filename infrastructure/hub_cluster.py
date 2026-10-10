# Copyright (c) 2026 David Schmid
"""Cluster installation and kubeconfig export for the hub server."""

import pulumi
import pulumi_command as command
import pulumi_kubernetes as k8s

from infrastructure.hub_server import hub_server, hub_ssh_private_key
from resources.hub_bootstrap import hub_connection_error_hint
from resources.k3s_version import resolve_k3s_version

_config = pulumi.Config()
k3s_version = resolve_k3s_version(_config.require("k3sVersion"))
pulumi.export("k3s_version", k3s_version)
_tailnet = pulumi.Config("tailscale").require("tailnet")

_conn = command.remote.ConnectionArgs(
    host=hub_server.name.apply(lambda name: f"{name}.{_tailnet}"),
    user="root",
    private_key=hub_ssh_private_key,
    dial_error_limit=40,
    per_dial_timeout=5,
)

# Install k3s bound to the Tailscale interface.
hub_cluster_install = command.remote.Command(
    "hub-cluster-install",
    connection=_conn,
    create=(
        "TS_IP=$(tailscale ip -4) && "
        f'curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="{k3s_version}" '
        'INSTALL_K3S_EXEC="server" sh -s - '
        "--flannel-iface=tailscale0 "
        "--node-ip=$TS_IP "
        "--node-external-ip=$TS_IP "
        "--bind-address=$TS_IP && "
        "until kubectl get nodes 2>/dev/null | grep -q ' Ready'; do sleep 2; done"
    ),
    triggers=[k3s_version, hub_server.id],
    opts=pulumi.ResourceOptions(
        parent=hub_server,
        hooks=pulumi.ResourceHookBinding(
            on_error=[
                pulumi.ErrorHook(
                    "hub-bootstrap-connection-hint",
                    hub_connection_error_hint,
                ),
            ],
        ),
    ),
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
    logging=command.remote.Logging.NONE,  # Don't log the kubeconfig in terminal output
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
