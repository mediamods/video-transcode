"""AWS Lambda handler for video processing (SQS trigger).

Receives SQS events containing video processing requests, downloads
source videos from S3, processes them through the video_prep pipeline,
uploads results to S3, and reports completion status to Convex.

SQS message body format::

    {
        "docId": "abc123",
        "versionId": "v1",
        "s3Key": "uploads/abc123/video.mp4",
        "chapterFile": "uploads/abc123/chapters.txt"  // optional
    }

Replaces the JavaScript handler.mjs stub with a Python implementation
backed by the ``video_prep`` processing pipeline.

SAM entry point: ``handler.handler``
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from s3_utils import download_from_s3, upload_directory_to_s3
from convex_client import report_to_convex
from video_prep import process_video, VideoProcessingConfig

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE_BUCKET = os.environ.get("SOURCE_BUCKET", "adapt-uploads")
DEST_BUCKET = os.environ.get("DEST_BUCKET", "adapt-cdn")


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for SQS batch events.

    Processes each SQS record independently.  Failed records are returned
    in ``batchItemFailures`` so SQS can retry them without reprocessing
    the entire batch.
    """
    logger.info("Received event: %s", json.dumps(event, indent=2))

    batch_item_failures: list[dict[str, str]] = []

    for record in event["Records"]:
        message_id = record["messageId"]
        message = None

        try:
            message = json.loads(record["body"])
            doc_id = message["docId"]
            version_id = message["versionId"]
            s3_key = message["s3Key"]

            if not all([doc_id, version_id, s3_key]):
                raise ValueError("Missing required fields: docId, versionId, s3Key")

            output_prefix = _process_video(
                doc_id=doc_id,
                version_id=version_id,
                s3_key=s3_key,
                chapter_s3_key=message.get("chapterFile"),
            )

            report_to_convex(
                version_id=version_id,
                status="published",
                published_s3_key=f"{output_prefix}/video/video.mp4",
            )

        except Exception as e:
            logger.error(
                "Error processing message %s: %s", message_id, e, exc_info=True,
            )

            if message and message.get("versionId"):
                report_to_convex(
                    version_id=message["versionId"],
                    status="failed",
                    publish_error=str(e)[:1000],
                )

            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ---------------------------------------------------------------------------
# Internal orchestration
# ---------------------------------------------------------------------------

def _process_video(
    *,
    doc_id: str,
    version_id: str,
    s3_key: str,
    chapter_s3_key: str | None = None,
) -> str:
    """Download, process, and upload a single video.

    Returns the S3 output prefix (e.g. ``video/{docId}``).
    """
    work_dir = Path(tempfile.mkdtemp(prefix="video-", dir="/tmp"))

    try:
        input_path = work_dir / "input.mp4"
        export_dir = work_dir / "export"
        export_dir.mkdir()

        logger.info(
            "Processing video: docId=%s, versionId=%s, s3Key=%s",
            doc_id, version_id, s3_key,
        )

        # 1. Download source video from S3
        download_from_s3(SOURCE_BUCKET, s3_key, input_path)

        # 2. Optionally download chapter file
        chapter_path: Path | None = None
        if chapter_s3_key:
            chapter_path = work_dir / "chapters.txt"
            download_from_s3(SOURCE_BUCKET, chapter_s3_key, chapter_path)

        # 3. Run the video processing pipeline
        config = VideoProcessingConfig(
            video_file=input_path,
            video_id=doc_id,
            export_dir=export_dir,
            chapter_file=chapter_path,
        )
        metadata = process_video(config)
        logger.info("Pipeline complete — metadata: %s", metadata.to_dict())

        # 4. Upload all outputs to S3
        output_prefix = f"video/{doc_id}"
        upload_directory_to_s3(DEST_BUCKET, output_prefix, export_dir)

        logger.info("Successfully processed video: %s", doc_id)
        return output_prefix

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.info("Cleaned up working directory: %s", work_dir)
