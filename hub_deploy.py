"""Hub deployment script using pyinfra."""

from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all
from pyinfra.api.operation import add_op
from pyinfra.operations import apt, server


def setup_servers(ip: str, ssh_key: str) -> None:
    """Initialize the hub server."""
    inventory = Inventory(
        [
            (
                ip,
                {
                    "ssh_user": "root",
                    "ssh_key": ssh_key,
                },
            ),
        ],
    )
    config = Config()
    state = State(inventory, config)
    connect_all(state)

    add_op(state, apt.update, name="Update package lists")
    add_op(state, apt.upgrade, name="Upgrade all packages", auto_remove=False)
    add_op(
        state,
        apt.packages,
        name="Install basic packages",
        packages=["gnupg2", "curl", "ca-certificates", "git"],
        present=True,
        latest=True,
        update=True,
    )
    add_op(
        state,
        server.shell,
        name="Install K3s",
        commands=[
            "TS_IP=$(tailscale ip -4) && "
            'curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="server" sh -s - '
            "--flannel-iface=tailscale0 "
            "--node-ip=$TS_IP "
            "--node-external-ip=$TS_IP "
            "--bind-address=$TS_IP",
        ],
    )
