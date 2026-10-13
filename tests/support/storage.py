"""Claim empty test buckets and remove all owned objects, versions, and uploads."""

import time
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from support.stack import SecretValues

if TYPE_CHECKING:
    from botocore.client import BaseClient

    from support.stack import DevStack

OWNER_KEY = "__pytest_owner__"
PAGE_SIZE = 1000
CLEANUP_TIMEOUT = 600
TOKEN_LIMIT = 128


@dataclass
class Bucket:
    """Operate on a dedicated bucket with bounded, redacted S3 calls."""

    client: BaseClient
    name: str
    endpoint: str

    @classmethod
    def from_stack(cls, stack: DevStack, namespace: str) -> Bucket:
        """Load credentials in memory and disable unbounded SDK retries."""
        endpoint = stack.config(f"{namespace}:s3Endpoint")
        name = stack.config(f"{namespace}:s3BucketName")
        credentials = SecretValues(
            {
                "aws_access_key_id": stack.config(f"{namespace}:s3AccessKey"),
                "aws_secret_access_key": stack.config(f"{namespace}:s3SecretKey"),
            },
        )
        if not all(value.strip() for value in [endpoint, name, *credentials.values()]):
            msg = "Test storage requires explicit nonempty configuration."
            raise RuntimeError(msg)
        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                **credentials,
                config=Config(
                    connect_timeout=5,
                    read_timeout=15,
                    retries={"total_max_attempts": 1},
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        except BotoCoreError, ValueError:
            msg = "Test storage client configuration failed."
            raise RuntimeError(msg) from None
        return cls(client, name, endpoint)

    def request(self, operation: str, **parameters: object) -> dict:
        """Do not expose provider errors containing configuration or object bodies."""
        try:
            return getattr(self.client, operation)(Bucket=self.name, **parameters)
        except BotoCoreError, ClientError:
            msg = f"Test storage operation {operation} failed."
            raise RuntimeError(msg) from None

    def require_empty(self) -> None:
        """Include old versions, delete markers, and incomplete uploads."""
        versions = self.request("list_object_versions", MaxKeys=1)
        current = self.request("list_objects_v2", MaxKeys=1)
        uploads = self.request("list_multipart_uploads", MaxUploads=1)
        if (
            versions.get("Versions")
            or versions.get("DeleteMarkers")
            or versions.get("IsTruncated")
            or current.get("Contents")
            or current.get("IsTruncated")
            or uploads.get("Uploads")
            or uploads.get("IsTruncated")
        ):
            msg = "Managed tests require empty dedicated storage buckets."
            raise RuntimeError(msg)

    def claim(self, token: str) -> None:
        """Use conditional creation to prevent two sessions claiming a bucket."""
        self.request(
            "put_object",
            Key=OWNER_KEY,
            Body=token.encode(),
            IfNoneMatch="*",
        )
        self.require_owner(token)

    def require_owner(self, token: str) -> None:
        """Refuse deletion if the session's ownership marker has changed."""
        response = self.request("get_object", Key=OWNER_KEY)
        try:
            with closing(response["Body"]) as body:
                observed = body.read(TOKEN_LIMIT)
        except OSError, BotoCoreError:
            msg = "Test bucket ownership could not be read."
            raise RuntimeError(msg) from None
        if observed != token.encode():
            msg = "Test bucket ownership changed; refusing storage deletion."
            raise RuntimeError(msg)

    def _delete(self, objects: list[dict[str, str]]) -> None:
        response = self.request(
            "delete_objects",
            Delete={"Objects": objects, "Quiet": True},
        )
        if response.get("Errors"):
            msg = "Test storage did not delete every requested object."
            raise RuntimeError(msg)

    def _abort_uploads(self, uploads: list[dict], prefix: str, deadline: float) -> None:
        for upload in uploads:
            if time.monotonic() >= deadline:
                msg = "Test upload cleanup exceeded its deadline."
                raise TimeoutError(msg)
            if not upload["Key"].startswith(prefix):
                msg = "Test upload listing escaped its owned prefix."
                raise RuntimeError(msg)
            self.request(
                "abort_multipart_upload",
                Key=upload["Key"],
                UploadId=upload["UploadId"],
            )

    def _clear(self, prefix: str, token: str | None) -> None:
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        while time.monotonic() < deadline:
            if token is not None:
                self.require_owner(token)
            versions = self.request(
                "list_object_versions",
                Prefix=prefix,
                MaxKeys=PAGE_SIZE,
            )
            objects = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for item in versions.get("Versions", [])
                + versions.get("DeleteMarkers", [])
                if token is None or item["Key"] != OWNER_KEY
            ]
            uploads = self.request(
                "list_multipart_uploads",
                Prefix=prefix,
                MaxUploads=PAGE_SIZE,
            )
            self._abort_uploads(uploads.get("Uploads", []), prefix, deadline)
            if objects:
                if time.monotonic() >= deadline:
                    msg = "Test object cleanup exceeded its deadline."
                    raise TimeoutError(msg)
                if any(not item["Key"].startswith(prefix) for item in objects):
                    msg = "Test object listing escaped its owned prefix."
                    raise RuntimeError(msg)
                self._delete(objects)
            if objects or uploads.get("Uploads"):
                continue
            current = self.request("list_objects_v2", Prefix=prefix, MaxKeys=PAGE_SIZE)
            if (
                versions.get("IsTruncated")
                or uploads.get("IsTruncated")
                or current.get("IsTruncated")
                or any(
                    token is None or item["Key"] != OWNER_KEY
                    for item in current.get("Contents", [])
                )
            ):
                msg = "Test storage listings did not prove complete deletion."
                raise RuntimeError(msg)
            return
        msg = "Test storage cleanup exceeded its deadline."
        raise TimeoutError(msg)

    def clear_prefix(self, prefix: str) -> None:
        """Limit reusable-stack cleanup to one test-owned experiment directory."""
        if (
            not prefix
            or not prefix.endswith("/")
            or any(part in {"", ".", ".."} for part in prefix[:-1].split("/"))
        ):
            msg = "Artifact cleanup requires a nonempty experiment prefix."
            raise ValueError(msg)
        self._clear(prefix, None)

    def release_claim(self, token: str) -> None:
        """Remove a setup-only claim, including an uncertain successful PUT."""
        versions = self.request(
            "list_object_versions",
            Prefix=OWNER_KEY,
            MaxKeys=PAGE_SIZE,
        )
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in versions.get("Versions", []) + versions.get("DeleteMarkers", [])
            if item["Key"] == OWNER_KEY
        ]
        if not objects:
            return
        self.require_owner(token)
        self._delete(objects)

    def clear_owned(self, token: str) -> None:
        """Keep the ownership claim until all test data has been deleted."""
        self._clear("", token)
        self.release_claim(token)
        self.require_empty()
