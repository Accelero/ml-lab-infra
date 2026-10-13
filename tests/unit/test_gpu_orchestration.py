"""Ensure GPU provisioning exceptions still cancel requests and verify RunPod."""

import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from support import gpu
from support.process import command
from support.runpod_watchdog import Lease
from support.stack import SecretText, SecretValues, secret_json

from .helpers import expect_equal

CLUSTER = "mltest-0123456789abcdef"


class GPUOrchestrationTests(unittest.TestCase):
    """Exercise finalizers without launching SkyPilot or contacting RunPod."""

    def test_worker_initializes_sdk_with_the_remote_endpoint(self) -> None:
        """A fresh worker must not cache localhost before reading its endpoint."""
        endpoint = "https://skypilot.test.invalid"
        observed = command(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from support.gpu_worker import _sky_client; "
                    "_sky_client(sys.argv[1]); "
                    "from sky.server import common; "
                    "print(common.get_server_url())"
                ),
                endpoint,
            ],
            timeout=30,
        )
        expect_equal(observed, endpoint)

    def test_launch_timeout_still_requests_cleanup_and_waits_for_watchdog(self) -> None:
        """A timeout before a job ID exists cannot bypass the direct API proof."""
        guard = MagicMock()
        guard.stdin.drain = AsyncMock()
        guard.wait = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "runpod-cleanup.json").write_text(
                json.dumps(
                    {
                        "status": "cleaned",
                        "pods": ["owned"],
                        "violation": None,
                    },
                ),
            )
            with (
                patch.object(
                    gpu.asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(return_value=guard),
                ),
                patch.object(gpu, "_wait_armed", new=AsyncMock()),
                patch.object(
                    gpu,
                    "execute",
                    new=AsyncMock(side_effect=[TimeoutError, ""]),
                ) as execute,
                pytest.raises(TimeoutError),
            ):
                asyncio.run(
                    gpu.run_gpu_job(
                        SecretValues(
                            {
                                "endpoint": "https://test.invalid",
                                "api_key": "private-key",
                                "task": {},
                            },
                        ),
                        Lease(CLUSTER),
                        path,
                    ),
                )
            expect_equal(execute.await_count, 2)
            cleanup = json.loads(execute.call_args.kwargs["data"])
            expect_equal(cleanup["action"], "cleanup")
            expect_equal(cleanup["cluster"], CLUSTER)
            guard.wait.assert_awaited_once()
            expect_equal((path / "runpod-cleanup.stop").exists(), expected=True)

    def test_failed_skypilot_cleanup_still_waits_for_runpod_proof(self) -> None:
        """SkyPilot failure does not prevent direct termination verification."""
        guard = MagicMock()
        guard.stdin.drain = AsyncMock()
        guard.wait = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "runpod-cleanup.json").write_text(
                json.dumps(
                    {
                        "status": "cleaned",
                        "pods": ["owned"],
                        "violation": None,
                    },
                ),
            )
            with (
                patch.object(
                    gpu.asyncio,
                    "create_subprocess_exec",
                    new=AsyncMock(return_value=guard),
                ),
                patch.object(gpu, "_wait_armed", new=AsyncMock()),
                patch.object(
                    gpu,
                    "execute",
                    new=AsyncMock(
                        side_effect=["", RuntimeError("SkyPilot unavailable")],
                    ),
                ),
                pytest.raises(RuntimeError, match="SkyPilot unavailable"),
            ):
                asyncio.run(
                    gpu.run_gpu_job(
                        SecretValues(
                            {
                                "endpoint": "https://test.invalid",
                                "api_key": "private-key",
                                "task": {},
                            },
                        ),
                        Lease(CLUSTER),
                        path,
                    ),
                )
            guard.wait.assert_awaited_once()

    def test_subprocess_and_fixture_representations_hide_credentials(self) -> None:
        """Diagnostic redaction does not change the actual stdin values."""
        values = SecretValues({"api_key": "private-key"})
        payload = secret_json(values)
        expect_equal("private-key" in repr(values), expected=False)
        expect_equal("private-key" in repr(payload), expected=False)
        expect_equal(json.loads(payload), {"api_key": "private-key"})
        expect_equal(isinstance(payload, SecretText), expected=True)
