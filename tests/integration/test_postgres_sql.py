# Copyright (c) 2026 David Schmid
"""Exercise policy SQL and connection gates on PostgreSQL 17."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
import yaml

from resources.postgres_lifecycle import (
    _CLOSE_DATABASES as CLOSE_DATABASES_SQL,
)
from resources.postgres_lifecycle import (
    _DATABASE_ACCESS as DATABASE_ACCESS_SQL,
)
from resources.postgres_lifecycle import (
    _OPEN_DATABASES as OPEN_DATABASES_SQL,
)
from resources.skypilot_config import _change_policy as change_policy
from resources.skypilot_config import _read_config as read_config
from resources.skypilot_config import _write_config as write_config

if TYPE_CHECKING:
    from integration.conftest import ContainerPostgres

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.fixture
def config_table(postgres_sql: ContainerPostgres) -> ContainerPostgres:
    """Reset the isolated configuration table for every test."""
    postgres_sql.query(
        "DROP TABLE IF EXISTS public.config_yaml;"
        "CREATE TABLE public.config_yaml (key text PRIMARY KEY, value text NOT NULL);",
        database="skypilot_db",
    )
    return postgres_sql


def test_policy_round_trip_preserves_literal_values(
    config_table: ContainerPostgres,
) -> None:
    """Quotes, backslashes, newlines, and nested YAML survive actual psql."""
    original = {"other": {"text": "quoted 'value'\\with\nnewlines", "enabled": True}}
    if not write_config(config_table, original, None):
        pytest.fail("Initial configuration was not inserted.")
    change_policy(config_table, "scripts.Policy")
    observed, _raw = read_config(config_table)
    if observed != original | {"admin_policy": "scripts.Policy"}:
        pytest.fail("Policy application changed unrelated configuration.")
    change_policy(config_table, None)
    observed, _raw = read_config(config_table)
    if observed != original:
        pytest.fail("Policy removal changed unrelated configuration.")


def test_stale_snapshot_does_not_overwrite_concurrent_settings(
    config_table: ContainerPostgres,
) -> None:
    """An actual UPDATE rejects the stale YAML snapshot."""
    write_config(config_table, {"other": "original"}, None)
    snapshot, raw = read_config(config_table)
    newer = {"other": "concurrent", "nested": {"keep": True}}
    write_config(config_table, newer, raw)
    if write_config(config_table, snapshot | {"admin_policy": "stale.Policy"}, raw):
        pytest.fail("A stale snapshot overwrote a concurrent update.")
    change_policy(config_table, "current.Policy")
    observed, _raw = read_config(config_table)
    if observed != newer | {"admin_policy": "current.Policy"}:
        pytest.fail("Policy retry did not preserve the concurrent settings.")


def test_absent_snapshot_does_not_overwrite_concurrent_insert(
    config_table: ContainerPostgres,
) -> None:
    """ON CONFLICT leaves a row created by another writer untouched."""
    write_config(config_table, {"other": "inserted"}, None)
    if write_config(config_table, {"admin_policy": "stale.Policy"}, None):
        pytest.fail("A stale insertion replaced an existing row.")
    observed, _raw = read_config(config_table)
    if observed != {"other": "inserted"}:
        pytest.fail("A conflicting insertion modified the row.")


def test_invalid_yaml_fails_without_replacing_the_row(
    config_table: ContainerPostgres,
) -> None:
    """A real persisted non-mapping cannot be silently rewritten."""
    config_table.query(
        "INSERT INTO config_yaml VALUES ('api_server_config', :'value');",
        {"value": yaml.safe_dump(["private-setting"])},
        database="skypilot_db",
    )
    with pytest.raises(TypeError, match="mapping"):
        change_policy(config_table, "scripts.Policy")
    value = config_table.query(
        "SELECT value FROM config_yaml;",
        database="skypilot_db",
    )
    if yaml.safe_load(value) != ["private-setting"]:
        pytest.fail("Invalid persisted configuration was overwritten.")


def test_database_connections_close_and_reopen(
    postgres_sql: ContainerPostgres,
) -> None:
    """Production gate SQL really changes whether clients can connect."""
    variables = {"databases": json.dumps(["skypilot_db"])}
    try:
        postgres_sql.query(CLOSE_DATABASES_SQL, variables)
        access = json.loads(postgres_sql.query(DATABASE_ACCESS_SQL, variables))
        if access != {"skypilot_db": False}:
            pytest.fail("The database connection gate did not close.")
        with pytest.raises(RuntimeError, match="exit status"):
            postgres_sql.query("SELECT 1;", database="skypilot_db")
    finally:
        postgres_sql.query(OPEN_DATABASES_SQL, variables)
    if postgres_sql.query("SELECT 1;", database="skypilot_db") != "1":
        pytest.fail("The database connection gate did not reopen.")


def test_sql_error_never_returns_partial_success(
    postgres_sql: ContainerPostgres,
) -> None:
    """ON_ERROR_STOP rejects a batch even after an earlier statement succeeded."""
    with pytest.raises(RuntimeError, match="exit status"):
        postgres_sql.query("SELECT 1; SELECT missing_test_column;")


def test_stale_update_rechecks_its_snapshot_after_a_real_row_lock(
    config_table: ContainerPostgres,
) -> None:
    """Postgres reevaluates the conditional write after a competing commit."""
    write_config(config_table, {"other": "original"}, None)
    snapshot, raw = read_config(config_table)
    newer = {"other": "competing-transaction"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        transaction = pool.submit(
            config_table.query,
            "SET application_name = 'policy-test-writer'; BEGIN; "
            "UPDATE config_yaml SET value = :'value' WHERE key = 'api_server_config'; "
            "SELECT pg_sleep(1); COMMIT;",
            {"value": yaml.safe_dump(newer)},
            database="skypilot_db",
            timeout=10,
        )
        deadline = time.monotonic() + 5
        while (
            config_table.query(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = 'policy-test-writer' "
                "AND wait_event = 'PgSleep';",
                database="skypilot_db",
            )
            != "1"
        ):
            if time.monotonic() >= deadline:
                pytest.fail("The competing transaction did not acquire its row lock.")
            time.sleep(0.02)
        if write_config(config_table, snapshot | {"admin_policy": "stale.Policy"}, raw):
            pytest.fail("A stale update replaced the competing transaction's settings.")
        transaction.result(timeout=10)
    observed, _raw = read_config(config_table)
    if observed != newer:
        pytest.fail("The competing settings were not preserved.")


def test_server_statement_timeout_is_reported(postgres_sql: ContainerPostgres) -> None:
    """A genuine PostgreSQL timeout cannot become a successful empty result."""
    with pytest.raises(RuntimeError, match="exit status"):
        postgres_sql.query(
            "SET statement_timeout = '50ms'; SELECT pg_sleep(2);",
            timeout=5,
        )
