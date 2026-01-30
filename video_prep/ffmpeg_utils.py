"""FFmpeg and ffprobe subprocess wrappers.

Extracted from the old ffwd_video_maker.py monolith (which mixed generic
ffmpeg utilities with fast-forward business logic) and from utils.py.

Every function here is a thin, typed wrapper around a single ffmpeg or
ffprobe invocation.  All calls go through ``run_ffmpeg`` which logs the
command, checks the return code, and raises on failure.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
from pathlib import Path

from .models import VideoProbeInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------

def check_dependencies() -> None:
    """Verify that ffmpeg and ffprobe are on the PATH.

    Call once at startup.  On Lambda, ffmpeg comes from a layer — this
    catches misconfiguration early with a clear message.
    """
    for tool in ("ffmpeg", "ffprobe"):
        try:
            subprocess.run(
                [tool, "-version"],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(
                f"{tool} not found. Ensure it is installed or available "
                f"via a Lambda layer."
            ) from exc


# ---------------------------------------------------------------------------
# Core command runner
# ---------------------------------------------------------------------------

def run_ffmpeg(
    args: list[str],
    *,
    description: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg / ffprobe command and return the completed process.

    Replaces the old ``executeCommand`` / ``executeCommandList`` which used
    ``Popen``, ignored return codes, and split command strings on whitespace
    (breaking on paths with spaces).

    Raises ``subprocess.CalledProcessError`` if the process exits non-zero.
    """
    logger.info("Running: %s  [%s]", " ".join(args), description)
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error(
            "Command failed (rc=%d): %s\nstderr: %s",
            result.returncode,
            " ".join(args),
            result.stderr,
        )
        raise subprocess.CalledProcessError(
            result.returncode, args, result.stdout, result.stderr
        )
    return result


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------

def probe_video(src: Path) -> VideoProbeInfo:
    """Run ffprobe once and return all metadata we need.

    Replaces five old functions (getVideoData, getVideoSize, getAudioData,
    getVideoTimescale, getVideoDuration) with a single probe + struct.
    """
    result = run_ffmpeg(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(src),
        ],
        description=f"probe {src.name}",
    )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])

    # --- video stream ---
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise ValueError(f"No video stream found in {src}")
    # Pick the stream with the largest frame area (matches old getVideoSize).
    video = max(video_streams, key=lambda s: s.get("width", 0) * s.get("height", 0))

    duration = float(video["duration"])
    width = int(video["width"])
    height = int(video["height"])

    # time_base is a fraction string like "1/90000" — we want the denominator.
    time_base_str: str = video.get("time_base", "1/90000")
    time_base = int(time_base_str.partition("/")[2])

    video_codec = video.get("codec_name", "unknown")
    pixel_format = video.get("pix_fmt", "unknown")

    # --- audio stream ---
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams:
        audio = audio_streams[0]
        sample_rate = audio.get("sample_rate", "48000")
        channel_layout = audio.get("channel_layout", "stereo")
        audio_codec = audio.get("codec_name", "unknown")
    else:
        sample_rate = "48000"
        channel_layout = "stereo"
        audio_codec = "none"

    # --- container format ---
    container_format = data.get("format", {}).get("format_name", "unknown")

    return VideoProbeInfo(
        duration=duration,
        width=width,
        height=height,
        time_base=time_base,
        audio_sample_rate=sample_rate,
        audio_channel_layout=channel_layout,
        video_codec=video_codec,
        audio_codec=audio_codec,
        pixel_format=pixel_format,
        container_format=container_format,
    )


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def create_blank_audio(
    dest: Path,
    duration: float,
    channel_layout: str = "stereo",
    sample_rate: str = "48000",
) -> None:
    """Generate a silent audio file of the given duration."""
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout={channel_layout}:sample_rate={sample_rate}",
            "-t", str(duration),
            str(dest),
        ],
        description=f"blank audio {duration}s",
    )


def extract_audio(src: Path, dest: Path) -> None:
    """Extract the audio track from a video (stream copy, no re-encode)."""
    run_ffmpeg(
        ["ffmpeg", "-y", "-i", str(src), "-vn", "-acodec", "copy", str(dest)],
        description=f"extract audio from {src.name}",
    )


