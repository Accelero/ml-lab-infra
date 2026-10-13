"""Check a fresh stack's actual state export using only a local Pulumi backend."""

import json
import shutil
from typing import TYPE_CHECKING

import pytest
from support.process import command
from support.stack import DevStack

if TYPE_CHECKING:
    import pathlib

pytestmark = pytest.mark.integration


def test_new_local_stack_has_empty_deployment_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating a local state record requires no cloud credentials or resources."""
    pulumi = shutil.which("pulumi")
    if pulumi is None:
        pytest.skip("This local CLI integration requires Pulumi.")
    monkeypatch.setenv("PULUMI_HOME", str(tmp_path / "pulumi-home"))
    monkeypatch.setenv("PULUMI_BACKEND_URL", (tmp_path / "backend").as_uri())
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE", "isolated-test-state")
    monkeypatch.setenv("PULUMI_SKIP_UPDATE_CHECK", "true")
    (tmp_path / "backend").mkdir()
    (tmp_path / "Pulumi.yaml").write_text("name: fixture-check\nruntime: python\n")
    command([pulumi, "stack", "init", "dev", "--cwd", str(tmp_path)])
    state = command(
        [pulumi, "stack", "export", "--stack", "dev", "--cwd", str(tmp_path)],
    )
    monkeypatch.setattr(DevStack, "pulumi", lambda _self, *_args: state)
    DevStack("dev").require_empty()
    parsed = json.loads(state)
    if parsed.get("deployment", {}).get("resources"):
        pytest.fail("A new local stack unexpectedly contains deployed resources.")
