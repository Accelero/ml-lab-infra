# Copyright (c) 2026 David Schmid
"""Launch one cheap GPU under a detached RunPod cleanup watchdog."""

import asyncio
import json
import sys
from typing import TYPE_CHECKING

from support.process import REPO_ROOT, execute, process_environment
from support.stack import secret_json

if TYPE_CHECKING:
    import pathlib

    from support.runpod_watchdog import Lease


async def _wait_armed(
    report_path: pathlib.Path,
    guard: asyncio.subprocess.Process,
) -> None:
    async with asyncio.timeout(30):
        while True:
            if await asyncio.to_thread(report_path.exists):
                status = json.loads(await asyncio.to_thread(report_path.read_text))[
                    "status"
                ]
                if status == "armed":
                    return
                msg = "RunPod watchdog did not arm."
                raise RuntimeError(msg)
            if guard.returncode is not None:
                msg = "RunPod watchdog exited before provisioning."
                raise RuntimeError(msg)
            await asyncio.sleep(0.1)


async def run_gpu_job(
    configuration: dict,
    lease: Lease,
    directory: pathlib.Path,
) -> None:
    """Arm before launch and verify direct API cleanup on all exit paths."""
    report = directory / "runpod-cleanup.json"
    request_file = directory / "sky-request-id"
    worker = {
        "endpoint": configuration["endpoint"],
        "cluster": lease.cluster,
        "request_file": str(request_file),
    }
    guard = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "support.runpod_watchdog",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=REPO_ROOT,
        env=process_environment(),
        start_new_session=True,
    )
    if guard.stdin is None:
        msg = "RunPod watchdog has no configuration pipe."
        raise RuntimeError(msg)
    guard.stdin.write(
        json.dumps(
            {
                "api_key": configuration["api_key"],
                "cluster": lease.cluster,
                "lifetime": lease.lifetime,
                "hourly_limit": str(lease.hourly_limit),
                "cleanup_limit": lease.cleanup_limit,
                "quiet_period": lease.quiet_period,
                "poll_interval": lease.poll_interval,
                "report_path": str(report),
                "remote_cleanup": worker,
            },
        ).encode(),
    )
    await guard.stdin.drain()
    guard.stdin.close()
    try:
        await _wait_armed(report, guard)
        await execute(
            [sys.executable, "-m", "support.gpu_worker"],
            limit=lease.lifetime,
            data=secret_json(
                worker | {"action": "launch", "task": configuration["task"]},
            ),
        )
    finally:
        try:
            await execute(
                [sys.executable, "-m", "support.gpu_worker"],
                limit=30,
                data=secret_json(worker | {"action": "cleanup"}),
            )
        finally:
            await asyncio.to_thread(report.with_suffix(".stop").touch)
            async with asyncio.timeout(lease.cleanup_limit + 20):
                await guard.wait()
            result = (
                json.loads(await asyncio.to_thread(report.read_text))
                if await asyncio.to_thread(report.exists)
                else {}
            )
            if result.get("status") != "cleaned":
                msg = f"RunPod termination was not verified; inspect {report}."
                raise RuntimeError(msg)
            if result.get("violation"):
                msg = f"GPU watchdog reported a failure; inspect {report}."
                raise RuntimeError(msg)
            if not result.get("pods"):
                msg = "GPU watchdog never observed a test-owned pod."
                raise RuntimeError(msg)