def create_padded_audio(src: Path, dest: Path, pad_file: Path) -> None:
    """Extract audio from *src* and concatenate silent padding after it.

    This mirrors the old ``makePaddedAudio``: extract -> concat with pad.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_audio = Path(tmp.name)
    try:
        extract_audio(src, tmp_audio)
        concatenate_media([tmp_audio, pad_file], dest)
    finally:
        tmp_audio.unlink(missing_ok=True)


def create_fast_forward_audio(
    src: Path,
    dest: Path,
    doubling_count: int,
) -> None:
    """Speed up audio by chaining atempo=2.0 filters.

    ffmpeg's ``atempo`` filter only supports the range [0.5, 2.0], so to
    achieve higher rates we chain multiple doublings.  ``doubling_count``
    is the number of doublings (e.g. 3 means 2^3 = 8x speed).

    The old code called this parameter ``times`` and computed
    ``['atempo=2.0'] * (times - 1)``.
    """
    filter_chain = ",".join(["atempo=2.0"] * doubling_count)
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-vn",
            "-filter:a", filter_chain,
            "-map_chapters", "-1",
            str(dest),
        ],
        description=f"fast-forward audio x{2 ** doubling_count}",
    )


# ---------------------------------------------------------------------------
# Video helpers
# ---------------------------------------------------------------------------

def strip_chapters(src: Path, dest: Path) -> None:
    """Copy video/audio streams while removing chapter metadata."""
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-codec", "copy",
            "-map_chapters", "-1",
            str(dest),
        ],
        description=f"strip chapters from {src.name}",
    )


def create_time_padded_video(
    src: Path,
    dest: Path,
    duration: int,
    *,
    include_audio: bool = False,
) -> None:
    """Pad (or trim) *src* to an exact *duration* in seconds.

    Process (mirrors old ``makeTimePaddedVideo``):
    1. Strip existing chapter metadata (so we can inject our own).
    2. Write an FFMETADATA file with a single chapter spanning 0 → *duration*.
    3. Mux the chapter-stripped video with the new metadata.

    The chapter trick is how the old code stamped a known duration onto each
    segment — ffmpeg uses the chapter end-time to set the container duration.
    """
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f_tmp:
        tmp_stripped = Path(f_tmp.name)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False,
    ) as f_meta:
        meta_path = Path(f_meta.name)
        end_ms = int(duration * 1000)
        f_meta.write(
            f";FFMETADATA1\ntitle=x\n\n"
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND={end_ms}\n"
            f"TITLE=x\n[STREAM]\ntitle=multi\\\nline"
        )

    try:
        strip_chapters(src, tmp_stripped)

        audio_flag: list[str] = [] if include_audio else ["-an"]
        run_ffmpeg(
            [
                "ffmpeg", "-y",
                "-i", str(tmp_stripped),
                "-i", str(meta_path),
                "-map_metadata", "1",
                *audio_flag,
                "-codec", "copy",
                str(dest),
            ],
            description=f"time-pad to {duration}s",
        )
    finally:
        tmp_stripped.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)


def create_fast_forward_video(
    src: Path,
    dest: Path,
    rate: int,
    time_base: int = 90000,
) -> None:
    """Create a sped-up copy of *src* using the ``setpts`` filter.

    This forces a full decode → filter → encode cycle, which is unavoidable
    for rate > 1.  Audio is stripped (handled separately).
    """
    rate_div = 1.0 / rate
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-an",
            "-vf", f"setpts={rate_div}*PTS",
            "-video_track_timescale", str(time_base),
            str(dest),
        ],
        description=f"fast-forward video x{rate}",
    )


# ---------------------------------------------------------------------------
# Concatenation & muxing
# ---------------------------------------------------------------------------

def concatenate_media(
    files: list[Path],
    target: Path,
    trim_lengths: list[float | int] | None = None,
) -> None:
    """Concatenate media files using the ffmpeg concat demuxer (stream copy).

    If *trim_lengths* is provided, each file is trimmed to the corresponding
    duration via the ``outpoint`` directive.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False,
    ) as f_list:
        list_path = Path(f_list.name)
        lines: list[str] = []
        if trim_lengths is None:
            for f in files:
                lines.append(f"file '{f}'")
        else:
            for f, trim in zip(files, trim_lengths):
                lines.append(f"file '{f}'")
                lines.append(f"outpoint {trim}")
        f_list.write("\n".join(lines))

    try:
        run_ffmpeg(
            [
                "ffmpeg", "-y",
                "-safe", "0",
                "-f", "concat",
                "-i", str(list_path),
                "-c", "copy",
                str(target),
            ],
            description=f"concatenate {len(files)} files",
        )
    finally:
        list_path.unlink(missing_ok=True)


