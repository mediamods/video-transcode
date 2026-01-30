"""Convex HTTP client for reporting video processing status.

Mirrors the JavaScript ``reportToConvex`` function from handler.mjs.
Uses ``urllib`` from the standard library to avoid extra dependencies.

Set the ``CONVEX_URL`` environment variable to your Convex deployment
URL (e.g. ``https://your-deployment.convex.cloud``).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

CONVEX_URL = os.environ.get("CONVEX_URL", "")


def report_to_convex(
    *,
    version_id: str,
    status: str,
    published_s3_key: str | None = None,
    publish_error: str | None = None,
) -> None:
    """Report processing completion status to Convex.

    This is best-effort: failures are logged but never raised, matching
    the behaviour of the JavaScript implementation.

    Calls the ``documents:completePublish`` mutation with::

        {
            "versionId": "...",
            "status": "published" | "failed",
            "publishedS3Key": "...",   // on success
            "publishError": "...",     // on failure
        }
    """
    if not CONVEX_URL:
        logger.warning("CONVEX_URL not set, skipping Convex notification")
        return

    payload: dict = {
        "versionId": version_id,
        "status": status,
    }
    if published_s3_key:
        payload["publishedS3Key"] = published_s3_key
    if publish_error:
        payload["publishError"] = publish_error

    try:
        url = f"{CONVEX_URL.rstrip('/')}/api/mutation"
        body = json.dumps({
            "path": "documents:completePublish",
            "args": payload,
        }).encode()

        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

        logger.info("Reported %s to Convex for version %s", status, version_id)

    except Exception:
        logger.exception("Failed to report to Convex (best-effort, continuing)")
