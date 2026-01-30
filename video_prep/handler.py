"""Lambda entry point and top-level orchestrator.

Replaces the old ``prep.py``.  Exposes two functions:

- ``lambda_handler(event, context)`` — AWS Lambda entry point.
- ``process_video(config)`` — core orchestrator, also callable directly
  for local testing or non-Lambda invocation.

All intermediate files are created inside a ``tempfile.TemporaryDirectory``
that auto-cleans on exit — critical for Lambda's limited ``/tmp`` space.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .chapterer import parse_chapters
from .ffmpeg_utils import check_dependencies, normalize_for_web, probe_video
from .ffwd_video_maker import make_ffwd_concat_video
from .models import VideoMetadata, VideoProbeInfo, VideoProcessingConfig
from .montager import make_montage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda handler.

    Expects *event* keys matching ``VideoProcessingConfig.from_event``:

    - ``video_file`` (str, required): path to the source video.
    - ``video_id`` (str, required): identifier used in the output .avd file.
    - ``export_dir`` (str, required): where to write final outputs.
    - ``chapter_file`` (str, optional): path to a chapter text file.
    - Plus optional tuning overrides (``thumbnail_width``, ``jpeg_quality``,
      ``blur_sigma``, ``blank_audio_duration``, ``padding_buffer_seconds``).

    Returns the ``.avd`` metadata dict.
    """
    config = VideoProcessingConfig.from_event(event)
    metadata = process_video(config)
    return metadata.to_dict()


# ---------------------------------------------------------------------------
# Core orchestrator
# ---------------------------------------------------------------------------

def process_video(config: VideoProcessingConfig) -> VideoMetadata:
    """Run the full video preparation pipeline.

    Steps:
    1. Verify ffmpeg/ffprobe are available.
    2. Probe the source video once.
    3. Normalize to H.264/AAC/MP4 if needed (for web playback).
    4. Create the fast-forward concatenated video.
    5. Create the thumbnail montage.
    6. Parse chapters (if a chapter file was provided).
    7. Write the ``.avd`` metadata JSON file.

    All temporary files live inside a ``TemporaryDirectory`` that is
    automatically removed when the function returns (or raises).
    """
    check_dependencies()

    if not config.video_file.exists():
        raise FileNotFoundError(f"Video file not found: {config.video_file}")

    config.export_dir.mkdir(parents=True, exist_ok=True)

    # Probe once, pass the result to every subsystem.
    probe_info = probe_video(config.video_file)
    logger.info(
        "Source: %s — %.1fs, %dx%d, video=%s, audio=%s, pix=%s",
        config.video_file.name,
        probe_info.duration,
        probe_info.width,
        probe_info.height,
        probe_info.video_codec,
        probe_info.audio_codec,
        probe_info.pixel_format,
    )

    metadata = VideoMetadata(video_id=config.video_id)

    with tempfile.TemporaryDirectory() as tmp:
        work_dir = Path(tmp)

        # ---- Normalize for web -------------------------------------------
        # Ensures the source is H.264/AAC/yuv420p in an MP4 container.
        # If already compatible this is a fast remux (stream copy);
        # otherwise a full transcode is performed.
        normalized_video = _normalize_source(
            config.video_file, work_dir, probe_info, config.web_crf,
        )
        # Re-probe after normalization so downstream steps get accurate
        # metadata (codec, duration, timescale may differ slightly).
        probe_info = probe_video(normalized_video)

        # ---- Fast-forward video ------------------------------------------
        video_data, video_path = make_ffwd_concat_video(
            video_src=normalized_video,
            work_dir=work_dir,
            output_filename="video.mp4",
            probe_info=probe_info,
            blank_audio_duration=config.blank_audio_duration,
            padding_buffer=config.padding_buffer_seconds,
        )
        metadata.video_data = video_data

        export_video_dir = config.export_dir / "video"
        export_video_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(video_path), str(export_video_dir / "video.mp4"))

        # ---- Montage -----------------------------------------------------
        montage_data, montage_path = make_montage(
            video_file=normalized_video,
            work_dir=work_dir,
            probe_info=probe_info,
            thumb_width=config.thumbnail_width,
            blur_sigma=config.blur_sigma,
            jpeg_quality=config.jpeg_quality,
        )
        metadata.montage_data = montage_data
        shutil.move(str(montage_path), str(config.export_dir / "montage.jpg"))

    # ---- Chapters (outside the temp dir — no temp files needed) ----------
    if config.chapter_file and config.chapter_file.exists():
        metadata.chapters = parse_chapters(config.chapter_file)

    # ---- Write .avd metadata file ----------------------------------------
    avd_path = config.export_dir / f"{config.video_id}.avd"
    avd_path.write_text(json.dumps(metadata.to_dict()))
    logger.info("Wrote metadata: %s", avd_path)

    return metadata


# ---------------------------------------------------------------------------
# Normalization helper
# ---------------------------------------------------------------------------

def _normalize_source(
    video_file: Path,
    work_dir: Path,
    probe_info: VideoProbeInfo,
    crf: int,
) -> Path:
    """Normalize the source video for web playback if needed.

    Returns the path to the normalized file (inside *work_dir*), or the
    original *video_file* if it's already web-compatible and just needs
    a clean remux.
    """
    normalized = work_dir / "normalized.mp4"
    normalize_for_web(video_file, normalized, probe_info, crf=crf)
    return normalized


# ---------------------------------------------------------------------------
# Chapters-only mode (replaces old justChapters function)
# ---------------------------------------------------------------------------

def process_chapters_only(
    chapter_file: Path,
    export_dir: Path,
    video_id: str,
) -> VideoMetadata:
    """Parse chapters and write a minimal .avd file (no video/montage).

    Useful when the video has already been processed but chapters need
    to be updated independently.
    """
    export_dir.mkdir(parents=True, exist_ok=True)

    metadata = VideoMetadata(video_id=video_id)
    if chapter_file.exists():
        metadata.chapters = parse_chapters(chapter_file)

    avd_path = export_dir / f"{video_id}.avd"
    avd_path.write_text(json.dumps(metadata.to_dict()))
    logger.info("Wrote chapters-only metadata: %s", avd_path)

    return metadata
