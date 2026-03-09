"""Tailscale Configuration."""

import json

import pulumi_tailscale as tailscale

# Dedicated OAuth client for the Tailscale Kubernetes operator.
# Needs auth_keys scope (to provision devices) and the k8s-operator tag.
operator_oauth_client = tailscale.OauthClient(
    "tailscale-operator-oauth-client",
    description="tailscale kubernetes operator",
    scopes=["auth_keys", "devices:core"],
    tags=["tag:k8s-operator"],
)

tailnet_settings = tailscale.TailnetSettings(
    "tailnet-settings",
    https_enabled=True,
)

acl = tailscale.Acl(
    "tailscale-acl",
    acl=json.dumps(
        {
            "tagOwners": {
                "tag:k8s-operator": ["autogroup:admin"],
                "tag:k8s": ["tag:k8s-operator"],
                "tag:hub-server": ["autogroup:admin"],
                "tag:skypilot-server": ["autogroup:admin"],
                "tag:skypilot-node": ["tag:skypilot-server"],
            },
            "grants": [
                # k8s-operator can reach anything (needs to provision/expose services)
                {
                    "src": ["tag:k8s-operator"],
                    "dst": ["*"],
                    "ip": ["*"],
                },
                # k8s nodes can talk to each other (pod/service mesh traffic)
                {
                    "src": ["tag:k8s"],
                    "dst": ["tag:k8s"],
                    "ip": ["*"],
                },
                # hub-server can reach k8s nodes and the operator
                {
                    "src": ["tag:hub-server"],
                    "dst": ["tag:k8s", "tag:k8s-operator"],
                    "ip": ["*"],
                },
                # skypilot-server can reach hub-server and k8s nodes
                {
                    "src": ["tag:skypilot-server"],
                    "dst": ["tag:hub-server", "tag:k8s"],
                    "ip": ["*"],
                },
                # skypilot-nodes can reach k8s services like MLflow and Skypilot-server
                {
                    "src": ["tag:skypilot-node"],
                    "dst": ["tag:k8s"],
                    "ip": ["*"],
                },
                # admins can reach everything
                {
                    "src": ["autogroup:admin"],
                    "dst": ["*"],
                    "ip": ["*"],
                },
                # regular users can reach k8s services
                {
                    "src": ["autogroup:member"],
                    "dst": ["tag:k8s"],
                    "ip": ["*"],
                },
            ],
        },
    ),
    overwrite_existing_content=True,
)
