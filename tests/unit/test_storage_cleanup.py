"""Verify storage cleanup against the actual boto3 request model."""

import io
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.stub import Stubber
from support import storage
from support.storage import OWNER_KEY, Bucket

from .helpers import expect_equal

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def store() -> Generator[tuple[Bucket, Stubber]]:
    """Block network access by stubbing every expected SDK call."""
    credentials = {
        "aws_access_key_id": "unit-test",
        "aws_secret_access_key": "unit-test",
    }
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        region_name="us-east-1",
        **credentials,
    )
    with closing(client), Stubber(client) as stub:
        yield Bucket(client, "unit-bucket", "http://127.0.0.1:1"), stub
        stub.assert_no_pending_responses()


def owner(stub: Stubber, value: str = "invocation") -> None:
    """Serve one exact ownership marker read."""
    stub.add_response(
        "get_object",
        {"Body": io.BytesIO(value.encode())},
        {"Bucket": "unit-bucket", "Key": OWNER_KEY},
    )


def empty(stub: Stubber) -> None:
    """Require fresh version, current-object, and multipart inventories."""
    stub.add_response(
        "list_object_versions",
        {},
        {"Bucket": "unit-bucket", "MaxKeys": 1},
    )
    stub.add_response("list_objects_v2", {}, {"Bucket": "unit-bucket", "MaxKeys": 1})
    stub.add_response(
        "list_multipart_uploads",
        {},
        {"Bucket": "unit-bucket", "MaxUploads": 1},
    )


@pytest.mark.parametrize("retained", ["Versions", "DeleteMarkers", "Uploads"])
def test_empty_precondition_includes_hidden_storage(
    store: tuple[Bucket, Stubber],
    retained: str,
) -> None:
    """Old versions and unfinished uploads are persisted test state too."""
    bucket, stub = store
    version = {"Key": "old", "VersionId": "v1"}
    stub.add_response(
        "list_object_versions",
        {retained: [version]} if retained != "Uploads" else {},
        {"Bucket": bucket.name, "MaxKeys": 1},
    )
    stub.add_response("list_objects_v2", {}, {"Bucket": bucket.name, "MaxKeys": 1})
    stub.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": "old", "UploadId": "u1"}]}
        if retained == "Uploads"
        else {},
        {"Bucket": bucket.name, "MaxUploads": 1},
    )
    with pytest.raises(RuntimeError, match="empty dedicated"):
        bucket.require_empty()


def test_claim_is_conditional_and_verifies_ownership(
    store: tuple[Bucket, Stubber],
) -> None:
    """Two sessions cannot silently share a claimed empty bucket."""
    bucket, stub = store
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": bucket.name,
            "Key": OWNER_KEY,
            "Body": b"invocation",
            "IfNoneMatch": "*",
        },
    )
    owner(stub)
    bucket.claim("invocation")


def test_changed_claim_refuses_all_deletion(store: tuple[Bucket, Stubber]) -> None:
    """A marker owned by another session is not permission to clear storage."""
    bucket, stub = store
    owner(stub, "different-session")
    with pytest.raises(RuntimeError, match="ownership changed"):
        bucket.clear_owned("invocation")


