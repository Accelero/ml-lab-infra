# Copyright (c) 2026 David Schmid
"""Validate Barman base backups and WAL coverage in S3."""

import gzip
import io
import re
import time
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

    from botocore.client import BaseClient

_CLUSTER = "postgres"
_METADATA_LIMIT = 64 * 1024
_LSN_SHIFT = 32
_LOG_SIZE = 1 << _LSN_SHIFT
_MIN_SEGMENT_SIZE = 1024 * 1024
_MAX_SEGMENT_SIZE = 1024 * _MIN_SEGMENT_SIZE
_WAL_PATTERN = re.compile(r"[0-9A-F]{24}")
_BACKUP_PATTERN = re.compile(r"[0-9]{8}T[0-9]{6}")
_HISTORY_FIELDS = 2


def _segment_size(size: int) -> int:
    if not _MIN_SEGMENT_SIZE <= size <= _MAX_SEGMENT_SIZE or size & (size - 1):
        msg = "Invalid PostgreSQL WAL segment size."
        raise ValueError(msg)
    return size


def _lsn(value: str) -> int:
    if not re.fullmatch(r"[0-9A-F]{1,8}/[0-9A-F]{1,8}", value):
        msg = "Invalid PostgreSQL WAL position."
        raise ValueError(msg)
    high, low = value.split("/")
    return (int(high, 16) << _LSN_SHIFT) | int(low, 16)


@dataclass(frozen=True)
class WalSegment:
    """A segment number within a PostgreSQL timeline."""

    timeline: int
    number: int

    @classmethod
    def parse(cls, filename: str, size: int) -> WalSegment:
        """Decode a WAL filename using the cluster's segment size."""
        _segment_size(size)
        if not _WAL_PATTERN.fullmatch(filename):
            msg = "Invalid WAL segment filename."
            raise ValueError(msg)
        timeline = int(filename[:8], 16)
        log = int(filename[8:16], 16)
        segment = int(filename[16:], 16)
        per_log = _LOG_SIZE // size
        if timeline == 0 or segment >= per_log:
            msg = "WAL filename is incompatible with the segment size."
            raise ValueError(msg)
        return cls(timeline, log * per_log + segment)

    def filename(self, size: int) -> str:
        """Encode the segment using PostgreSQL's log/segment numbering."""
        log, segment = divmod(self.number, _LOG_SIZE // _segment_size(size))
        return f"{self.timeline:08X}{log:08X}{segment:08X}"


@dataclass(frozen=True)
class ArchiveTarget:
    """The closed WAL segment containing a teardown restore point."""

    wal: str
    system_identifier: str
    segment_size: int

    def __post_init__(self) -> None:
        """Reject malformed targets before accessing archive objects."""
        WalSegment.parse(self.wal, self.segment_size)
        if not self.system_identifier.isdecimal():
            msg = "Invalid PostgreSQL system identifier."
            raise ValueError(msg)


@dataclass(frozen=True)
class BaseBackup:
    """Completed Barman backup metadata needed for recovery coverage."""

    identifier: str
    begin_wal: str
    end_wal: str
    begin_lsn: int
    end_lsn: int
    system_identifier: str
    segment_size: int

    @classmethod
    def from_info(cls, identifier: str, info: dict[str, str]) -> BaseBackup:
        """Read scalar fields from Barman's backup.info format."""
        if not _BACKUP_PATTERN.fullmatch(identifier):
            msg = "Invalid Barman backup identifier."
            raise ValueError(msg)
        backup = cls(
            identifier=identifier,
            begin_wal=info["begin_wal"],
            end_wal=info["end_wal"],
            begin_lsn=_lsn(info["begin_xlog"]),
            end_lsn=_lsn(info["end_xlog"]),
            system_identifier=info["systemid"],
            segment_size=_segment_size(int(info["xlog_segment_size"])),
        )
        begin = WalSegment.parse(backup.begin_wal, backup.segment_size)
        end = WalSegment.parse(backup.end_wal, backup.segment_size)
        if (
            not backup.system_identifier.isdecimal()
            or begin.timeline != end.timeline
            or int(info["timeline"]) != begin.timeline
            or begin.number > end.number
            or backup.begin_lsn > backup.end_lsn
            or not begin.number * backup.segment_size
            <= backup.begin_lsn
            < (begin.number + 1) * backup.segment_size
            or not end.number * backup.segment_size
            <= backup.end_lsn
            <= (end.number + 1) * backup.segment_size
        ):
            msg = "Inconsistent Barman backup metadata."
            raise ValueError(msg)
        if info.get("parent_backup_id", "None") != "None":
            msg = "Incremental base backups are not supported by this stack."
            raise ValueError(msg)
        if info.get("tablespaces", "None") not in {"None", "[]"}:
            msg = "Custom tablespaces require additional backup validation."
            raise ValueError(msg)
        return backup


def _history(text: str, current: int) -> list[tuple[int, int]]:
    ancestors = []
    previous_timeline = 0
    previous_lsn = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=2)
        if len(fields) < _HISTORY_FIELDS:
            msg = "Malformed PostgreSQL timeline history."
            raise ValueError(msg)
        timeline, switch = int(fields[0]), _lsn(fields[1])
        if not previous_timeline < timeline < current or switch < previous_lsn:
            msg = "Inconsistent PostgreSQL timeline history."
            raise ValueError(msg)
        ancestors.append((timeline, switch))
        previous_timeline, previous_lsn = timeline, switch
    if not ancestors:
        msg = "Empty PostgreSQL timeline history."
        raise ValueError(msg)
    return ancestors


