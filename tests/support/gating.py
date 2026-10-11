# Copyright (c) 2026 David Schmid
"""Keep infrastructure and paid test operations explicitly opt-in."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TestOptions:
    """Describe the operations authorized for this pytest invocation."""

    stack: str | None = None
    docker: bool = False
    e2e: bool = False
    gpu: bool = False
    lifecycle: bool = False

    def skip_reason(self, markers: set[str]) -> str | None:
        """Return the missing opt-in before any fixture can access infrastructure."""
        requirements = (
            ("docker", self.docker, "--run-docker"),
            ("live", bool(self.stack), "--dev-stack NAME"),
            ("e2e", self.e2e, "--run-e2e"),
            ("gpu", self.gpu, "--run-gpu"),
            ("lifecycle", self.lifecycle, "--run-lifecycle"),
        )
        for marker, enabled, option in requirements:
            if marker in markers and not enabled:
                return f"Test requires {option}."
        return None
