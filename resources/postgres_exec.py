"""Run checked psql commands on the current CloudNativePG primary."""

import base64
import re
from contextlib import closing

import kubernetes.client
from kubernetes.stream import stream

_NAMESPACE = "infra"
_CLUSTER = "postgres"
_NOT_FOUND = 404
_REQUEST_TIMEOUT = (5, 30)
_EXEC_TIMEOUT = 60
_VARIABLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def _stdin(sql: str, variables: dict[str, str]) -> str:
    """Load literal values through stdin, keeping them out of command arguments."""
    lines = []
    for name, value in variables.items():
        if not _VARIABLE_NAME.fullmatch(name):
            msg = "Invalid psql variable name."
            raise ValueError(msg)
        encoded = base64.b64encode(value.encode()).decode("ascii")
        lines.append(
            f"SELECT convert_from(decode('{encoded}', 'base64'), 'UTF8') "
            f'AS "{name}"\n\\gset',
        )
    return "\n".join([*lines, sql.strip(), "\\q", ""])


class PostgresExec:
    """Keep SQL output separate from errors and require successful completion."""

    def __init__(self, client: kubernetes.client.ApiClient) -> None:
        """Use the caller's in-memory Kubernetes connection."""
        self.core = kubernetes.client.CoreV1Api(client)
        self.custom = kubernetes.client.CustomObjectsApi(client)

    def cluster(self) -> dict | None:
        """Treat only a Kubernetes 404 as an absent Cluster."""
        try:
            return self.custom.get_namespaced_custom_object(
                "postgresql.cnpg.io",
                "v1",
                _NAMESPACE,
                "clusters",
                _CLUSTER,
                _request_timeout=_REQUEST_TIMEOUT,
            )
        except kubernetes.client.exceptions.ApiException as exc:
            if exc.status != _NOT_FOUND:
                raise
        return None

    def query(
        self,
        sql: str,
        variables: dict[str, str] | None = None,
        *,
        database: str = "postgres",
        timeout: float = _EXEC_TIMEOUT,
    ) -> str:
        """Execute stdin SQL without logging SQL, values, or sensitive error text."""
        if timeout <= 0:
            msg = "Postgres SQL command timed out."
            raise TimeoutError(msg)
        cluster = self.cluster() or {}
        primary = cluster.get("status", {}).get("currentPrimary")
        if not isinstance(primary, str) or not primary:
            msg = "Postgres has no known primary for SQL execution."
            raise RuntimeError(msg)
        if cluster.get("metadata", {}).get("deletionTimestamp"):
            msg = "Postgres Cluster is being deleted; refusing SQL execution."
            raise RuntimeError(msg)
        input_sql = _stdin(sql, variables or {})
        command = [
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
        ]
        response = stream(
            self.core.connect_get_namespaced_pod_exec,
            primary,
            _NAMESPACE,
            command=command,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=False,
            _preload_content=False,
            _request_timeout=_REQUEST_TIMEOUT,
        )
        with closing(response) as process:
            process.write_stdin(input_sql)
            process.run_forever(timeout=timeout)
            if process.is_open():
                msg = "Postgres SQL command timed out."
                raise TimeoutError(msg)
            returncode = process.returncode
            if returncode != 0:
                msg = f"Postgres SQL command failed with exit status {returncode}."
                raise RuntimeError(msg)
            return process.read_stdout().strip()