def required_wals(
    backup: BaseBackup,
    target: ArchiveTarget,
    history: str = "",
) -> Iterator[str]:
    """Enumerate required segments along the target timeline's ancestry."""
    if (
        backup.system_identifier != target.system_identifier
        or backup.segment_size != target.segment_size
    ):
        msg = "Backup belongs to a different PostgreSQL cluster."
        raise ValueError(msg)
    begin = WalSegment.parse(backup.begin_wal, target.segment_size)
    end = WalSegment.parse(target.wal, target.segment_size)
    ancestors = _history(history, end.timeline) if history else []
    timelines = [timeline for timeline, _ in ancestors] + [end.timeline]
    if begin.timeline not in timelines:
        msg = "Backup is not on the teardown target's timeline ancestry."
        raise ValueError(msg)
    index = timelines.index(begin.timeline)
    if ancestors and index < len(ancestors):
        if backup.end_lsn > ancestors[index][1]:
            msg = "Backup extends beyond its timeline's recovery branch."
            raise ValueError(msg)
    elif WalSegment.parse(backup.end_wal, target.segment_size).number > end.number:
        msg = "Backup ends after the teardown target."
        raise ValueError(msg)
    first = begin.number
    for timeline, switch in ancestors[index:]:
        last = (switch - 1) // target.segment_size
        for number in range(first, last + 1):
            yield WalSegment(timeline, number).filename(target.segment_size)
        first = switch // target.segment_size
    if first > end.number:
        msg = "Teardown target precedes the required WAL range."
        raise ValueError(msg)
    for number in range(first, end.number + 1):
        yield WalSegment(end.timeline, number).filename(target.segment_size)


class PostgresArchive:
    """Read completed backups and verify required S3 WAL objects."""

    def __init__(self, client: BaseClient, bucket: str) -> None:
        """Use an existing S3 client with bounded request retries."""
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_props(cls, props: dict) -> PostgresArchive:
        """Construct an S3 client from resolved Pulumi resource inputs."""
        client = boto3.client(
            "s3",
            endpoint_url=props["s3_endpoint"],
            aws_access_key_id=props["s3_access_key"],
            aws_secret_access_key=props["s3_secret_key"],
            config=Config(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ),
        )
        return cls(client, props["s3_bucket"])

    def objects(self, prefix: str, deadline: float) -> dict[str, int]:
        """List nonempty archive objects, following every S3 page."""
        objects = {}
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket,
            Prefix=prefix,
        )
        for page in pages:
            self._check_deadline(deadline)
            for obj in page.get("Contents", []):
                if obj["Size"] > 0:
                    objects[obj["Key"]] = obj["Size"]
        return objects

    def metadata(self, key: str, deadline: float) -> str:
        """Read bounded, optionally gzip-compressed archive metadata."""
        self._check_deadline(deadline)
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        with closing(response["Body"]) as body:
            data = body.read(_METADATA_LIMIT + 1)
        if len(data) > _METADATA_LIMIT:
            msg = "Archive metadata exceeds the size limit."
            raise ValueError(msg)
        if key.endswith(".gz"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
                data = source.read(_METADATA_LIMIT + 1)
            if len(data) > _METADATA_LIMIT:
                msg = "Decompressed archive metadata exceeds the size limit."
                raise ValueError(msg)
        return data.decode("utf-8")

    def backups(self, deadline: float) -> list[BaseBackup]:
        """Return completed full backups with a base-data archive present."""
        prefix = f"{_CLUSTER}/base/"
        objects = self.objects(prefix, deadline)
        backups = []
        for key in sorted(objects):
            relative = key.removeprefix(prefix)
            identifier, separator, filename = relative.partition("/")
            if not separator or filename != "backup.info":
                continue
            info = dict(
                line.split("=", 1)
                for line in self.metadata(key, deadline).splitlines()
                if "=" in line
            )
            if info.get("status") != "DONE":
                continue
            backup = BaseBackup.from_info(identifier, info)
            data_prefix = f"{prefix}{identifier}/data"
            if not any(
                re.fullmatch(
                    r"(?:_\d+)?\.tar(?:\.(?:gz|bz2|xz|lz4|snappy|zst|zstd))?",
                    obj.removeprefix(data_prefix),
                )
                for obj in objects
                if obj.startswith(data_prefix)
            ):
                msg = f"Completed backup {identifier} has no base-data archive."
                raise ValueError(msg)
            backups.append(backup)
        return sorted(backups, key=lambda backup: backup.identifier, reverse=True)

    def missing_wals(
        self,
        backup: BaseBackup,
        target: ArchiveTarget,
        deadline: float,
    ) -> list[str]:
        """Find gaps in the required timeline chain, including history files."""
        objects = self.objects(f"{_CLUSTER}/wals/", deadline)
        files = {}
        for key in objects:
            name = key.rsplit("/", 1)[-1].removesuffix(".gz")
            files[name] = key
        timeline = WalSegment.parse(target.wal, target.segment_size).timeline
        history_name = f"{timeline:08X}.history"
        history = ""
        if timeline > 1:
            if history_name not in files:
                return [history_name]
            history = self.metadata(files[history_name], deadline)
        missing = []
        for filename in required_wals(backup, target, history):
            self._check_deadline(deadline)
            if filename not in files:
                missing.append(filename)
        # Recovery can request the intermediate timeline history files as well.
        begin = WalSegment.parse(backup.begin_wal, target.segment_size)
        for ancestor, _ in _history(history, timeline) if history else []:
            name = f"{ancestor:08X}.history"
            if ancestor > 1 and ancestor >= begin.timeline and name not in files:
                missing.append(name)
        return missing

    @staticmethod
    def _check_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            msg = "S3 archive verification timed out."
            raise TimeoutError(msg)
