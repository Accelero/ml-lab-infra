from pyinfra.api import Config, Inventory, State
from pyinfra.api.connect import connect_all
from pyinfra.api.operation import add_op
from pyinfra.operations import apt, server


def setup_servers(iphub_ssh_key: str) -> None:
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

    add_op(state, apt.packages, packages=["tailscale"])

    tailscel = apt.packages(
        state, name="Install Tailscale", packages=["tailscale"], update=True
    )
    add_op(state, tailscel)
