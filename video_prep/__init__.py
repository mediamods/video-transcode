"""Video preparation pipeline — Python 3.10+ rewrite for AWS Lambda.

Quick start::

    from video_prep import process_video, VideoProcessingConfig
    from pathlib import Path

    config = VideoProcessingConfig(
        video_file=Path("input.mp4"),
        video_id="my_video",
        export_dir=Path("output/"),
    )
    metadata = process_video(config)

Or from Lambda::

    from video_prep import lambda_handler
    # event = {"video_file": "...", "video_id": "...", "export_dir": "..."}
"""

from .handler import lambda_handler, process_chapters_only, process_video
from .models import VideoProcessingConfig

__all__ = [
    "lambda_handler",
    "process_video",
    "process_chapters_only",
    "VideoProcessingConfig",
]
