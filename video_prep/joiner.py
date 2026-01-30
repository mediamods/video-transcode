"""Video joiner — concatenate multiple videos with silent gaps.

Each input video is padded to an integer-second boundary (same scheme as
the fast-forward pipeline), audio is extracted and padded to match, then
all segments are concatenated and muxed into a single output file.

Changes from the original ``joiner.py``:
- Generalized from exactly 2 videos to an arbitrary list.
- Imports utilities from ``ffmpeg_utils`` instead of from
  ``ffwd_video_maker`` (which was an inappropriate coupling).
- The ``sim=True`` dry-run mode is removed — if you need duration
  estimates without creating files, probe the videos directly.
- The final mux uses stream copy (fixes the same ``copy_video = ''``
  bug that existed in the old code at line 54).
- Temp-file lifecycle is managed by the caller via *work_dir*.
- No more script-level test invocation at the bottom of the file.
"""

from __future__ import annotations

import logging
import math
import shutil
from pathlib import Path

from .ffmpeg_utils import (
    concatenate_media,
    create_blank_audio,
    create_padded_audio,
    create_time_padded_video,
    mux_video_audio,
    probe_video,
)

logger = logging.getLogger(__name__)


def join_videos(
    video_files: list[Path],
    export_file: Path,
    work_dir: Path,
    *,
    blank_audio_duration: float = 5.0,
    padding_buffer: int = 1,
) -> list[int]:
    """Join *video_files* into a single file with silent gaps between them.

    Each video is padded to ``ceil(duration) + padding_buffer`` seconds,
    with silence filling the gap.  The result is written to *export_file*.

    Parameters
    ----------
    video_files:
        Ordered list of source videos to join.
    export_file:
        Where to write the final joined video.
    work_dir:
        Directory for intermediate files (caller manages cleanup).
    blank_audio_duration:
        Length of the silent audio clip used for padding (seconds).
    padding_buffer:
        Extra seconds added when ceiling each segment's duration.

    Returns
    -------
    The list of padded durations (one per input video), useful for
    building metadata or seeking.
    """
    if not video_files:
        raise ValueError("At least one video file is required")

    # Probe the first video for audio format (all inputs should match).
    first_info = probe_video(video_files[0])

    blank_audio = work_dir / "blank_audio.mp4"
    create_blank_audio(
        blank_audio,
        blank_audio_duration,
        first_info.audio_channel_layout,
        first_info.audio_sample_rate,
    )

    padded_durations: list[int] = []
    padded_videos: list[Path] = []
    padded_audios: list[Path] = []

    for idx, src in enumerate(video_files):
        info = probe_video(src)
        fit_duration = int(math.ceil(info.duration + padding_buffer))
        padded_durations.append(fit_duration)

        fit_video = work_dir / f"join_video_{idx}.mp4"
        create_time_padded_video(src, fit_video, fit_duration)
        padded_videos.append(fit_video)

        fit_audio = work_dir / f"join_audio_{idx}.mp4"
        create_padded_audio(src, fit_audio, blank_audio)
        padded_audios.append(fit_audio)

    # Concatenate all padded segments.
    concat_video = work_dir / "join_concat_video.mp4"
    concatenate_media(padded_videos, concat_video)

    concat_audio = work_dir / "join_concat_audio.mp4"
    concatenate_media(padded_audios, concat_audio, padded_durations)

    # Mux — stream copy, no re-encode (fixes old bug).
    joined = work_dir / "join_muxed.mp4"
    mux_video_audio(concat_video, concat_audio, joined)

    # Final time-pad to the total duration.
    total_duration = sum(padded_durations)
    final = work_dir / "join_final.mp4"
    create_time_padded_video(joined, final, total_duration, include_audio=True)

    # Move to the requested export location.
    export_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(final), str(export_file))

    logger.info("Joined %d videos → %s (durations: %s)", len(video_files), export_file, padded_durations)
    return padded_durations
