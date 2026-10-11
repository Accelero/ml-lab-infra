# Copyright (c) 2026 David Schmid
"""Execute test subprocesses with deadlines and redacted failures."""

import asyncio
import os
import pathlib

REPO_ROOT = pathlib.Path(__file__).parents[2]


def process_environment() -> dict[str, str]:
    """Make support modules importable in isolated child processes."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [
                str(REPO_ROOT),
                str(REPO_ROOT / "tests"),
                os.environ.get("PYTHONPATH", ""),
            ],
        ),
    }


async def execute(
    args: list[str],
    *,
    limit: float = 30,
    data: str | None = None,
) -> str:
    """Kill and reap timed-out commands without exposing their input or output."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=REPO_ROOT,
        env=process_environment(),
    )
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(data.encode() if data is not None else None),
            limit,
        )
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        msg = f"Test command failed with exit status {process.returncode}."
        raise RuntimeError(msg)
    return stdout.decode().strip()


def command(args: list[str], *, timeout: float = 30, data: str | None = None) -> str:
    """Run an asynchronous subprocess from a synchronous pytest fixture."""
    try:
        return asyncio.run(execute(args, limit=timeout, data=data))
    except OSError, TimeoutError:
        msg = "Test command could not complete within its deadline."
        raise RuntimeError(msg) from None
