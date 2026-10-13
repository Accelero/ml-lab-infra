"""Read and terminate test-owned RunPod pods through the REST API."""

import json
import re
from contextlib import closing
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import quote, urlsplit

_BODY_LIMIT = 2 * 1024 * 1024
_CLUSTER = re.compile(r"mltest-[0-9a-f]{16}")


@dataclass(frozen=True)
class Pod:
    """Keep only inventory fields needed for ownership and billing checks."""

    identifier: str
    name: str
    status: str
    hourly_cost: Decimal | None
    gpu_count: int
    network_volume: bool


def owns_pod(cluster: str, name: str) -> bool:
    """Match a unique cluster, optional SkyPilot user hash, and node suffix."""
    if not _CLUSTER.fullmatch(cluster):
        msg = "GPU tests require a unique mltest cluster name."
        raise ValueError(msg)
    return (
        re.fullmatch(
            rf"{re.escape(cluster)}(?:-[a-z0-9]+)?-(?:head|worker)",
            name,
        )
        is not None
    )


def _cost(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() and result > 0 else None


@dataclass(frozen=True)
class RunPodAPI:
    """Use bounded HTTP calls without logging credentials or API bodies."""

    api_key: str = field(repr=False)
    base_url: str = "https://rest.runpod.io/v1"

    def request(self, method: str, path: str) -> object:
        """Allow the official HTTPS API or an isolated loopback test server."""
        parsed = urlsplit(self.base_url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/v1"
            or not (
                (
                    parsed.scheme == "https"
                    and parsed.hostname == "rest.runpod.io"
                    and parsed.port is None
                )
                or (parsed.scheme == "http" and parsed.hostname == "127.0.0.1")
            )
        ):
            msg = "Unsupported RunPod API URL."
            raise ValueError(msg)
        connection_type = (
            HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        )
        try:
            with closing(
                connection_type(parsed.hostname, parsed.port, timeout=10),
            ) as connection:
                connection.request(
                    method,
                    parsed.path + path,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                with closing(connection.getresponse()) as response:
                    if method == "DELETE" and response.status == HTTPStatus.NOT_FOUND:
                        return None
                    if (
                        not HTTPStatus.OK
                        <= response.status
                        < HTTPStatus.MULTIPLE_CHOICES
                    ):
                        msg = f"RunPod API failed (HTTP {response.status})."
                        raise RuntimeError(msg)
                    if method == "DELETE":
                        return None
                    body = response.read(_BODY_LIMIT + 1)
                    if len(body) > _BODY_LIMIT:
                        msg = "RunPod inventory exceeded the response size limit."
                        raise RuntimeError(msg)
                    return json.loads(body)
        except OSError, ValueError, HTTPException:
            msg = "RunPod API transport or response failed."
            raise RuntimeError(msg) from None

    def pods(self) -> list[Pod]:
        """Reject malformed inventories rather than declaring cleanup complete."""
        payload = self.request("GET", "/pods")
        if not isinstance(payload, list):
            msg = "RunPod inventory must be a list."
            raise TypeError(msg)
        pods = []
        for item in payload:
            if not isinstance(item, dict) or any(
                not isinstance(item.get(key), str) or not item[key]
                for key in ("id", "name", "desiredStatus")
            ):
                msg = "RunPod inventory contains a malformed pod."
                raise RuntimeError(msg)
            gpu = item.get("gpu") or {}
            count = (
                gpu.get("count", item.get("gpuCount", 0))
                if isinstance(gpu, dict)
                else 0
            )
            if type(count) is not int:
                msg = "RunPod inventory contains an invalid GPU count."
                raise TypeError(msg)
            pods.append(
                Pod(
                    item["id"],
                    item["name"],
                    item["desiredStatus"],
                    _cost(item.get("costPerHr")),
                    count,
                    bool(item.get("networkVolume") or item.get("networkVolumeId")),
                ),
            )
        return pods

    def delete(self, identifier: str) -> None:
        """Terminate rather than stop a pod, releasing its attached pod storage."""
        self.request("DELETE", f"/pods/{quote(identifier, safe='')}")


def budget_violation(pods: list[Pod], hourly_limit: Decimal) -> str | None:
    """Refuse unknown prices, multiple GPUs, persistent volumes, or excess rates."""
    if len(pods) > 1:
        return "GPU test provisioned more than one pod."
    for pod in pods:
        if pod.hourly_cost is None:
            return "GPU test pod has no valid hourly price."
        if pod.hourly_cost > hourly_limit:
            return "GPU test exceeded its hourly price limit."
        if pod.gpu_count != 1:
            return "GPU test requires exactly one GPU."
        if pod.network_volume:
            return "GPU test must not attach persistent network volumes."
    return None
