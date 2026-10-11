# Copyright (c) 2026 David Schmid
"""Read development-stack secrets without writing or printing them."""

import json
import shutil
from dataclasses import dataclass

from support.process import command


class SecretValues[Value](dict[str, Value]):
    """Keep credential fixture values out of pytest argument representations."""

    def __repr__(self) -> str:
        """Hide values when pytest renders a failing call's arguments."""
        return "<redacted test configuration>"


class SecretText(str):
    """Keep JSON sent to subprocess stdin out of exception representations."""

    __slots__ = ()

    def __repr__(self) -> str:
        """Retain ordinary string behavior while hiding diagnostic values."""
        return "<redacted test input>"


def secret_json(value: object) -> SecretText:
    """Serialize an in-memory payload without exposing it in pytest tracebacks."""
    return SecretText(json.dumps(value))


@dataclass(frozen=True)
class DevStack:
    """Bind every Pulumi operation to the requested development stack."""

    name: str

    def pulumi(self, *args: str, timeout: float = 30) -> str:
        """Run Pulumi without depending on the workspace's selected stack."""
        return command(
            [shutil.which("pulumi") or "pulumi", *args, "--stack", self.name],
            timeout=timeout,
        )

    def output(self, key: str) -> str:
        """Read one output in memory, including its secret value when necessary."""
        return self.pulumi("stack", "output", key, "--show-secrets")

    def config(self, key: str) -> str:
        """Read one configuration value without printing it."""
        return self.pulumi("config", "get", key)

    def preflight(self) -> None:
        """Resolve the requested stack and confirm Tailscale is connected."""
        self.pulumi("stack", "--show-name")
        status = json.loads(
            command([shutil.which("tailscale") or "tailscale", "status", "--json"]),
        )
        if status.get("BackendState") != "Running":
            msg = "Live tests require a connected Tailscale client."
            raise RuntimeError(msg)
