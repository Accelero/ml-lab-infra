# Copyright (c) 2026 David Schmid
"""Tailscale Configuration."""

import json

import pulumi_tailscale as tailscale

# Dedicated OAuth client for the Tailscale Kubernetes operator.
# Needs auth_keys scope (to provision devices) and the k8s-operator tag.
operator_oauth_client = tailscale.OauthClient(
    "tailscale-operator-oauth-client",
    description="k8s operator",
    scopes=["auth_keys", "devices:core"],
    tags=["tag:k8s-operator"],
)

# OAuth client for the SkyPilot API server.
# Needs auth_keys scope (to provision devices) and the skypilot-server tag.
skypilot_ts_oauth_client = tailscale.OauthClient(
    "skypilot-ts-oauth-client",
    description="SkyPilot API server",
    scopes=["auth_keys"],
    tags=["tag:skypilot-server"],
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
                "tag:mlflow": ["tag:k8s-operator"],
                "tag:skypilot-api": ["tag:k8s-operator"],
                "tag:hub-server": ["autogroup:admin"],
                "tag:skypilot-server": ["autogroup:admin"],
                "tag:skypilot-node": ["tag:skypilot-server"],
            },
            "grants": [
                # The operator provisions proxies and requests HTTPS certificates.
                {
                    "src": ["tag:k8s-operator"],
                    "dst": ["tag:k8s", "autogroup:internet"],
                    "ip": ["*"],
                },
                {
                    "src": ["tag:k8s"],
                    "dst": ["tag:k8s"],
                    "ip": ["*"],
                },
                {
                    "src": ["tag:hub-server"],
                    "dst": ["tag:k8s", "tag:k8s-operator"],
                    "ip": ["*"],
                },
                {
                    "src": ["tag:skypilot-server"],
                    "dst": ["tag:hub-server", "tag:k8s"],
                    "ip": ["*"],
                },
                {
                    "src": ["tag:skypilot-node"],
                    "dst": ["tag:mlflow", "tag:skypilot-api"],
                    "ip": ["tcp:443"],
                },
                {
                    "src": ["autogroup:admin"],
                    "dst": ["*"],
                    "ip": ["*"],
                },
                {
                    "src": ["autogroup:member"],
                    "dst": ["tag:k8s"],
                    "ip": ["*"],
                },
            ],
            "tests": [
                {
                    "src": "tag:skypilot-node",
                    "proto": "tcp",
                    "accept": ["tag:mlflow:443", "tag:skypilot-api:443"],
                    "deny": [
                        "tag:mlflow:22",
                        "tag:mlflow:80",
                        "tag:skypilot-api:22",
                        "tag:skypilot-api:80",
                        "tag:hub-server:22",
                        "tag:hub-server:6443",
                        "tag:k8s-operator:443",
                        "tag:skypilot-node:22",
                    ],
                },
                {
                    "src": "tag:skypilot-node",
                    "proto": "udp",
                    "deny": ["tag:mlflow:443", "tag:skypilot-api:443"],
                },
            ],
        },
    ),
    overwrite_existing_content=True,
)
