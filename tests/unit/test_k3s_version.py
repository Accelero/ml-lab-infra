"""K3s channel resolution without network calls or Pulumi resource registration."""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from resources import k3s_version
from resources.k3s_version import resolve_k3s_version

from .helpers import expect_equal

_RELEASE = "v1.37.1+k3s1"
_RELEASE_URL = "https://github.com/k3s-io/k3s/releases/tag/v1.37.1%2Bk3s1"


class K3sVersionTests(unittest.TestCase):
    """Check pins, valid channels, malformed redirects, and request failures."""

    def setUp(self) -> None:
        """Replace HTTPS transport with a closeable response double."""
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 302
        self.response.getheader.return_value = _RELEASE_URL
        transport = patch.object(
            k3s_version.http.client,
            "HTTPSConnection",
            return_value=self.connection,
        )
        self.transport = self.enterContext(transport)

    def test_exact_pin_does_not_access_network(self) -> None:
        """Pinned releases remain reproducible and work offline."""
        expect_equal(resolve_k3s_version(_RELEASE), _RELEASE)
        expect_equal(self.transport.call_count, 0)

    def test_channels_resolve_and_close_transport(self) -> None:
        """Minor-version channels starting with v must also be resolved."""
        for channel in ("stable", "latest", "testing", "v1.37"):
            with self.subTest(channel=channel):
                expect_equal(resolve_k3s_version(channel), _RELEASE)
                expect_equal(
                    self.connection.request.call_args.args,
                    ("GET", f"/v1-release/channels/{channel}"),
                )
        expect_equal(self.connection.close.call_count, 4)
        expect_equal(self.response.close.call_count, 4)
        expect_equal(self.transport.call_args.kwargs, {"timeout": 10})

    def test_reject_invalid_input_before_network(self) -> None:
        """Reject incomplete pins and shell or URL syntax in configuration."""
        for value in ("", "v1.37.1", "stable/extra", "stable?x=1", 'stable"; exit 0'):
            with (
                self.subTest(value=value),
                pytest.raises(ValueError, match="k3sVersion"),
            ):
                resolve_k3s_version(value)
        expect_equal(self.transport.call_count, 0)

    def test_reject_nonredirect_status(self) -> None:
        """HTTP errors and unexpected success responses must never select stable."""
        for status in (200, 404, 500):
            self.response.status = status
            with self.subTest(status=status), pytest.raises(RuntimeError, match="HTTP"):
                resolve_k3s_version("stable")
        expect_equal(self.connection.close.call_count, 3)
        expect_equal(self.response.close.call_count, 3)

    def test_reject_missing_or_untrusted_redirect(self) -> None:
        """Require a complete official HTTPS release URL with no extra components."""
        locations = (
            "",
            "/k3s-io/k3s/releases/tag/" + _RELEASE,
            _RELEASE_URL.replace("https:", "http:"),
            _RELEASE_URL.replace("github.com", "github.com.example.org"),
            _RELEASE_URL.replace("k3s-io/k3s", "another/project"),
            _RELEASE_URL + "?download=1",
            _RELEASE_URL + "#extra",
            _RELEASE_URL.rsplit("/", 1)[0] + "/stable",
        )
        for location in locations:
            self.response.getheader.return_value = location
            with (
                self.subTest(location=location),
                pytest.raises(RuntimeError, match="official release"),
            ):
                resolve_k3s_version("stable")

    def test_timeout_closes_connection_and_propagates(self) -> None:
        """Transport failure must abort evaluation instead of choosing a fallback."""
        self.connection.request.side_effect = TimeoutError
        with pytest.raises(TimeoutError):
            resolve_k3s_version("stable")
        expect_equal(self.connection.close.call_count, 1)
