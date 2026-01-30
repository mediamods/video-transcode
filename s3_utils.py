"""S3 download and upload utilities.

Mirrors the interface of the JavaScript ``s3-utils.mjs`` stub.  Uses
``boto3`` which is pre-installed in the Lambda Python runtime.

When running locally with SAM / LocalStack, set ``AWS_SAM_LOCAL=1`` or
``LOCALSTACK_HOSTNAME`` to route requests to the local S3 endpoint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import boto3

logger = logging.getLogger(__name__)

# Lazy-initialised S3 client (created once per Lambda cold start).
_s3_client = None

CONTENT_TYPE_MAP = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".json": "application/json",
    ".avd": "application/json",
}


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        kwargs: dict = {}
        if os.environ.get("AWS_SAM_LOCAL") or os.environ.get("LOCALSTACK_HOSTNAME"):
            kwargs["endpoint_url"] = "http://host.docker.internal:4566"
            kwargs["aws_access_key_id"] = "test"
            kwargs["aws_secret_access_key"] = "test"
        _s3_client = boto3.client("s3", **kwargs)
    return _s3_client


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_from_s3(bucket: str, key: str, local_path: Path) -> None:
    """Download an object from S3 to a local file."""
    logger.info("Downloading s3://%s/%s → %s", bucket, key, local_path)
    _get_s3_client().download_file(bucket, key, str(local_path))
    size = local_path.stat().st_size
    logger.info("Downloaded %d bytes", size)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload_to_s3(
    bucket: str,
    key: str,
    local_path: Path,
    content_type: str | None = None,
) -> None:
    """Upload a local file to S3."""
    logger.info("Uploading %s → s3://%s/%s", local_path, bucket, key)
    kwargs: dict = {}
    if content_type:
        kwargs["ExtraArgs"] = {"ContentType": content_type}
    _get_s3_client().upload_file(str(local_path), bucket, key, **kwargs)
    size = local_path.stat().st_size
    logger.info("Uploaded %d bytes", size)


def upload_buffer_to_s3(
    bucket: str,
    key: str,
    data: bytes,
    content_type: str,
) -> None:
    """Upload raw bytes to S3."""
    logger.info("Uploading buffer → s3://%s/%s", bucket, key)
    _get_s3_client().put_object(
        Bucket=bucket, Key=key, Body=data, ContentType=content_type,
    )
    logger.info("Uploaded %d bytes", len(data))


def upload_directory_to_s3(
    bucket: str,
    prefix: str,
    local_dir: Path,
) -> None:
    """Recursively upload every file in *local_dir* to S3.

    The directory structure is preserved under *prefix*.
    Content-Type is inferred from the file extension.
    """
    for file_path in sorted(local_dir.rglob("*")):
        if not file_path.is_file():
            continue
        relative = file_path.relative_to(local_dir)
        s3_key = f"{prefix}/{relative}"
        content_type = CONTENT_TYPE_MAP.get(file_path.suffix.lower())
        upload_to_s3(bucket, s3_key, file_path, content_type)
