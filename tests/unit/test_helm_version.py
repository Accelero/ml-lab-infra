# Copyright (c) 2026 David Schmid
"""Keep chart selection explicit and reject accidental floating constraints."""

import unittest

import pytest

from resources.helm_version import helm_chart_version

from .helpers import expect_equal


class HelmVersionTests(unittest.TestCase):
    """Accept stable tracking or a complete, unmodified chart pin."""

    def test_stable_delegates_non_prerelease_selection_to_helm(self) -> None:
        """The wildcard excludes prereleases under Helm's SemVer rules."""
        expect_equal(helm_chart_version("stable"), "*")

    def test_exact_versions_are_preserved(self) -> None:
        """Explicit pins can select releases, prereleases, or build metadata."""
        for version in ("0.14.0", "1.9.6", "0.14.0-rc.1", "1.2.3+build.4"):
            with self.subTest(version=version):
                expect_equal(helm_chart_version(version), version)

    def test_invalid_versions_and_constraints_are_rejected(self) -> None:
        """Only the named stable channel may select a moving version."""
        for version in (
            "",
            "latest",
            "Stable",
            " stable ",
            "*",
            ">=0.14.0",
            "~1.9.0",
            "1.9",
            "v0.14.0",
            "01.2.3",
            "1.2.3-rc.01",
            "1.2.3+",
            "1.2.3\n",
        ):
            with (
                self.subTest(version=version),
                pytest.raises(ValueError, match="exact SemVer version"),
            ):
                helm_chart_version(version)
