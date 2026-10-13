"""Check Tailscale request handling without network calls."""

import json
import os
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest

from resources import ts_device_cleanup
from resources.ts_device_cleanup import (
    _TailscaleDeviceCleanupProvider as CleanupProvider,
)
from scripts import skypilot_policies
from scripts.skypilot_policies import _create_auth_key as create_auth_key
from scripts.skypilot_policies import _get_oauth_token as get_oauth_token

from .helpers import expect_equal

_PROPS = {
    "hostname": "hub-server",
    "client_id": "test-client",
    "client_secret": "test-secret",
    "tailnet": "example.ts.net",
}


def response(payload: object) -> MagicMock:
    """Provide a closeable JSON response."""
    result = MagicMock()
    result.status = 200
    result.read.return_value = json.dumps(payload).encode()
    result.__enter__.return_value = result
    return result


class PolicyClientTests(unittest.TestCase):
    """Verify policy requests close connections and redact errors."""

    def setUp(self) -> None:
        """Replace the policy's HTTPS transport."""
        self.connection = MagicMock()
        self.transport = self.enterContext(
            patch.object(
                skypilot_policies,
                "HTTPSConnection",
                return_value=self.connection,
            ),
        )

    def test_oauth_request_and_connection_cleanup(self) -> None:
        """Keep credentials in the request body and close both handles."""
        reply = response({"access_token": "test-token"})
        self.connection.getresponse.return_value = reply
        expect_equal(get_oauth_token("client", "secret"), "test-token")
        self.transport.assert_called_once_with("api.tailscale.com", timeout=10)
        request = self.connection.request.call_args
        expect_equal(request.args, ("POST", "/api/v2/oauth/token"))
        expect_equal(
            parse_qs(request.kwargs["body"].decode()),
            {
                "grant_type": ["client_credentials"],
                "client_id": ["client"],
                "client_secret": ["secret"],
            },
        )
        reply.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_auth_key_keeps_worker_capabilities(self) -> None:
        """Request a single-use ephemeral worker key with a five-minute expiry."""
        reply = response({"key": "test-key"})
        self.connection.getresponse.return_value = reply
        expect_equal(
            create_auth_key("token", "example.ts.net"),
            "test-key",
        )
        request = self.connection.request.call_args
        expect_equal(request.args, ("POST", "/api/v2/tailnet/example.ts.net/keys"))
        payload = json.loads(request.kwargs["body"])
        expect_equal(
            payload["capabilities"]["devices"]["create"],
            {
                "reusable": False,
                "ephemeral": True,
                "preauthorized": True,
                "tags": ["tag:skypilot-node"],
            },
        )
        expect_equal(payload["expirySeconds"], 300)
        reply.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_http_failures_never_expose_response_bodies(self) -> None:
        """Reject error responses before reading their contents."""
        for operation in (get_oauth_token, create_auth_key):
            reply = response(
                {"access_token": "sensitive-value", "key": "sensitive-value"},
            )
            reply.status = 401
            self.connection.reset_mock()
            self.connection.getresponse.return_value = reply
            with (
                self.subTest(operation=operation.__name__),
                pytest.raises(RuntimeError) as error,
            ):
                operation("client", "secret")
            expect_equal("sensitive-value" in str(error.value), expected=False)
            reply.read.assert_not_called()
            reply.close.assert_called_once_with()
            self.connection.close.assert_called_once_with()

    def test_invalid_success_responses_close_connections(self) -> None:
        """Missing, empty, or non-string credentials must fail."""
        for field, operation in (
            ("access_token", get_oauth_token),
            ("key", create_auth_key),
        ):
            for payload in ({}, [], {field: None}, {field: ""}, {field: 1}):
                self.connection.reset_mock()
                reply = response(payload)
                self.connection.getresponse.return_value = reply
                with (
                    self.subTest(field=field, payload=payload),
                    pytest.raises(RuntimeError),
                ):
                    operation("client", "secret")
                reply.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()

    def test_transport_failure_closes_connection(self) -> None:
        """Release a connection when sending the request fails."""
        self.connection.request.side_effect = TimeoutError("request timed out")
        with pytest.raises(TimeoutError):
            get_oauth_token("client", "secret")
        self.connection.close.assert_called_once_with()

    def test_policy_preserves_task_setup_and_configuration(self) -> None:
        """Prepend Tailscale setup and retain the submitted task settings."""
        task = skypilot_policies.sky.Task(setup="echo test-setup", run="echo test-run")
        config = {"test_setting": "keep"}
        request = skypilot_policies.sky.UserRequest(
            request_name="jobs.launch",
            task=task,
            skypilot_config=config,
        )
        with (
            patch.dict(
                os.environ,
                {
                    "TAILSCALE_OAUTH_CLIENT_ID": "client",
                    "TAILSCALE_OAUTH_CLIENT_SECRET": "secret",
                    "TAILSCALE_TAILNET": "example.ts.net",
                },
            ),
            patch.object(skypilot_policies, "_get_oauth_token", return_value="token"),
            patch.object(
                skypilot_policies,
                "_create_auth_key",
                return_value="key",
            ) as create,
        ):
            result = skypilot_policies.SkyPilotAdminPolicy.validate_and_mutate(request)
        create.assert_called_once_with("token", "example.ts.net")
        expect_equal(result.task.envs["TAILSCALE_AUTH_KEY"], "key")
        expect_equal(result.task.setup.endswith("echo test-setup"), expected=True)
        expect_equal(result.task.run, "echo test-run")
        expect_equal(result.skypilot_config, config)
        self.transport.assert_not_called()


class DeviceCleanupTests(unittest.TestCase):
    """Keep device matching and warning behavior while bounding requests."""

    def test_matching_device_is_deleted_and_responses_are_closed(self) -> None:
        """Delete only the matching hostname and close all response handles."""
        replies = [
            response({"access_token": "test-token"}),
            response(
                {
                    "devices": [
                        {"name": "other.example.ts.net", "id": "other-id"},
                        {"name": "hub-server.example.ts.net", "id": "hub-id"},
                    ],
                },
            ),
            response({}),
        ]
        with patch.object(
            ts_device_cleanup.urllib.request,
            "urlopen",
            side_effect=replies,
        ) as request:
            CleanupProvider().delete("test", _PROPS)
        expect_equal(request.call_count, 3)
        deletion = request.call_args_list[2]
        expect_equal(
            deletion.args[0].full_url,
            "https://api.tailscale.com/api/v2/device/hub-id",
        )
        expect_equal(deletion.args[0].method, "DELETE")
        for invocation, reply in zip(request.call_args_list, replies, strict=True):
            expect_equal(invocation.kwargs["timeout"], 10)
            reply.__exit__.assert_called_once()

    def test_missing_device_does_not_send_delete(self) -> None:
        """Skip deletion when no hostname matches."""
        replies = [response({"access_token": "token"}), response({"devices": []})]
        with patch.object(
            ts_device_cleanup.urllib.request,
            "urlopen",
            side_effect=replies,
        ) as request:
            CleanupProvider().delete("test", _PROPS)
        expect_equal(request.call_count, 2)

    def test_cleanup_warning_does_not_include_external_error_text(self) -> None:
        """Keep cleanup nonblocking without printing sensitive exception contents."""
        with (
            patch.object(
                ts_device_cleanup.urllib.request,
                "urlopen",
                side_effect=OSError("sensitive-value"),
            ),
            patch.object(ts_device_cleanup.log, "warn") as warning,
        ):
            CleanupProvider().delete("test", _PROPS)
        expect_equal(warning.call_count, 1)
        expect_equal("sensitive-value" in warning.call_args.args[0], expected=False)
