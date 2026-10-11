# Copyright (c) 2026 David Schmid
"""Independently enforce a GPU lease and verify termination."""

import json
import os
import pathlib
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from support.process import command
from support.runpod import RunPodAPI, budget_violation, owns_pod

if TYPE_CHECKING:
    from collections.abc import Callable

MAX_LIFETIME = 600
MAX_CLEANUP = 180
MAX_HOURLY_COST = Decimal("0.50")


@dataclass(frozen=True)
class Lease:
    """Bound provisioning plus execution and the subsequent cleanup period."""

    cluster: str
    lifetime: float = 600
    hourly_limit: Decimal = Decimal("0.35")
    cleanup_limit: float = 180
    quiet_period: float = 60
    poll_interval: float = 2

    def __post_init__(self) -> None:
        """Reject invalid budgets before provisioning can begin."""
        owns_pod(self.cluster, "")
        if (
            not 0 < self.lifetime <= MAX_LIFETIME
            or not self.hourly_limit.is_finite()
            or not Decimal(0) < self.hourly_limit <= MAX_HOURLY_COST
        ):
            msg = "GPU tests allow at most 600 seconds and $0.50 per hour."
            raise ValueError(msg)
        if (
            not 0
            < self.poll_interval
            <= self.quiet_period
            < self.cleanup_limit
            <= MAX_CLEANUP
        ):
            msg = "Invalid GPU cleanup deadlines."
            raise ValueError(msg)


def write_report(path: pathlib.Path, report: dict[str, object]) -> None:
    """Atomically write only ownership, status, and redacted failure information."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report))
    temporary.replace(path)


@dataclass
class CleanupState:
    """Track verified absence separately from successful delete requests."""

    deadline: float | None = None
    quiet_since: float | None = None
    violation: str | None = None
    seen: set[str] = field(default_factory=set)

    def begin(self, lease: Lease, violation: str | None = None) -> None:
        """Start cleanup once and retain the first failure."""
        self.violation = self.violation or violation
        if self.deadline is None:
            self.deadline = time.monotonic() + lease.cleanup_limit

    def sweep(self, api: RunPodAPI, lease: Lease) -> bool:
        """Delete owned pods repeatedly, including late provisioning results."""
        pods = [
            pod
            for pod in api.pods()
            if (pod.identifier in self.seen or owns_pod(lease.cluster, pod.name))
            and pod.status != "TERMINATED"
        ]
        self.seen.update(pod.identifier for pod in pods)
        problem = budget_violation(pods, lease.hourly_limit)
        if problem:
            self.begin(lease, problem)
        if self.deadline is None:
            return False
        for pod in pods:
            api.delete(pod.identifier)
        if pods:
            self.quiet_since = None
        elif self.quiet_since is None:
            self.quiet_since = time.monotonic()
        return (
            self.quiet_since is not None
            and time.monotonic() - self.quiet_since >= lease.quiet_period
        )

    def report(self, status: str) -> dict[str, object]:
        """Expose IDs for manual recovery without including API response bodies."""
        return {
            "status": status,
            "pods": sorted(self.seen),
            "violation": self.violation,
        }


def monitor(
    api: RunPodAPI,
    lease: Lease,
    report_path: pathlib.Path,
    remote_cleanup: Callable[[], None] | None = None,
) -> None:
    """Continue cleanup after runner death, normal completion, or budget expiry."""
    stop_path = report_path.with_suffix(".stop")
    parent = os.getppid()
    expires = time.monotonic() + lease.lifetime
    state = CleanupState()
    cancelled = False
    while True:
        now = time.monotonic()
        if state.deadline is None and (
            stop_path.exists() or now >= expires or os.getppid() != parent
        ):
            reason = (
                None
                if stop_path.exists()
                else "GPU lease expired or its pytest runner exited."
            )
            state.begin(lease, reason)
        try:
            if state.sweep(api, lease):
                write_report(report_path, state.report("cleaned"))
                return
            write_report(
                report_path,
                state.report(
                    "cleaning" if state.deadline is not None else "armed",
                ),
            )
        except RuntimeError, TypeError:
            state.begin(lease, "RunPod inventory or termination failed.")
            state.quiet_since = None
        if state.deadline is not None and not cancelled:
            cancelled = True
            if remote_cleanup is not None and not stop_path.exists():
                remote_cleanup()
        if state.deadline is not None and time.monotonic() >= state.deadline:
            state.violation = (
                "RunPod cleanup could not be verified before its deadline."
            )
            write_report(report_path, state.report("failed"))
            msg = "RunPod cleanup could not be verified."
            raise RuntimeError(msg)
        time.sleep(lease.poll_interval)


def main() -> None:
    """Read the API key from stdin so it never appears in argv or the journal."""
    configuration = json.load(sys.stdin)
    lease = Lease(
        cluster=configuration["cluster"],
        lifetime=configuration.get("lifetime", 600),
        hourly_limit=Decimal(str(configuration.get("hourly_limit", "0.35"))),
        cleanup_limit=configuration.get("cleanup_limit", 180),
        quiet_period=configuration.get("quiet_period", 60),
        poll_interval=configuration.get("poll_interval", 2),
    )
    api = RunPodAPI(
        configuration["api_key"],
        configuration.get("base_url", "https://rest.runpod.io/v1"),
    )

    def cancel_remote() -> None:
        if "remote_cleanup" in configuration:
            try:
                command(
                    [sys.executable, "-m", "support.gpu_worker"],
                    timeout=30,
                    data=json.dumps(
                        configuration["remote_cleanup"] | {"action": "cleanup"},
                    ),
                )
            except RuntimeError:
                # The direct RunPod sweep must continue even if SkyPilot is down.
                return

    monitor(api, lease, pathlib.Path(configuration["report_path"]), cancel_remote)


if __name__ == "__main__":
    main()
