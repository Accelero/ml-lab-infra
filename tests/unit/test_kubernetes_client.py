# Copyright (c) 2026 David Schmid
"""Check in-memory kubeconfig loading without Kubernetes access."""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from resources import kubernetes_client

from .helpers import expect_equal


class ClientTests(unittest.TestCase):
    """Keep client configuration private to each connection."""

    def test_configuration_is_passed_explicitly(self) -> None:
        """Load the parsed kubeconfig into a separate Configuration instance."""
        configuration = MagicMock()
        with (
            patch.object(
                kubernetes_client.kubernetes.client,
                "Configuration",
                return_value=configuration,
            ),
            patch.object(
                kubernetes_client.kubernetes.config,
                "load_kube_config_from_dict",
            ) as load,
            patch.object(kubernetes_client.kubernetes.client, "ApiClient") as client,
        ):
            result = kubernetes_client.kubeconfig_client("apiVersion: v1\n")
        load.assert_called_once_with(
            {"apiVersion": "v1"},
            client_configuration=configuration,
        )
        client.assert_called_once_with(configuration=configuration)
        expect_equal(result, client.return_value)

    def test_invalid_kubeconfig_does_not_create_a_client(self) -> None:
        """Propagate configuration errors before opening a client."""
        with (
            patch.object(
                kubernetes_client.kubernetes.config,
                "load_kube_config_from_dict",
                side_effect=ValueError("invalid kubeconfig"),
            ),
            patch.object(kubernetes_client.kubernetes.client, "ApiClient") as client,
            pytest.raises(ValueError, match="invalid kubeconfig"),
        ):
            kubernetes_client.kubeconfig_client("apiVersion: v1\n")
        client.assert_not_called()
