# Copyright (c) 2026 David Schmid
"""Assertions compatible with the repository's ban on assert statements."""

import pytest


def expect_equal(actual: object, expected: object) -> None:
    """Report differing values without relying on optimization-sensitive asserts."""
    if actual != expected:
        pytest.fail(f"Expected {expected!r}, got {actual!r}")
