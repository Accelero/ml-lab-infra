"""Deregister Tailscale devices during Pulumi teardown."""

import base64
import json
import urllib.request

import pulumi
from pulumi import Input, ResourceOptions, log
from pulumi.dynamic import CreateResult, Resource, ResourceProvider

_REQUEST_TIMEOUT = 10


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
            credentials = base64.b64encode(
                f"{client_id}:{client_secret}".encode(),
            ).decode()
            with urllib.request.urlopen(
                urllib.request.Request(
                    "https://api.tailscale.com/api/v2/oauth/token",
                    data=b"grant_type=client_credentials",
                    headers={
                        "Authorization": f"Basic {credentials}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                ),
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                token = json.load(response)["access_token"]

            with urllib.request.urlopen(
                urllib.request.Request(
                    f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/devices",
                    headers={"Authorization": f"Bearer {token}"},
                ),
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                devices = json.load(response).get("devices", [])

            for device in devices:
                if device.get("name", "").split(".")[0] == hostname:
                    with urllib.request.urlopen(
                        urllib.request.Request(
                            f"https://api.tailscale.com/api/v2/device/{device['id']}",
                            method="DELETE",
                            headers={"Authorization": f"Bearer {token}"},
                        ),
                        timeout=_REQUEST_TIMEOUT,
                    ):
                        pass
                    log.info(f"Tailscale node '{hostname}' removed.")
                    break
            else:
                log.info(f"Tailscale node '{hostname}' not found in tailnet, skipping.")
        except (OSError, ValueError, KeyError) as exc:
            log.warn(
                f"Tailscale cleanup failed for '{hostname}' ({type(exc).__name__}).",
            )


class TailscaleDeviceCleanup(Resource):
    """Remove a device from the tailnet when the resource is deleted."""

    def __init__(
        self,
        name: str,
        hostname: Input[str],
        opts: ResourceOptions | None = None,
    ) -> None:
        """Register cleanup for the given hostname."""
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
