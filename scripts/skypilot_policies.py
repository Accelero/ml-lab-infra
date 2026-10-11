# Copyright (c) 2026 David Schmid
"""Inject Tailscale access through the registered SkyPilot admin policies."""

import json
import os
import pathlib
import urllib.parse
from contextlib import closing
from http import HTTPStatus
from http.client import HTTPSConnection

import sky

_TAILSCALE_SETUP = (pathlib.Path(__file__).parent / "tailscale_setup.sh").read_text()
_REQUEST_TIMEOUT = 10


def _get_oauth_token(client_id: str, client_secret: str) -> str:
    """Exchange OAuth client credentials for a short-lived bearer token."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    ).encode()
    with closing(
        HTTPSConnection("api.tailscale.com", timeout=_REQUEST_TIMEOUT),
    ) as connection:
        connection.request(
            "POST",
            "/api/v2/oauth/token",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with closing(connection.getresponse()) as response:
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                msg = f"Tailscale OAuth token exchange failed (HTTP {response.status})."
                raise RuntimeError(msg)
            data = json.loads(response.read())
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        msg = "Tailscale OAuth response has no access token."
        raise RuntimeError(msg)
    return token


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
            "expirySeconds": 300,
        },
    ).encode()
    with closing(
        HTTPSConnection("api.tailscale.com", timeout=_REQUEST_TIMEOUT),
    ) as connection:
        connection.request(
            "POST",
            f"/api/v2/tailnet/{tailnet}/keys",
            body=payload,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
            },
        )
        with closing(connection.getresponse()) as response:
            if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
                msg = f"Tailscale auth key creation failed (HTTP {response.status})."
                raise RuntimeError(msg)
            data = json.loads(response.read())
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        msg = "Tailscale key response has no auth key."
        raise RuntimeError(msg)
    return key


class _TailscalePolicy(sky.AdminPolicy):
    """Prepend Tailscale setup and inject a per-job ephemeral auth key."""

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
        auth_key = _create_auth_key(bearer, os.environ["TAILSCALE_TAILNET"])

        task = user_request.task
        task.update_envs({"TAILSCALE_AUTH_KEY": auth_key})
        existing_setup = task.setup or ""
        task.setup = _TAILSCALE_SETUP + existing_setup

        return sky.MutatedUserRequest(
            task=task,
            skypilot_config=user_request.skypilot_config,
        )


class SkyPilotAdminPolicy(sky.AdminPolicy):
    """Apply the registered admin policies in order."""

    _policies: tuple[type[sky.AdminPolicy], ...] = (_TailscalePolicy,)

    @classmethod
    def validate_and_mutate(
        cls,
        user_request: sky.UserRequest,
    ) -> sky.MutatedUserRequest:
        """Thread the request through each sub-policy in sequence."""
        result = user_request
        for policy in cls._policies:
            result = policy.validate_and_mutate(result)
        return result
