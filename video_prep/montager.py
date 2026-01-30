"""Thumbnail montage generator.

Extracts one frame per second from a video and assembles them into a
single grid JPEG image.  This is a full rewrite of the original
montager.py which shelled out to ImageMagick (``convert`` + ``montage``)
for every single thumbnail and the final grid assembly.

Changes from the original:
- **Pillow replaces ImageMagick** — no ``convert`` / ``montage`` subprocesses.
  This eliminates N+2 subprocess calls (one per thumbnail + montage + final
  convert) and makes the module Lambda-friendly (no ImageMagick layer needed).
- Aspect ratio is computed from ``probe_info`` instead of extracting a
  sample frame just to measure it.
- All parameters are explicit function arguments (no module-level globals).
- Proper temp-file hygiene — extracted frames are cleaned up after use.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from PIL import Image, ImageFilter

from .ffmpeg_utils import extract_frame
from .models import MontageData, VideoProbeInfo

logger = logging.getLogger(__name__)

# JPEG spec maximum dimension (width or height).
MAX_JPEG_DIMENSION = 65500


def make_montage(
    video_file: Path,
    work_dir: Path,
    *,
    probe_info: VideoProbeInfo,
    thumb_width: int = 30,
    blur_sigma: float = 0.5,
    jpeg_quality: int = 85,
    max_jpeg_dimension: int = MAX_JPEG_DIMENSION,
) -> tuple[MontageData, Path]:
    """Create a montage of thumbnail frames from *video_file*.

    One frame is sampled per second of video.  Each frame is resized to
    *thumb_width* pixels wide (maintaining aspect ratio), given a slight
    Gaussian blur, and pasted into a grid image saved as a JPEG.

    Returns ``(MontageData, path_to_montage_jpg)``.
    """
    video_duration = probe_info.duration
    total_seconds = int(math.floor(video_duration))

    # --- Compute thumbnail height from the video's aspect ratio -----------
    aspect = probe_info.height / probe_info.width
    thumb_height = max(1, round(thumb_width * aspect))

    # --- Grid layout ------------------------------------------------------
    # How many thumbnails can we fit without exceeding the JPEG pixel limit?
    max_fit_w = max_jpeg_dimension // thumb_width
    max_fit_h = max_jpeg_dimension // thumb_height
    thumb_count = min(total_seconds, max_fit_w * max_fit_h)

    # Replicate the old layout algorithm: square-ish grid, biased by the
    # aspect ratio of each thumbnail cell.
    stacks_per_row = max(1, math.floor(thumb_width / thumb_height))
    sqr = int(math.ceil(math.sqrt(thumb_count / stacks_per_row)))
    cols = sqr
    rows = int(math.ceil(thumb_count / cols)) + 1

    logger.info(
        "Montage: %d thumbs (%dx%d), grid %dx%d",
        thumb_count, thumb_width, thumb_height, cols, rows,
    )

    # --- Extract frames & build thumbnails --------------------------------
    frame_dir = work_dir / "frames"
    frame_dir.mkdir(exist_ok=True)

    thumbnails: list[Image.Image] = []
    for i in range(thumb_count + 1):
        # Map index to a second offset spread evenly across the video.
        sec = int(round((i / thumb_count) * total_seconds)) if thumb_count else 0
        frame_path = frame_dir / f"{sec}.png"

        extract_frame(video_file, frame_path, sec)

        # The old code had a fallback: if the frame couldn't be extracted
        # (e.g. right at the end of the file), try one second earlier.
        if not frame_path.exists() and sec > 0:
            logger.warning("Frame at %ds missing — retrying at %ds", sec, sec - 1)
            extract_frame(video_file, frame_path, sec - 1)

        if not frame_path.exists():
            logger.warning("Skipping missing frame at %ds", sec)
            continue

        img = Image.open(frame_path)
        img = img.resize((thumb_width, thumb_height), Image.LANCZOS)
        if blur_sigma > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=blur_sigma))
        thumbnails.append(img)

        # Clean up the full-size frame immediately to save disk space
        # (important on Lambda with limited /tmp).
        frame_path.unlink(missing_ok=True)

    # --- Assemble grid ----------------------------------------------------
    grid_width = cols * thumb_width
    grid_height = rows * thumb_height
    canvas = Image.new("RGB", (grid_width, grid_height))

    for idx, thumb in enumerate(thumbnails):
        x = (idx % cols) * thumb_width
        y = (idx // cols) * thumb_height
        canvas.paste(thumb, (x, y))

    # --- Save as progressive JPEG -----------------------------------------
    montage_path = work_dir / "montage.jpg"
    canvas.save(
        montage_path,
        "JPEG",
        quality=jpeg_quality,
        progressive=True,
        optimize=True,
    )
    logger.info("Montage saved: %s (%dx%d)", montage_path, grid_width, grid_height)

    data = MontageData(
        thumb_width=thumb_width,
        thumb_height=thumb_height,
        columns=cols,
        thumb_count=thumb_count,
    )
    return data, montage_path