def mux_video_audio(
    video_file: Path,
    audio_file: Path,
    dest: Path,
) -> None:
    """Mux separate video and audio streams into one container.

    BUG FIX: The old code (ffwd_video_maker.py:279, joiner.py:54) had
    ``copy_video = ''`` instead of ``'-c:v copy'``, causing a full
    re-encode of the video stream every time.  This function always
    stream-copies both tracks.
    """
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(video_file),
            "-i", str(audio_file),
            "-c:v", "copy",
            "-c:a", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            str(dest),
        ],
        description="mux video+audio (stream copy)",
    )


# ---------------------------------------------------------------------------
# Web normalization
# ---------------------------------------------------------------------------

def normalize_for_web(
    src: Path,
    dest: Path,
    probe_info: VideoProbeInfo,
    *,
    crf: int = 18,
) -> Path:
    """Ensure *src* is in a web-compatible format (H.264 / AAC / MP4 / yuv420p).

    If the source already meets all criteria, it is remuxed into a clean
    MP4 container (stream copy — fast, no quality loss).  Otherwise a full
    transcode to libx264 + aac is performed.

    Parameters
    ----------
    src:
        Path to the uploaded / source video.
    dest:
        Path where the normalised video should be written.
    probe_info:
        Pre-computed probe results for *src*.
    crf:
        H.264 Constant Rate Factor used when transcoding is needed.
        Lower = higher quality.  18 is visually lossless.

    Returns
    -------
    *dest* — the path to the normalised file (for convenience in chaining).
    """
    if probe_info.is_web_compatible:
        # Already compatible — remux into a clean MP4 (normalises container
        # quirks like missing moov atoms, odd metadata, etc.).
        logger.info(
            "Source is web-compatible (%s/%s/%s) — remuxing only.",
            probe_info.video_codec,
            probe_info.audio_codec,
            probe_info.pixel_format,
        )
        args = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(dest),
        ]
    else:
        logger.info(
            "Source needs normalisation (video=%s, audio=%s, pix=%s, container=%s) "
            "— transcoding to H.264/AAC/yuv420p.",
            probe_info.video_codec,
            probe_info.audio_codec,
            probe_info.pixel_format,
            probe_info.container_format,
        )
        args = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(dest),
        ]

    run_ffmpeg(args, description="normalize for web")
    return dest


# ---------------------------------------------------------------------------
# Frame extraction (moved from old utils.py)
# ---------------------------------------------------------------------------

def _secs_to_timecode(s: int) -> str:
    """Convert integer seconds to HH:MM:SS format for ffmpeg ``-ss``."""
    hours = s // 3600
    minutes = (s % 3600) // 60
    secs = s % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def extract_frame(video: Path, dest: Path, seconds: int) -> None:
    """Extract a single frame at the given second offset.

    Uses ``-ss`` before ``-i`` for fast keyframe-based seeking.
    Falls back to the previous second if the requested position fails
    (replicates the old montager.py recovery logic).
    """
    tc = _secs_to_timecode(seconds)
    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-ss", tc,
            "-i", str(video),
            "-vf", "thumbnail",
            "-frames:v", "1",
            "-vf", "scale=iw*sar:ih",
            str(dest),
        ],
        description=f"extract frame at {tc}",
    )
