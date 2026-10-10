# Copyright (c) 2026 David Schmid
"""Keep hub recovery hints accurate and independent of secret property values."""

import shlex
import unittest
from unittest.mock import patch

import pulumi

from resources.hub_bootstrap import hub_bootstrap_hint, hub_connection_error_hint

from .helpers import expect_equal

_SERVER_URN = "urn:pulumi:test::ml-lab-infra::hcloud:index/server:Server::hub-server"
_KEY_URN = (
    "urn:pulumi:test::ml-lab-infra::"
    "tailscale:index/tailnetKey:TailnetKey::hub-ts-auth-key"
)
_INSTALL_URN = (
    "urn:pulumi:test::ml-lab-infra::"
    "hcloud:index/server:Server$command:remote:Command::hub-cluster-install"
)
_RECOVERY_COMMAND = (
    f"pulumi up --refresh --replace {shlex.quote(_KEY_URN)} "
    f"--replace {shlex.quote(_SERVER_URN)}"
)


class HubBootstrapTests(unittest.TestCase):
    """Explain consumed keys without masking unrelated failures or leaking inputs."""

    def test_creation_hint_has_recovery_command_without_secret_inputs(self) -> None:
        """Recovery targets the current stack and never prints secret user data."""
        args = pulumi.ResourceHookArgs(
            urn=_SERVER_URN,
            id="",
            name="hub-server",
            type="hcloud:index/server:Server",
            new_inputs={"userData": "test-secret-never-log"},
        )
        with patch("resources.hub_bootstrap.pulumi.log.warn") as warning:
            hub_bootstrap_hint(args)
        message = warning.call_args.args[0]
        expect_equal(warning.call_count, 1)
        expect_equal(_RECOVERY_COMMAND in message, expected=True)
        expect_equal("test-secret-never-log" in message, expected=False)
        expect_equal("requires an unused" in message, expected=True)

    def test_ssh_failure_keeps_original_error_and_does_not_retry(self) -> None:
        """The error hint is conditional and never repeats external error contents."""
        errors = ["dial tcp: i/o timeout: test-secret-never-log"]
        args = pulumi.ErrorHookArgs(
            urn=_INSTALL_URN,
            id="",
            name="hub-cluster-install",
            type="command:remote:Command",
            failed_operation="create",
            errors=errors,
            new_inputs={"connection": {"privateKey": "test-private-key-never-log"}},
        )
        with patch("resources.hub_bootstrap.pulumi.log.warn") as warning:
            expect_equal(hub_connection_error_hint(args), expected=False)
        message = warning.call_args.args[0]
        expect_equal(_RECOVERY_COMMAND in message, expected=True)
        expect_equal("If the VM was rebuilt" in message, expected=True)
        expect_equal("test-secret-never-log" in message, expected=False)
        expect_equal("test-private-key-never-log" in message, expected=False)
        expect_equal(args.errors, errors)

    def test_unrelated_command_failures_do_not_blame_bootstrap(self) -> None:
        """A connected server with a failed installation gets its normal error."""
        for operation, errors in (
            ("create", ["Process exited with status 1"]),
            ("create", []),
            ("update", ["dial tcp: i/o timeout"]),
            ("delete", ["no such host"]),
        ):
            args = pulumi.ErrorHookArgs(
                urn=_INSTALL_URN,
                id="",
                name="hub-cluster-install",
                type="command:remote:Command",
                failed_operation=operation,
                errors=errors,
            )
            with (
                self.subTest(operation=operation, errors=errors),
                patch("resources.hub_bootstrap.pulumi.log.warn") as warning,
            ):
                expect_equal(hub_connection_error_hint(args), expected=False)
                expect_equal(warning.call_count, 0)
