# Copyright (c) 2026 David Schmid
"""Validate chart pins and delegate stable release selection to Helm."""

import semver


def helm_chart_version(version: str) -> str:
    """Return an exact chart version or Helm's non-prerelease constraint."""
    if version == "stable":
        return "*"
    if not semver.Version.is_valid(version):
        msg = "Helm chart version must be 'stable' or an exact SemVer version."
        raise ValueError(msg)
    return version
