# Copyright (c) 2026 David Schmid
"""Verify the exact base backup and subsequent WAL recover both sentinels."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from support.backups import BackupProbe, ClusterExec

pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.timeout(2400)]


def test_restore_contains_base_backup_and_wal_data(
    recovery_cluster: ClusterExec,
    sentinel_and_backup: BackupProbe,
) -> None:
    """Restore the pre-backup row and a row requiring post-backup WAL replay."""
    probe = sentinel_and_backup
    rows = recovery_cluster.query(
        "SELECT format('SELECT val FROM %I ORDER BY val', :'table_name') \\gexec",
        {"table_name": probe.table},
        database="mlflow_db",
    ).splitlines()
    if rows != sorted([probe.before, probe.after]):
        pytest.fail("Recovery did not preserve both base-backup and post-backup rows.")
    system_id = recovery_cluster.query(
        "SELECT (pg_control_system()).system_identifier::text;",
    )
    if system_id != probe.target.system_identifier:
        pytest.fail("Recovery adopted a different PostgreSQL system.")
