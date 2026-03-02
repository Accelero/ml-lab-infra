"""Custom Pulumi resource to cleanup Tailscale nodes."""

import base64
import json
import urllib.request
from typing import Optional

import pulumi
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import CreateResult, Resource, ResourceProvider


class _TailscaleDeviceCleanupProvider(ResourceProvider):
    def create(self, props: dict) -> CreateResult:
        return CreateResult(id_=props["hostname"], outs=props)

    def delete(self, _id: str, props: dict) -> None:
        hostname = props["hostname"]
        client_id = props["client_id"]
        client_secret = props["client_secret"]
        tailnet = props["tailnet"]
        log.info(f"Cleaning up Tailscale node '{hostname}'...")
        try:
            # 1. OAuth token
            b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            with urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.tailscale.com/api/v2/oauth/token",
                    data=b"grant_type=client_credentials",
                    headers={
                        "Authorization": f"Basic {b64}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
            ) as r:
                token = json.load(r)["access_token"]

            # 2. List devices
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices",
                    headers={"Authorization": f"Bearer {token}"},
                )
            ) as r:
                devices = json.load(r).get("devices", [])

            # 3. Delete matching node
            for d in devices:
                if d.get("name", "").split(".")[0] == hostname:
                    urllib.request.urlopen(
                        urllib.request.Request(
                            f"https://api.tailscale.com/api/v2/device/{d['id']}",
                            method="DELETE",
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    )
                    log.info(f"Tailscale node '{hostname}' removed.")
                    break
            else:
                log.info(f"Tailscale node '{hostname}' not found in tailnet, skipping.")
        except Exception as e:
            log.warn(f"Tailscale cleanup warning: {e}")


class TailscaleDeviceCleanup(Resource):
    """Pulumi resource that deletes a Tailscale device on deletion."""

    def __init__(
        self,
        name: str,
        hostname: Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Initialize a TailscaleDeviceCleanup resource.

        Args:
            name (str): The name of the Pulumi resource.
            hostname (Input[str]): The Tailscale device hostname to cleanup.
            opts (ResourceOptions | None, optional): Pulumi resource options. \
            Defaults to None.

        """
        ts_config = pulumi.Config("tailscale")
        super().__init__(
            _TailscaleDeviceCleanupProvider(),
            name,
            {
                "hostname": hostname,
                "client_id": ts_config.require("oauthClientId"),
                "client_secret": ts_config.require_secret("oauthClientSecret"),
                "tailnet": ts_config.require("tailnet"),
            },
            opts,
        )
