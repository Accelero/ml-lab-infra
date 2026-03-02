import dis
import hashlib
from collections.abc import Callable

from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider


def get_function_logic_hash(func: Callable) -> str:
    """Hashes the logic of a Python function."""
    # co_code: The actual opcodes (instructions)
    # co_consts: The literal values used in the function
    logic_data = func.__code__.co_code + str(func.__code__.co_consts).encode()
    return hashlib.sha256(logic_data).hexdigest()


class PyinfraProvider(ResourceProvider):
    def diff(self, id, old_props, new_props):
        replaces = []

        # 1. Check if the Bytecode changed
        if old_props.get("logic_hash") != new_props.get("logic_hash"):
            replaces.append("logic_hash")

        # 2. Check if the Inputs (IP or SSH Key) changed
        if old_props.get("ip") != new_props.get("ip"):
            replaces.append("ip")
        if old_props.get("ssh_key") != new_props.get("ssh_key"):
            replaces.append("ssh_key")

        return DiffResult(
            changes=len(replaces) > 0,
            replaces=replaces,
            delete_before_replace=True,  # Important: Ensures clean re-run
        )

    def create(self, props):
        # Import your setup function here or ensure it's in scope
        from pyinfra.hub_deploy import setup_servers

        setup_servers(ip=props["ip"], ssh_key=props["ssh_key"])
        return CreateResult(id_=f"pyinfra-{props['ip']}", outs=props)


class PyinfraDeployment(Resource):
    def __init__(self, name, ip, ssh_key, logic_func, opts=None):
        props = {
            "ip": ip,
            "ssh_key": ssh_key,
            "logic_hash": get_function_logic_hash(logic_func),
        }
        super().__init__(PyinfraProvider(), name, props, opts)
