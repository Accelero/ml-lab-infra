# Copyright (c) 2026 David Schmid
"""Gate live operations and provide one stack per pytest session."""

from typing import TYPE_CHECKING

import pytest

from support.gating import TestOptions
from support.managed_stack import managed_stack
from support.stack import DevStack

if TYPE_CHECKING:
    from collections.abc import Generator


def pytest_addoption(parser: pytest.Parser) -> None:
    """Require explicit development-stack and spending opt-ins."""
    group = parser.getgroup("ml-lab tests")
    group.addoption("--dev-stack", help="Disposable development stack for live tests")
    for option in ("docker", "e2e", "gpu", "lifecycle", "manage-stack"):
        group.addoption(
            f"--run-{option}" if option != "manage-stack" else "--manage-stack",
            action="store_true",
            default=False,
        )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip unauthorized fixtures and keep stack mutation sequential."""
    options = TestOptions(
        stack=config.getoption("--dev-stack"),
        docker=config.getoption("--run-docker"),
        e2e=config.getoption("--run-e2e"),
        gpu=config.getoption("--run-gpu"),
        lifecycle=config.getoption("--run-lifecycle"),
        manage_stack=config.getoption("--manage-stack"),
    )
    if (
        options.e2e or options.gpu or options.lifecycle or options.manage_stack
    ) and not options.stack:
        msg = "Live options require --dev-stack NAME."
        raise pytest.UsageError(msg)
    if (options.gpu or options.lifecycle) and not options.e2e:
        msg = "GPU and lifecycle tests also require --run-e2e."
        raise pytest.UsageError(msg)
    live = [item for item in items if item.get_closest_marker("live") is not None]
    if (
        options.stack
        and live
        and (
            hasattr(config, "workerinput")
            or config.getoption("numprocesses", default=0)
        )
    ):
        msg = "Live stack tests must run sequentially without pytest-xdist."
        raise pytest.UsageError(msg)
    for item in items:
        reason = options.skip_reason({marker.name for marker in item.iter_markers()})
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
        if options.manage_stack:
            timeout = item.get_closest_marker("timeout")
            seconds = timeout.args[0] if timeout and timeout.args else 120
            item.add_marker(pytest.mark.timeout(seconds, func_only=True), append=False)


@pytest.fixture(scope="session")
def dev_stack(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[DevStack]:
    """Optionally build once; dependent fixture teardown precedes stack destruction."""
    name = request.config.getoption("--dev-stack")
    if not name:
        pytest.fail("Live tests require --dev-stack NAME.")
    stack = DevStack(name)
    stack.preflight()
    if request.config.getoption("--manage-stack"):
        directory = tmp_path_factory.mktemp("stack-session")
        with managed_stack(stack, directory / "cleanup.json") as deployed:
            yield deployed
    else:
        yield stack