def test_full_cleanup_removes_versions_markers_uploads_and_claim_last(
    store: tuple[Bucket, Stubber],
) -> None:
    """Deleting visible keys alone cannot pass the final empty-bucket proof."""
    bucket, stub = store
    marker = {"Key": OWNER_KEY, "VersionId": "owner-v"}
    data = {"Key": "postgres/base", "VersionId": "data-v"}
    deleted = {"Key": "postgres/old", "VersionId": "delete-v"}
    owner(stub)
    stub.add_response(
        "list_object_versions",
        {"Versions": [marker, data], "DeleteMarkers": [deleted]},
        {"Bucket": bucket.name, "Prefix": "", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": "postgres/incomplete", "UploadId": "upload"}]},
        {"Bucket": bucket.name, "Prefix": "", "MaxUploads": 1000},
    )
    stub.add_response(
        "abort_multipart_upload",
        {},
        {"Bucket": bucket.name, "Key": "postgres/incomplete", "UploadId": "upload"},
    )
    stub.add_response(
        "delete_objects",
        {},
        {"Bucket": bucket.name, "Delete": {"Objects": [data, deleted], "Quiet": True}},
    )
    owner(stub)
    stub.add_response(
        "list_object_versions",
        {"Versions": [marker]},
        {"Bucket": bucket.name, "Prefix": "", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_multipart_uploads",
        {},
        {"Bucket": bucket.name, "Prefix": "", "MaxUploads": 1000},
    )
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": OWNER_KEY}]},
        {"Bucket": bucket.name, "Prefix": "", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_object_versions",
        {"Versions": [marker]},
        {"Bucket": bucket.name, "Prefix": OWNER_KEY, "MaxKeys": 1000},
    )
    owner(stub)
    stub.add_response(
        "delete_objects",
        {},
        {"Bucket": bucket.name, "Delete": {"Objects": [marker], "Quiet": True}},
    )
    empty(stub)
    bucket.clear_owned("invocation")


def test_partial_delete_failure_is_not_cleanup_success(
    store: tuple[Bucket, Stubber],
) -> None:
    """HTTP 200 can include individual object deletion errors."""
    bucket, stub = store
    data = {"Key": "42/model", "VersionId": "v1"}
    stub.add_response(
        "list_object_versions",
        {"Versions": [data]},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_multipart_uploads",
        {},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxUploads": 1000},
    )
    stub.add_response(
        "delete_objects",
        {"Errors": [{"Key": data["Key"], "Code": "AccessDenied"}]},
        {"Bucket": bucket.name, "Delete": {"Objects": [data], "Quiet": True}},
    )
    with pytest.raises(RuntimeError, match="every requested object"):
        bucket.clear_prefix("42/")


def test_listing_cannot_delete_another_experiments_objects(
    store: tuple[Bucket, Stubber],
) -> None:
    """Prefix ownership is checked even if a provider returns unexpected keys."""
    bucket, stub = store
    stub.add_response(
        "list_object_versions",
        {"Versions": [{"Key": "43/model", "VersionId": "v1"}]},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_multipart_uploads",
        {},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxUploads": 1000},
    )
    with pytest.raises(RuntimeError, match="escaped"):
        bucket.clear_prefix("42/")


def test_cleanup_deadline_bounds_the_multipart_loop(
    store: tuple[Bucket, Stubber],
) -> None:
    """A large upload inventory cannot multiply the deadline by its size."""
    bucket, stub = store
    stub.add_response(
        "list_object_versions",
        {},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxKeys": 1000},
    )
    stub.add_response(
        "list_multipart_uploads",
        {"Uploads": [{"Key": "42/incomplete", "UploadId": "u1"}]},
        {"Bucket": bucket.name, "Prefix": "42/", "MaxUploads": 1000},
    )
    with (
        patch.object(storage.time, "monotonic", side_effect=[0, 0, 601]),
        pytest.raises(TimeoutError, match="upload cleanup"),
    ):
        bucket.clear_prefix("42/")


def test_sdk_errors_never_expose_provider_messages(
    store: tuple[Bucket, Stubber],
) -> None:
    """Credential-bearing provider messages do not reach pytest exceptions."""
    bucket, stub = store
    stub.add_client_error(
        "list_object_versions",
        service_error_code="AccessDenied",
        service_message="private-test-key",
        expected_params={"Bucket": bucket.name, "MaxKeys": 1},
    )
    with pytest.raises(RuntimeError) as error:
        bucket.require_empty()
    expect_equal("private-test-key" in str(error.value), expected=False)


def test_client_configuration_errors_are_redacted() -> None:
    """Constructor failures cannot expose credential keyword arguments."""
    stack = MagicMock()
    stack.config.side_effect = [
        "bad-endpoint",
        "dev-artifacts",
        "test-access",
        "private-test-key",
    ]
    with (
        patch.object(
            storage.boto3,
            "client",
            side_effect=ValueError("private-test-key"),
        ),
        pytest.raises(RuntimeError, match="configuration failed") as error,
    ):
        Bucket.from_stack(stack, "mlflow")
    expect_equal("private-test-key" in str(error.value), expected=False)


def test_empty_credentials_cannot_fall_back_to_operator_credentials() -> None:
    """An incomplete dev configuration cannot select ambient production keys."""
    stack = MagicMock()
    stack.config.side_effect = ["https://storage.invalid", "dev-artifacts", "", ""]
    with (
        patch.object(storage.boto3, "client") as client,
        pytest.raises(RuntimeError, match="nonempty configuration"),
    ):
        Bucket.from_stack(stack, "mlflow")
    client.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "/", "../", "42/../", "42//", "42"])
def test_artifact_cleanup_rejects_root_and_traversal_prefixes(prefix: str) -> None:
    """A malformed experiment directory cannot become bucket-wide deletion."""
    bucket = Bucket(MagicMock(), "unit-bucket", "http://127.0.0.1:1")
    with pytest.raises(ValueError, match="nonempty"):
        bucket.clear_prefix(prefix)
    bucket.client.list_object_versions.assert_not_called()
