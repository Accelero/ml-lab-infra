# Copyright (c) 2026 David Schmid
"""Regression coverage for Barman metadata and complete WAL ancestry."""

import gzip
import io
import unittest
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from resources.postgres_archive import (
    ArchiveTarget,
    BaseBackup,
    PostgresArchive,
    WalSegment,
    required_wals,
)

from .helpers import expect_equal

_SIZE = 16 * 1024 * 1024
_DEADLINE = float("inf")
_IDENTIFIER = "20261009T120000"
_SYSTEM = "1234567890"


def backup_info() -> dict[str, str]:
    """Return a full backup spanning WAL segments one and two."""
    return {
        "status": "DONE",
        "begin_wal": WalSegment(1, 1).filename(_SIZE),
        "end_wal": WalSegment(1, 2).filename(_SIZE),
        "begin_xlog": "0/1000028",
        "end_xlog": "0/2000028",
        "systemid": _SYSTEM,
        "timeline": "1",
        "xlog_segment_size": str(_SIZE),
    }


def base_backup() -> BaseBackup:
    """Build a completed base backup for range tests."""
    return BaseBackup.from_info(_IDENTIFIER, backup_info())


def target(number: int, timeline: int = 1) -> ArchiveTarget:
    """Build a cutoff with the test cluster's identity and segment size."""
    return ArchiveTarget(WalSegment(timeline, number).filename(_SIZE), _SYSTEM, _SIZE)


def archive_for(objects: dict[str, bytes]) -> PostgresArchive:
    """Provide a paginated S3 double with bounded readable object bodies."""
    client = MagicMock()

    def pages(**kwargs: str) -> list[dict]:
        return [
            {"Contents": [{"Key": key, "Size": len(value)}]}
            for key, value in objects.items()
            if key.startswith(kwargs["Prefix"])
        ]

    def get_object(**kwargs: str) -> dict:
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}

    client.get_paginator.return_value.paginate.side_effect = pages
    client.get_object.side_effect = get_object
    return PostgresArchive(client, "test-bucket")


class WalRangeTests(unittest.TestCase):
    """Check segment arithmetic and timeline transitions independently of S3."""

    def test_rollover_for_nondefault_segment_size(self) -> None:
        """Segment numbers wrap at the configured size, rather than always 256."""
        size = 64 * 1024 * 1024
        segment = WalSegment(1, 64)
        filename = "000000010000000100000000"
        expect_equal(segment.filename(size), filename)
        expect_equal(WalSegment.parse(filename, size), segment)
        with pytest.raises(ValueError, match="incompatible"):
            WalSegment.parse("000000010000000000000040", size)

    def test_invalid_segment_size(self) -> None:
        """Reject sizes that PostgreSQL cannot use."""
        for size in (0, 3 * 1024 * 1024, 2 * 1024 * 1024 * 1024):
            with (
                self.subTest(size=size),
                pytest.raises(ValueError, match="segment size"),
            ):
                WalSegment.parse(target(3).wal, size)

    def test_same_timeline_includes_backup_and_cutoff(self) -> None:
        """Recovery requires all segments, including both endpoints."""
        expected = [WalSegment(1, number).filename(_SIZE) for number in range(1, 5)]
        expect_equal(list(required_wals(base_backup(), target(4))), expected)

    def test_promotion_requires_both_versions_of_split_segment(self) -> None:
        """A switch within a segment requires the parent and child copies."""
        expected = [WalSegment(1, number).filename(_SIZE) for number in range(1, 4)] + [
            WalSegment(2, number).filename(_SIZE) for number in range(3, 6)
        ]
        history = "1 0/3000028 promoted"
        result = list(required_wals(base_backup(), target(5, 2), history))
        expect_equal(result, expected)

    def test_promotion_at_segment_boundary(self) -> None:
        """Do not require a parent segment starting after the switch."""
        expected = [WalSegment(1, number).filename(_SIZE) for number in (1, 2)]
        expected.extend(WalSegment(2, number).filename(_SIZE) for number in (3, 4))
        history = "1 0/3000000 promoted"
        result = list(required_wals(base_backup(), target(4, 2), history))
        expect_equal(result, expected)

    def test_multiple_promotions(self) -> None:
        """Enumerate every intermediate timeline along the recovery branch."""
        history = "1 0/3000028 promoted\n2 0/5000028 promoted\n"
        segments = list(required_wals(base_backup(), target(6, 3), history))
        expected = [WalSegment(1, n).filename(_SIZE) for n in range(1, 4)]
        expected.extend(WalSegment(2, n).filename(_SIZE) for n in range(3, 6))
        expected.extend(WalSegment(3, n).filename(_SIZE) for n in (5, 6))
        expect_equal(segments, expected)

    def test_wrong_branch_or_cluster_fails(self) -> None:
        """Presence of unrelated WAL cannot establish recovery coverage."""
        cases = [
            (target(4, 3), "2 0/3000028 promoted"),
            (replace(target(4), system_identifier="987654321"), ""),
            (replace(target(4), segment_size=32 * 1024 * 1024), ""),
            (target(1), ""),
            (target(4, 2), "1 0/1000028 promoted"),
            (target(4, 3), "2 0/3000028 promoted\n1 0/4000028 bad"),
        ]
        for cutoff, history in cases:
            with (
                self.subTest(cutoff=cutoff, history=history),
                pytest.raises(ValueError, match=r"Backup|history|target"),
            ):
                list(required_wals(base_backup(), cutoff, history))

    def test_inconsistent_or_unsupported_backup(self) -> None:
        """Reject malformed metadata and backup formats requiring extra validation."""
        errors = r"Invalid|Inconsistent|Incremental|Custom"
        for overrides in (
            {"begin_xlog": "invalid"},
            {"end_xlog": "0/4000028"},
            {"timeline": "2"},
            {"parent_backup_id": _IDENTIFIER},
            {"tablespaces": "[(123, 'extra', '/data')]"},
        ):
            with (
                self.subTest(overrides=overrides),
                pytest.raises(ValueError, match=errors),
            ):
                BaseBackup.from_info(_IDENTIFIER, backup_info() | overrides)


