"""Start a disposable PostgreSQL container for SQL integration tests."""

import shutil
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from support.process import command

from resources.postgres_exec import PostgresExec
from resources.postgres_exec import _stdin as sql_stdin

if TYPE_CHECKING:
    from collections.abc import Generator


class ContainerPostgres(PostgresExec):
    """Run the production SQL input protocol against a real local PostgreSQL."""

    def __init__(self, name: str) -> None:
        """Replace the Kubernetes transport with docker exec."""
        self.name = name

    def query(
        self,
        sql: str,
        variables: dict[str, str] | None = None,
        *,
        database: str = "postgres",
        timeout: float = 60,
    ) -> str:
        """Check psql's exit code while keeping values on stdin."""
        return command(
            [
                shutil.which("docker") or "docker",
                "exec",
                "-i",
                self.name,
                "psql",
                "-X",
                "-w",
                "-q",
                "-U",
                "postgres",
                "-d",
                database,
                "-t",
                "-A",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            data=sql_stdin(sql, variables or {}),
            timeout=timeout,
        )


@pytest.fixture(scope="module")
def postgres_sql() -> Generator[ContainerPostgres]:
    """Clean up even if container creation or readiness fails."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("--run-docker requires Docker.")
    name = f"ml-lab-sql-test-{uuid.uuid4().hex}"
    try:
        command(
            [
                docker,
                "run",
                "--detach",
                "--rm",
                "--network",
                "none",
                "--name",
                name,
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "postgres:17.9",
            ],
            timeout=120,
        )
        executor = ContainerPostgres(name)
        deadline = time.monotonic() + 60
        while True:
            if command([docker, "exec", name, "cat", "/proc/1/comm"]) != "postgres":
                if time.monotonic() >= deadline:
                    msg = "Postgres initialization did not finish."
                    raise TimeoutError(msg)
                time.sleep(0.5)
                continue
            try:
                executor.query("SELECT 1;", timeout=5)
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        executor.query("CREATE DATABASE skypilot_db;")
        yield executor
    finally:
        command([docker, "rm", "--force", name], timeout=30)
