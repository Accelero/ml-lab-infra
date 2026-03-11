"""SkyPilot admin policy: auto-inject Tailscale into every job.

On each job submission the policy prepends a setup script that installs Tailscale
and joins the tailnet using a one-time ephemeral auth key scoped to tag:skypilot-node.
That key is injected into the task environment so the setup script can join the tailnet.

Benefits over a static reusable key:
- Each job gets a unique key; if it leaks, it is already consumed.
- OAuth clients do not expire, no rotation needed.
- Key generation is auditable per-job in the Tailscale admin panel.
"""

import json
import os
import pathlib
import urllib.parse
from http.client import HTTPSConnection

import sky

_TAILNET = os.environ["TAILSCALE_TAILNET"]

_TAILSCALE_SETUP = (pathlib.Path(__file__).parent / "tailscale_setup.sh").read_text()


def _get_oauth_token(client_id: str, client_secret: str) -> str:
    """Exchange OAuth client credentials for a short-lived bearer token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    ).encode()
    conn = HTTPSConnection("api.tailscale.com", timeout=10)
    conn.request(
        "POST",
        "/api/v2/oauth/token",
        body=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = conn.getresponse()
    data = json.loads(resp.read())
    if "access_token" not in data:
        msg = f"Tailscale OAuth token exchange failed (HTTP {resp.status}): {data}"
        raise RuntimeError(msg)
    return data["access_token"]


def _create_auth_key(bearer_token: str, tailnet: str) -> str:
    """Create a one-time ephemeral preauthorised key for tag:skypilot-node."""
    payload = json.dumps(
        {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": False,
                        "ephemeral": True,
                        "preauthorized": True,
                        "tags": ["tag:skypilot-node"],
                    },
                },
            },
            "expirySeconds": 1000,
        },
    ).encode()
    conn = HTTPSConnection("api.tailscale.com", timeout=10)
    conn.request(
        "POST",
        f"/api/v2/tailnet/{tailnet}/keys",
        body=payload,
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        },
    )
    resp = conn.getresponse()
    data = json.loads(resp.read())
    if "key" not in data:
        msg = f"Tailscale auth key creation failed (HTTP {resp.status}): {data}"
        raise RuntimeError(msg)
    return data["key"]


class TailscalePolicy(sky.AdminPolicy):
    """Injects Tailscale into every SkyPilot job via a fresh per-job auth key."""

    @classmethod
    def validate_and_mutate(
        cls,
        user_request: sky.UserRequest,
    ) -> sky.MutatedUserRequest:
        """Generate a one-time Tailscale auth key and prepend setup to the task."""
        client_id = os.environ.get("TAILSCALE_OAUTH_CLIENT_ID", "")
        client_secret = os.environ.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            msg = (
                "TAILSCALE_OAUTH_CLIENT_ID / TAILSCALE_OAUTH_CLIENT_SECRET are not set."
            )
            raise RuntimeError(msg)

        bearer = _get_oauth_token(client_id, client_secret)
        auth_key = _create_auth_key(bearer, _TAILNET)

        task = user_request.task
        skypilot_config = user_request.skypilot_config
        task.update_envs(
            {
                "TAILSCALE_AUTH_KEY": auth_key,
            },
        )

        existing_setup = task.setup or ""
        task.setup = _TAILSCALE_SETUP + existing_setup

        return sky.MutatedUserRequest(
            task=task,
            skypilot_config=skypilot_config,
        )