class ArchiveInventoryTests(unittest.TestCase):
    """Check actual archive inventory handling with a paginated S3 double."""

    def test_backup_must_be_done_and_have_data(self) -> None:
        """An incomplete or empty prefix is never a recoverable base backup."""
        prefix = f"postgres/base/{_IDENTIFIER}/"
        info = backup_info() | {"status": "STARTED"}
        metadata = "\n".join(f"{key}={value}" for key, value in info.items()).encode()
        objects = {prefix + "backup.info": metadata, prefix + "data.tar": b"data"}
        expect_equal(archive_for(objects).backups(_DEADLINE), [])
        objects[prefix + "backup.info"] = metadata.replace(b"STARTED", b"DONE")
        expect_equal(archive_for(objects).backups(_DEADLINE), [base_backup()])
        objects[prefix + "data.tar"] = b""
        with pytest.raises(ValueError, match="no base-data"):
            archive_for(objects).backups(_DEADLINE)

    def test_missing_middle_is_not_hidden_by_newest_segment(self) -> None:
        """Newest segment presence alone must not permit teardown."""
        objects = {f"postgres/wals/hash/{target(n).wal}.gz": b"wal" for n in (1, 3, 4)}
        objects[f"postgres/wals/hash/{target(2).wal}.partial.gz"] = b"partial"
        missing = archive_for(objects).missing_wals(base_backup(), target(4), _DEADLINE)
        expect_equal(missing, [target(2).wal])

    def test_history_and_parent_segments_are_required(self) -> None:
        """Verify the old timeline and the compressed current history file."""
        objects = {f"postgres/wals/hash/{target(4, 2).wal}.gz": b"wal"}
        archive = archive_for(objects)
        expect_equal(
            archive.missing_wals(base_backup(), target(4, 2), _DEADLINE),
            ["00000002.history"],
        )
        objects["postgres/wals/00000002.history.gz"] = gzip.compress(
            b"1 0/3000028 promoted\n",
        )
        for number in range(1, 4):
            objects[f"postgres/wals/hash/{target(number).wal}.gz"] = b"wal"
        expect_equal(
            archive.missing_wals(base_backup(), target(4, 2), _DEADLINE),
            [target(3, 2).wal],
        )
        objects[f"postgres/wals/hash/{target(3, 2).wal}.gz"] = b"wal"
        expect_equal(
            archive.missing_wals(base_backup(), target(4, 2), _DEADLINE),
            [],
        )

    def test_metadata_decompression_is_bounded(self) -> None:
        """Reject oversized compressed or plain metadata."""
        data = b"x" * (64 * 1024 + 1)
        objects = {"oversized": data, "oversized.gz": gzip.compress(data)}
        for key in objects:
            with self.subTest(key=key), pytest.raises(ValueError, match="size limit"):
                archive_for(objects).metadata(key, _DEADLINE)

    def test_old_history_before_base_timeline_is_not_required(self) -> None:
        """Retention of an older ancestor history is unnecessary for a newer base."""
        backup = replace(
            base_backup(),
            begin_wal=target(5, 3).wal,
            end_wal=target(6, 3).wal,
            begin_lsn=5 * _SIZE + 40,
            end_lsn=6 * _SIZE + 40,
        )
        objects = {
            f"postgres/wals/hash/{target(n, 3).wal}.gz": b"wal" for n in (5, 6, 7)
        }
        objects["postgres/wals/00000003.history"] = (
            b"1 0/3000028 promoted\n2 0/5000028 promoted\n"
        )
        expect_equal(
            archive_for(objects).missing_wals(backup, target(7, 3), _DEADLINE),
            [],
        )

    def test_expired_deadline_fails(self) -> None:
        """Archive scans cannot silently continue beyond their deadline."""
        with pytest.raises(TimeoutError):
            archive_for({"postgres/wals/file": b"wal"}).objects("postgres/", 0)
