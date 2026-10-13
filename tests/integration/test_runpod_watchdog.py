"""Exercise real HTTP and watchdog subprocesses against a local RunPod API double."""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import pytest
from support.process import command
from support.runpod import RunPodAPI

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator

CLUSTER = "mltest-0123456789abcdef"
pytestmark = pytest.mark.integration


@dataclass
class Inventory:
    """Model owned pods, foreign pods, failures, and delayed cloud creation."""

    pods: list[dict] = field(
        default_factory=lambda: [
            {
                "id": "owned",
                "name": f"{CLUSTER}-abcdef12-head",
                "desiredStatus": "RUNNING",
                "costPerHr": "0.20",
                "gpuCount": 1,
            },
            {
                "id": "foreign",
                "name": "production-head",
                "desiredStatus": "RUNNING",
                "costPerHr": "2.00",
                "gpuCount": 8,
            },
        ],
    )
    deletions: list[str] = field(default_factory=list)
    late: bool = False
    fail_deletion: bool = False
    base_url: str = ""


@pytest.fixture
def inventory() -> Generator[Inventory]:
    """Run a local API server without contacting RunPod or creating hardware."""
    state = Inventory()

    class Handler(BaseHTTPRequestHandler):
        """Serve the subset of the official API used by the watchdog."""

        def do_GET(self) -> None:
            """Return an inventory, including a delayed second owned pod."""
            if state.late and state.deletions == ["owned"]:
                state.pods.append(
                    {
                        "id": "late",
                        "name": f"{CLUSTER}-abcdef12-worker",
                        "desiredStatus": "RUNNING",
                        "costPerHr": "0.20",
                        "gpuCount": 1,
                    },
                )
                state.late = False
            body = json.dumps(state.pods).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_DELETE(self) -> None:
            """Delete a pod or simulate a provider outage."""
            identifier = self.path.rsplit("/", 1)[-1]
            if state.fail_deletion:
                self.send_response(503)
            else:
                state.deletions.append(identifier)
                state.pods = [pod for pod in state.pods if pod["id"] != identifier]
                self.send_response(204)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            """Keep HTTP bodies and headers out of pytest output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def configuration(state: Inventory, directory: pathlib.Path) -> dict:
    """Use tiny local deadlines while retaining the production watchdog code."""
    return {
        "api_key": "local-test-key",
        "base_url": state.base_url,
        "cluster": CLUSTER,
        "lifetime": 0.2,
        "hourly_limit": "0.35",
        "cleanup_limit": 2,
        "quiet_period": 0.1,
        "poll_interval": 0.02,
        "report_path": str(directory / "cleanup.json"),
    }


def test_rest_inventory_and_delete(inventory: Inventory) -> None:
    """Real HTTP requests parse inventory and release exactly the requested pod."""
    api = RunPodAPI("local-test-key", inventory.base_url)
    if {pod.identifier for pod in api.pods()} != {"owned", "foreign"}:
        pytest.fail("The API client did not parse the inventory.")
    api.delete("owned")
    if [pod.identifier for pod in api.pods()] != ["foreign"]:
        pytest.fail("The API client did not verify pod deletion.")


def test_watchdog_expires_and_preserves_foreign_pods(
    inventory: Inventory,
    tmp_path: pathlib.Path,
) -> None:
    """A separate process enforces the lease and reports observed absence."""
    config = configuration(inventory, tmp_path)
    command(
        [sys.executable, "-m", "support.runpod_watchdog"],
        data=json.dumps(config),
        timeout=5,
    )
    report = json.loads((tmp_path / "cleanup.json").read_text())
    if report["status"] != "cleaned" or report["pods"] != ["owned"]:
        pytest.fail("The watchdog did not verify owned hardware cleanup.")
    if inventory.deletions != ["owned"] or inventory.pods[0]["id"] != "foreign":
        pytest.fail("The watchdog changed unrelated hardware.")


def test_watchdog_removes_late_pods(
    inventory: Inventory,
    tmp_path: pathlib.Path,
) -> None:
    """Provisioning results arriving after cleanup begins cannot escape its sweep."""
    inventory.late = True
    command(
        [sys.executable, "-m", "support.runpod_watchdog"],
        data=json.dumps(configuration(inventory, tmp_path)),
        timeout=5,
    )
    if inventory.deletions != ["owned", "late"]:
        pytest.fail("The watchdog missed a late provisioning result.")


def test_watchdog_releases_over_budget_hardware_before_lease_expiry(
    inventory: Inventory,
    tmp_path: pathlib.Path,
) -> None:
    """The actual provider rate triggers immediate termination independently."""
    inventory.pods[0]["costPerHr"] = "0.80"
    config = configuration(inventory, tmp_path)
    config["lifetime"] = 60
    command(
        [sys.executable, "-m", "support.runpod_watchdog"],
        data=json.dumps(config),
        timeout=5,
    )
    report = json.loads((tmp_path / "cleanup.json").read_text())
    if (
        report["status"] != "cleaned"
        or report["violation"] != "GPU test exceeded its hourly price limit."
        or inventory.deletions != ["owned"]
    ):
        pytest.fail("The watchdog did not promptly enforce the actual hourly rate.")


def test_watchdog_fails_when_provider_will_not_release_hardware(
    inventory: Inventory,
    tmp_path: pathlib.Path,
) -> None:
    """Cleanup failure remains a test failure with recoverable pod IDs."""
    inventory.fail_deletion = True
    with pytest.raises(RuntimeError, match="exit status"):
        command(
            [sys.executable, "-m", "support.runpod_watchdog"],
            data=json.dumps(configuration(inventory, tmp_path)),
            timeout=5,
        )
    report = json.loads((tmp_path / "cleanup.json").read_text())
    if report["status"] != "failed" or report["pods"] != ["owned"]:
        pytest.fail("The watchdog incorrectly claimed hardware release.")
    if "local-test-key" in json.dumps(report):
        pytest.fail("The watchdog journal exposed the API key.")


def test_watchdog_survives_its_runner_exiting(
    inventory: Inventory,
    tmp_path: pathlib.Path,
) -> None:
    """A detached watchdog still releases owned pods when its parent dies."""
    config = configuration(inventory, tmp_path)
    config["lifetime"] = 60
    script = """
import asyncio, json, os, pathlib, sys
from support.process import process_environment
configuration = json.load(sys.stdin)
async def abandon():
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'support.runpod_watchdog',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        env=process_environment(),
    )
    process.stdin.write(json.dumps(configuration).encode())
    await process.stdin.drain()
    process.stdin.close()
    async with asyncio.timeout(3):
        while not pathlib.Path(configuration['report_path']).exists():
            await asyncio.sleep(0.01)
    os._exit(0)
asyncio.run(abandon())
"""
    command([sys.executable, "-c", script], data=json.dumps(config), timeout=5)
    deadline = time.monotonic() + 5
    path = tmp_path / "cleanup.json"
    while time.monotonic() < deadline:
        report = json.loads(path.read_text())
        if report["status"] == "cleaned":
            break
        time.sleep(0.02)
    else:
        pytest.fail("The watchdog did not clean up after its runner exited.")
    if inventory.deletions != ["owned"] or report["pods"] != ["owned"]:
        pytest.fail("Cleanup after runner death changed the wrong pods.")
