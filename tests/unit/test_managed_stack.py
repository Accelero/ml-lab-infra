"""Check disposable stack ownership and cleanup across setup and test failures."""

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from support import managed_stack as lifecycle
from support.managed_stack import managed_stack, recreated_stack
from support.stack import DevStack

from .helpers import expect_equal

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
def dependencies() -> tuple[MagicMock, list[MagicMock], list[str]]:
    """Observe stack and bucket operations without external calls."""
    events = []
    stack = MagicMock(name="stack")
    stack.name = "dev"
    stack.require_empty.side_effect = lambda: events.append("empty-stack")
    stack.pulumi.side_effect = lambda action, *_args, **_kwargs: events.append(action)
    buckets = []
    for name in ("backup-dev", "artifacts-dev"):
        bucket = MagicMock()
        bucket.name = name
        bucket.endpoint = "https://storage.invalid"
        bucket.require_empty.side_effect = lambda: events.append("empty-bucket")
        bucket.claim.side_effect = lambda _token: events.append("claim")
        bucket.clear_owned.side_effect = lambda _token: events.append("purge")
        buckets.append(bucket)
    return stack, buckets, events


def test_session_builds_once_and_purges_only_after_verified_destroy(
    dependencies: tuple,
    tmp_path: pathlib.Path,
) -> None:
    """The backup chain remains available through deployment and all test work."""
    stack, buckets, events = dependencies
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        managed_stack(stack, tmp_path / "cleanup.json") as deployed,
    ):
        expect_equal(deployed, stack)
        expect_equal(
            events,
            [
                "empty-stack",
                "empty-bucket",
                "empty-bucket",
                "claim",
                "claim",
                "preview",
                "up",
            ],
        )
        events.append("test")
    expect_equal(events[-5:], ["test", "destroy", "empty-stack", "purge", "purge"])
    expect_equal(
        json.loads((tmp_path / "cleanup.json").read_text())["status"],
        "cleaned",
    )
    for bucket in buckets:
        bucket.client.close.assert_called_once()


@pytest.mark.parametrize("failure_phase", ["setup", "test"])
def test_setup_and_test_failures_still_destroy_and_purge(
    dependencies: tuple,
    tmp_path: pathlib.Path,
    failure_phase: str,
) -> None:
    """The finalizer is installed before pulumi up can partially succeed."""
    stack, buckets, events = dependencies
    if failure_phase == "setup":

        def fail_up(action: str, *_args: str, **_kwargs: object) -> None:
            events.append(action)
            if action == "up":
                msg = "injected failure"
                raise RuntimeError(msg)

        stack.pulumi.side_effect = fail_up
    msg = "injected failure"
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        pytest.raises(RuntimeError, match="injected failure"),
        managed_stack(stack, tmp_path / "cleanup.json"),
    ):
        raise RuntimeError(msg)
    expect_equal(events[-4:], ["destroy", "empty-stack", "purge", "purge"])


def test_failed_destroy_retains_archives_and_ownership(
    dependencies: tuple,
    tmp_path: pathlib.Path,
) -> None:
    """A failed teardown cannot destroy the data needed to retry it."""
    stack, buckets, _events = dependencies
    stack.pulumi.side_effect = ["", "", RuntimeError("destroy failed")]
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        pytest.raises(RuntimeError, match="destroy failed"),
        managed_stack(stack, tmp_path / "cleanup.json"),
    ):
        pass
    for bucket in buckets:
        bucket.clear_owned.assert_not_called()
        bucket.release_claim.assert_not_called()
    expect_equal(
        json.loads((tmp_path / "cleanup.json").read_text())["status"],
        "failed",
    )


def test_partial_bucket_failure_does_not_skip_the_other_bucket(
    dependencies: tuple,
    tmp_path: pathlib.Path,
) -> None:
    """Each bucket gets a cleanup attempt and any failure fails the session."""
    stack, buckets, _events = dependencies
    buckets[0].clear_owned.side_effect = RuntimeError("provider denied deletion")
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        pytest.raises(ExceptionGroup, match="storage cleanup failed"),
        managed_stack(stack, tmp_path / "cleanup.json"),
    ):
        pass
    buckets[1].clear_owned.assert_called_once()


def test_failed_second_claim_releases_first_without_deploying(
    dependencies: tuple,
    tmp_path: pathlib.Path,
) -> None:
    """A partial storage claim is cleaned up before any paid VM exists."""
    stack, buckets, _events = dependencies
    buckets[1].claim.side_effect = RuntimeError("already claimed")
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        pytest.raises(RuntimeError, match="already claimed"),
        managed_stack(stack, tmp_path / "cleanup.json"),
    ):
        pytest.fail("A failed claim must not yield the stack.")
    stack.pulumi.assert_not_called()
    for bucket in buckets:
        bucket.release_claim.assert_called_once()


def test_nonempty_stack_or_bucket_prevents_creation(
    dependencies: tuple,
    tmp_path: pathlib.Path,
) -> None:
    """A reusable development deployment cannot be claimed for wholesale cleanup."""
    stack, buckets, _events = dependencies
    buckets[0].require_empty.side_effect = RuntimeError("not empty")
    with (
        patch.object(lifecycle.Bucket, "from_stack", side_effect=buckets),
        pytest.raises(RuntimeError, match="not empty"),
        managed_stack(stack, tmp_path / "cleanup.json"),
    ):
        pytest.fail("Nonempty storage must not yield the stack.")
    stack.pulumi.assert_not_called()
    for bucket in buckets:
        bucket.claim.assert_not_called()


def test_failed_recreation_attempts_restoration_and_keeps_storage() -> None:
    """Recovery setup failure gets one bounded restoration attempt."""
    stack = MagicMock()
    stack.pulumi.side_effect = ["", "", RuntimeError("up failed"), ""]
    with pytest.raises(RuntimeError, match="up failed"), recreated_stack(stack):
        pytest.fail("Failed recovery must not yield.")
    expect_equal(
        [call.args[0] for call in stack.pulumi.call_args_list],
        ["preview", "destroy", "up", "up"],
    )


@pytest.mark.parametrize("resources", [[], [{"type": "pulumi:pulumi:Stack"}]])
def test_empty_stack_accepts_only_root_metadata(resources: list) -> None:
    """The stack export stays encrypted and is never written to disk."""
    with patch.object(
        DevStack,
        "pulumi",
        return_value=json.dumps({"deployment": {"resources": resources}}),
    ) as pulumi:
        DevStack("dev").require_empty()
    pulumi.assert_called_once_with("stack", "export")


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"deployment": {}},
        {"deployment": {"resources": [{"type": "hcloud:index/server:Server"}]}},
        {"deployment": {"resources": [None]}},
        {"deployment": {"resources": [], "pending_operations": [{"type": "creating"}]}},
    ],
)
def test_nonempty_or_malformed_stack_export_is_rejected(state: dict) -> None:
    """Unknown state cannot authorize ownership or bucket purging."""
    with (
        patch.object(DevStack, "pulumi", return_value=json.dumps(state)),
        pytest.raises(RuntimeError, match="without deployed resources"),
    ):
        DevStack("dev").require_empty()
