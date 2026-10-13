"""Verify external tests cannot run without their opt-ins."""

import unittest
from unittest.mock import MagicMock

import pytest
from support import stack_fixtures
from support.gating import TestOptions as Options

from .helpers import expect_equal


class TestGatingTests(unittest.TestCase):
    """Check the authorization boundaries before fixture setup."""

    def test_unit_tests_are_enabled_by_default(self) -> None:
        """Ordinary tests need no infrastructure configuration."""
        expect_equal(Options().skip_reason(set()) is None, expected=True)

    def test_each_external_operation_requires_its_option(self) -> None:
        """A development stack alone does not authorize GPU or teardown."""
        for marker in ("docker", "live", "e2e", "gpu", "lifecycle"):
            with self.subTest(marker=marker):
                expect_equal(
                    Options().skip_reason({marker}) is not None,
                    expected=True,
                )
        options = Options(stack="dev", e2e=True)
        expect_equal(options.skip_reason({"live", "e2e"}) is None, expected=True)
        expect_equal(
            options.skip_reason({"live", "e2e", "gpu"}) is not None,
            expected=True,
        )
        expect_equal(
            options.skip_reason({"live", "e2e", "lifecycle"}) is not None,
            expected=True,
        )

    def test_complete_opt_in_enables_only_the_requested_operations(self) -> None:
        """GPU authorization still leaves destructive lifecycle tests disabled."""
        options = Options(stack="dev", e2e=True, gpu=True)
        expect_equal(options.skip_reason({"live", "e2e", "gpu"}) is None, expected=True)
        expect_equal(
            options.skip_reason({"live", "e2e", "lifecycle"}) is not None,
            expected=True,
        )

    def test_managed_session_requires_an_explicit_stack(self) -> None:
        """Stack creation and bucket cleanup cannot use the workspace default."""
        config = MagicMock(spec=pytest.Config)
        options = {"--manage-stack": True}
        config.getoption.side_effect = lambda name, **_kwargs: options.get(name)
        with pytest.raises(pytest.UsageError, match="--dev-stack"):
            stack_fixtures.pytest_collection_modifyitems(config, [])

    def test_parallel_live_stack_mutation_is_rejected(self) -> None:
        """A session fixture per xdist worker would create competing owners."""
        config = MagicMock(spec=pytest.Config)
        options = {"--dev-stack": "dev", "numprocesses": 2}
        config.getoption.side_effect = lambda name, **_kwargs: options.get(name)
        item = MagicMock(spec=pytest.Item)
        item.get_closest_marker.return_value = pytest.mark.live.mark
        with pytest.raises(pytest.UsageError, match="sequentially"):
            stack_fixtures.pytest_collection_modifyitems(config, [item])
