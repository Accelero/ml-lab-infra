"""Run actual pytest sessions with fake cloud transports to verify fixture order."""

import json
import sys
from typing import TYPE_CHECKING

import pytest
from support.process import REPO_ROOT, command

if TYPE_CHECKING:
    import pathlib

pytestmark = pytest.mark.integration
FUNCTION_TEST_COUNT = 2
BUCKET_COUNT = 2

CONFIGURATION = """
import json, pathlib, time
from support import managed_stack, stack_fixtures
from support.stack import DevStack
import pytest

pytest_plugins = ["support.stack_fixtures"]
directory = pathlib.Path(__file__).parent
scenario = json.loads((directory / "scenario.json").read_text())
def record(event):
    with (directory / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\\n")

class FakeStack(DevStack):
    def preflight(self):
        record("preflight")
    def require_empty(self):
        record("empty-stack")
    def pulumi(self, action, *args, **kwargs):
        record(action)
        if action == "up" and scenario == "up_failure":
            raise RuntimeError("injected deployment failure")
        if action == "up" and scenario == "slow_setup":
            time.sleep(0.2)
        if action == "destroy" and scenario == "trailing_local":
            time.sleep(0.2)
        return ""

class FakeBucket:
    def __init__(self, name):
        self.name = name
        self.endpoint = "https://unused.invalid"
        self.client = self
    def require_empty(self):
        record("empty-bucket")
    def claim(self, token):
        record("claim")
    def release_claim(self, token):
        record("release-claim")
    def clear_owned(self, token):
        record("purge")
    def close(self):
        record("close")

stack_fixtures.DevStack = FakeStack
managed_stack.Bucket.from_stack = classmethod(
    lambda cls, stack, namespace: FakeBucket(namespace)
)

@pytest.fixture
def owned_data(dev_stack):
    record("data-setup")
    try:
        yield dev_stack
    finally:
        record("data-cleanup")
"""

TESTS = """
import pytest
from conftest import record, scenario
pytestmark = [pytest.mark.live, pytest.mark.timeout(0.05)]

def test_first(owned_data):
    record("first-test")
    if scenario == "test_failure":
        pytest.fail("injected assertion failure")

def test_second(owned_data):
    record("second-test")
"""


@pytest.mark.parametrize(
    "scenario",
    ["success", "test_failure", "up_failure", "slow_setup", "trailing_local"],
)
def test_real_pytest_session_builds_once_and_always_finalizes(
    tmp_path: pathlib.Path,
    scenario: str,
) -> None:
    """Dependent data teardown runs before final stack and storage cleanup."""
    (tmp_path / "conftest.py").write_text(CONFIGURATION)
    (tmp_path / "test_session.py").write_text(TESTS)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(REPO_ROOT / "pyproject.toml"),
        "--confcutdir",
        str(tmp_path),
        "--basetemp",
        str(tmp_path / "pytest-temp"),
        "-q",
        "-o",
        "addopts=",
        "-o",
        "log_cli=false",
        str(tmp_path / "test_session.py"),
        "--dev-stack",
        "fake-dev",
        "--manage-stack",
    ]
    if scenario == "trailing_local":
        trailing = tmp_path / "test_local_tail.py"
        trailing.write_text(
            "import pytest\nfrom conftest import record\n"
            "@pytest.mark.timeout(0.05)\n"
            "def test_local_tail():\n    record('local-tail')\n",
        )
        args.append(str(trailing))
    if scenario in {"test_failure", "up_failure"}:
        with pytest.raises(RuntimeError, match="exit status"):
            command(args, timeout=30)
    else:
        command(args, timeout=30)
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    if (
        events.count("up") != 1
        or events.count("destroy") != 1
        or events.count("purge") != BUCKET_COUNT
    ):
        pytest.fail("The session did not build once and clean up exactly once.")
    if scenario != "up_failure":
        if (
            events.count("data-setup") != FUNCTION_TEST_COUNT
            or events.count("data-cleanup") != FUNCTION_TEST_COUNT
        ):
            pytest.fail("Each test did not receive independent setup and teardown.")
        last_data_cleanup = max(
            index for index, event in enumerate(events) if event == "data-cleanup"
        )
        if last_data_cleanup >= events.index("destroy"):
            pytest.fail("Stack destruction ran before dependent data cleanup.")
    if events.index("destroy") >= events.index("purge"):
        pytest.fail("Storage was purged before stack destruction.")
    journal = next((tmp_path / "pytest-temp").glob("stack-session*/cleanup.json"))
    if not json.loads(journal.read_text())["cleanup_verified"]:
        pytest.fail("The session did not record verified cleanup.")
