"""Fast-forward video pipeline.

Creates a series of sped-up copies of a source video at exponentially
increasing playback rates (1x, 2x, 4x, 8x, 16x, ...) and concatenates
them into a single output file with synchronised audio.

The loop continues until the sped-up segment is shorter than one second.
Each segment is padded to an integer-second boundary (``ceil(duration) +
padding_buffer``) so the client app can seek to exact second offsets.

The padding consists of the last frame held frozen (video side) and
silence (audio side).

Changes from the original ``ffwd_video_maker.py``:
- All ffmpeg calls go through ``ffmpeg_utils`` — no more inline command
  strings or ignored return codes.
- The final video+audio mux uses stream copy (``-c:v copy -c:a copy``)
  instead of re-encoding.  This was a significant bug in the old code
  (line 279: ``copy_video = ''``).
- The ``times`` variable is renamed to ``doubling_count`` for clarity.
- Returns a ``VideoSegmentData`` dataclass instead of a raw dict.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from .ffmpeg_utils import (
    concatenate_media,
    create_blank_audio,
    create_fast_forward_audio,
    create_fast_forward_video,
    create_padded_audio,
    create_time_padded_video,
    extract_audio,
    mux_video_audio,
    probe_video,
)
from .models import VideoProbeInfo, VideoSegmentData

logger = logging.getLogger(__name__)


def make_ffwd_concat_video(
    video_src: Path,
    work_dir: Path,
    output_filename: str,
    *,
    probe_info: VideoProbeInfo,
    blank_audio_duration: float = 5.0,
    padding_buffer: int = 1,
) -> tuple[VideoSegmentData, Path]:
    """Build the concatenated fast-forward video.

    Parameters
    ----------
    video_src:
        Path to the source video file.
    work_dir:
        Directory for intermediate files (caller manages cleanup).
    output_filename:
        Filename for the final output (placed inside *work_dir*).
    probe_info:
        Pre-computed ffprobe metadata for *video_src*.
    blank_audio_duration:
        Length of the silent audio clip used for padding (seconds).
    padding_buffer:
        Extra seconds added when ceiling each segment's duration.

    Returns
    -------
    A tuple of ``(VideoSegmentData, path_to_output_file)``.
    """
    video_duration = probe_info.duration

    # --- Shared blank audio used for padding every segment ----------------
    blank_audio = work_dir / "blank_audio.mp4"
    create_blank_audio(
        blank_audio,
        blank_audio_duration,
        probe_info.audio_channel_layout,
        probe_info.audio_sample_rate,
    )

    # --- 1x segment (original speed) -------------------------------------
    # Pad the source video to ceil(duration) + buffer seconds.
    fit_duration = int(math.ceil(video_duration + padding_buffer))
    fit_video = work_dir / "src_fit.mp4"
    create_time_padded_video(video_src, fit_video, fit_duration)

    padded_audio = work_dir / "src_padded.mp4"
    create_padded_audio(video_src, padded_audio, blank_audio)

    # We'll also need the raw audio track for the atempo filter later.
    raw_audio = work_dir / "src_audio.mp4"
    extract_audio(video_src, raw_audio)

    # Accumulate metadata for each segment.
    rates: list[int] = [1]
    durations: list[float] = [video_duration]
    padded_durations: list[int] = [fit_duration]
    video_files: list[Path] = [fit_video]
    audio_files: list[Path] = [padded_audio]

    # --- Sped-up segments (2x, 4x, 8x, ...) ------------------------------
    doubling_count = 1
    rate = 2
    ffwd_duration = video_duration

    while ffwd_duration > 1:
        logger.info("Creating %dx segment ...", rate)

        # Video: speed up via setpts filter (requires transcode).
        ffwd_video = work_dir / f"ffwd_video_{rate}.mp4"
        create_fast_forward_video(video_src, ffwd_video, rate, probe_info.time_base)

        # Probe the result to get the actual (not estimated) duration.
        ffwd_info = probe_video(ffwd_video)
        ffwd_duration = ffwd_info.duration

        # Pad to integer seconds.
        ffwd_fit_duration = int(math.ceil(ffwd_duration + padding_buffer))
        ffwd_fit_video = work_dir / f"ffwd_video_fit_{rate}.mp4"
        create_time_padded_video(ffwd_video, ffwd_fit_video, ffwd_fit_duration)

        # Audio: speed up via chained atempo=2.0 filters, then pad.
        ffwd_audio = work_dir / f"ffwd_audio_{rate}.mp4"
        create_fast_forward_audio(raw_audio, ffwd_audio, doubling_count)
        ffwd_audio_padded = work_dir / f"ffwd_audio_pad_{rate}.mp4"
        concatenate_media([ffwd_audio, blank_audio], ffwd_audio_padded)

        rates.append(rate)
        durations.append(ffwd_duration)
        padded_durations.append(ffwd_fit_duration)
        video_files.append(ffwd_fit_video)
        audio_files.append(ffwd_audio_padded)

        doubling_count += 1
        rate = 2 ** doubling_count

    # --- Concatenate all segments -----------------------------------------
    concat_video = work_dir / "final_video.mp4"
    concatenate_media(video_files, concat_video)

    concat_audio = work_dir / "final_audio.mp4"
    concatenate_media(audio_files, concat_audio, padded_durations)

    logger.info(
        "Padded durations: %s  (total %ds)",
        padded_durations, sum(padded_durations),
    )

    # --- Mux video + audio (stream copy — NO re-encode) -------------------
    joined = work_dir / "final_join.mp4"
    mux_video_audio(concat_video, concat_audio, joined)

    # --- Final time-pad with audio included -------------------------------
    total_duration = sum(padded_durations)
    dest = work_dir / output_filename
    create_time_padded_video(joined, dest, total_duration, include_audio=True)

    segment_data = VideoSegmentData(
        rates=rates,
        durations=durations,
        padded_durations=padded_durations,
    )
    return segment_data, dest
