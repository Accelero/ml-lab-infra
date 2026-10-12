# Copyright (c) 2026 David Schmid
"""Own one disposable stack and its persisted state for a pytest session."""

import json
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from support.storage import Bucket

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator

    from support.stack import DevStack


@dataclass
class StackSession:
    """Retain ownership evidence when stack deletion or bucket cleanup fails."""

    stack: DevStack
    buckets: list[Bucket]
    journal: pathlib.Path
    token: str
    started: bool = False
    cleaned: bool = False

    def record(self, status: str) -> None:
        """Write metadata for manual recovery without configuration or secrets."""
        temporary = self.journal.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "status": status,
                    "stack": self.stack.name,
                    "buckets": [bucket.name for bucket in self.buckets],
                    "owner": self.token,
                    "deployment_attempted": self.started,
                    "cleanup_verified": self.cleaned,
                },
            ),
        )
        temporary.replace(self.journal)

    def release_unstarted(self, bucket: Bucket) -> None:
        """Rollback bucket claims if deployment was never attempted."""
        if not self.started:
            bucket.release_claim(self.token)

    def cleanup(self) -> None:
        """Verify stack deletion before purging the backup chain and artifacts."""
        self.stack.pulumi("destroy", "--yes", "--non-interactive", timeout=1200)
        self.stack.require_empty()
        errors = []
        for bucket in self.buckets:
            try:
                bucket.clear_owned(self.token)
            except RuntimeError, TimeoutError:
                errors.append(
                    RuntimeError(f"Cleanup failed for test bucket {bucket.name}."),
                )
        if errors:
            msg = f"Test storage cleanup failed; inspect {self.journal}."
            raise ExceptionGroup(msg, errors)
        self.cleaned = True
        self.record("cleaned")


@contextmanager
def managed_stack(stack: DevStack, journal: pathlib.Path) -> Generator[DevStack]:
    """Build once and clean up even when setup fails before yielding."""
    stack.require_empty()
    with ExitStack() as connections:
        buckets = []
        for namespace in ("backup", "mlflow"):
            bucket = Bucket.from_stack(stack, namespace)
            connections.callback(bucket.client.close)
            buckets.append(bucket)
        if len({(bucket.endpoint, bucket.name) for bucket in buckets}) != len(buckets):
            msg = "Backup and artifact test buckets must be distinct."
            raise ValueError(msg)
        for bucket in buckets:
            bucket.require_empty()
        session = StackSession(stack, buckets, journal, uuid.uuid4().hex)
        try:
            with ExitStack() as claims:
                for bucket in buckets:
                    claims.callback(session.release_unstarted, bucket)
                    bucket.claim(session.token)
                session.record("claimed")
                stack.pulumi("preview", "--non-interactive", timeout=600)
                session.started = True
                try:
                    stack.pulumi("up", "--yes", "--non-interactive", timeout=1800)
                    session.record("running")
                    yield stack
                finally:
                    session.cleanup()
        except BaseException:
            with suppress(OSError):
                session.record("failed")
            raise


@contextmanager
def recreated_stack(stack: DevStack) -> Generator[DevStack]:
    """Preserve storage while restoring a failed destroy/recreate attempt."""
    stack.pulumi("preview", "--non-interactive", timeout=600)
    restored = False
    try:
        stack.pulumi("destroy", "--yes", "--non-interactive", timeout=1200)
        stack.pulumi("up", "--yes", "--refresh", "--non-interactive", timeout=1800)
        restored = True
        yield stack
    finally:
        if not restored:
            stack.pulumi("up", "--yes", "--refresh", "--non-interactive", timeout=1800)
