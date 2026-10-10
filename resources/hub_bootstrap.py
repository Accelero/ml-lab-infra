# Copyright (c) 2026 David Schmid
"""Explain recovery from a hub bootstrap key or connectivity failure."""

import shlex

import pulumi

_CONNECTION_ERRORS = (
    "dial tcp",
    "dial error",
    "failed to connect",
    "connection refused",
    "connection timed out",
    "i/o timeout",
    "no such host",
    "network is unreachable",
    "ssh: handshake failed",
)


def _rebuild_command(urn: str) -> str:
    """Address the existing root-level key and server in the hook's stack."""
    stack_project = urn.rsplit("::", 2)[0]
    key_urn = f"{stack_project}::tailscale:index/tailnetKey:TailnetKey::hub-ts-auth-key"
    server_urn = f"{stack_project}::hcloud:index/server:Server::hub-server"
    return (
        f"pulumi up --refresh --replace {shlex.quote(key_urn)} "
        f"--replace {shlex.quote(server_urn)}"
    )


def hub_bootstrap_hint(args: pulumi.ResourceHookArgs) -> None:
    """Remind operators about single-use keys only when creating a VM."""
    pulumi.log.warn(
        "Creating hub-server requires an unused Tailscale bootstrap key. "
        "When rebuilding, use --refresh to observe and renew an invalid key, "
        "or explicitly replace hub-ts-auth-key as well. "
        "If the new VM fails to join Tailscale, renew the key and recreate the VM: "
        + _rebuild_command(args.urn),
    )


def hub_connection_error_hint(args: pulumi.ErrorHookArgs) -> bool:
    """Add a possible bootstrap cause without hiding the SSH error or retrying."""
    if args.failed_operation == "create" and any(
        marker in error.lower()
        for error in args.errors
        for marker in _CONNECTION_ERRORS
    ):
        pulumi.log.warn(
            "Could not reach hub-server over Tailscale for K3s installation. "
            "Check the runner's Tailscale connection and the VM's bootstrap logs. "
            "If the VM was rebuilt with a consumed or expired bootstrap key, "
            "replace both hub-ts-auth-key and hub-server; renewing the key alone "
            "does not rerun cloud-init. Recovery command: "
            + _rebuild_command(args.urn),
        )
    return False
