"""Create Kubernetes clients from in-memory kubeconfigs."""

import kubernetes.client
import kubernetes.config
import yaml


def kubeconfig_client(kubeconfig: str) -> kubernetes.client.ApiClient:
    """Load kubeconfig without changing the default client configuration."""
    configuration = kubernetes.client.Configuration()
    kubernetes.config.load_kube_config_from_dict(
        yaml.safe_load(kubeconfig),
        client_configuration=configuration,
    )
    return kubernetes.client.ApiClient(configuration=configuration)
