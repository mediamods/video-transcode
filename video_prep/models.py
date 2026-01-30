"""Data models for the video preparation pipeline.

All structured data flows through these dataclasses. Output serialization
methods (to_dict / to_list) preserve the compact single-letter key format
used by the .avd JSON files for backward compatibility with the client app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class VideoProcessingConfig:
    """Input configuration for the video processing pipeline.

    Every formerly-hardcoded magic number is a named field here with its
    historical default value, so callers that don't care can ignore them.
    """

    video_file: Path
    video_id: str
    export_dir: Path
    chapter_file: Path | None = None

    # Montage tuning
    thumbnail_width: int = 30          # px — width of each thumbnail in the grid
    jpeg_quality: int = 85             # 1-100 JPEG quality for the montage
    blur_sigma: float = 0.5            # Pillow GaussianBlur radius (see montager.py)

    # Fast-forward video tuning
    blank_audio_duration: float = 5.0  # seconds of silence used for padding
    padding_buffer_seconds: int = 1    # extra seconds added when ceiling each segment

    # Web normalization
    web_crf: int = 18                  # H.264 CRF for normalization (0=lossless, 23=default, 51=worst)

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> VideoProcessingConfig:
        """Construct from an AWS Lambda event dict."""
        return cls(
            video_file=Path(event["video_file"]),
            video_id=event["video_id"],
            export_dir=Path(event["export_dir"]),
            chapter_file=Path(event["chapter_file"]) if event.get("chapter_file") else None,
            thumbnail_width=event.get("thumbnail_width", 30),
            jpeg_quality=event.get("jpeg_quality", 85),
            blur_sigma=event.get("blur_sigma", 0.5),
            blank_audio_duration=event.get("blank_audio_duration", 5.0),
            padding_buffer_seconds=event.get("padding_buffer_seconds", 1),
            web_crf=event.get("web_crf", 18),
        )


# ---------------------------------------------------------------------------
# ffprobe result
# ---------------------------------------------------------------------------

@dataclass
class VideoProbeInfo:
    """Parsed ffprobe metadata for a video file.

    Collapses what the old code spread across five separate functions
    (getVideoData, getVideoSize, getAudioData, getVideoTimescale,
    getVideoDuration) into a single probe + single struct.
    """

    duration: float           # seconds (from the video stream)
    width: int
    height: int
    time_base: int            # video_track_timescale (e.g. 90000)
    audio_sample_rate: str    # e.g. "48000"
    audio_channel_layout: str # e.g. "stereo"
    video_codec: str          # e.g. "h264", "hevc", "vp9"
    audio_codec: str          # e.g. "aac", "mp3", "opus", "none"
    pixel_format: str         # e.g. "yuv420p", "yuv444p"
    container_format: str     # e.g. "mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"

    @property
    def is_web_compatible(self) -> bool:
        """Check if the video is already in a web-friendly format.

        Web browsers broadly support H.264 + AAC in MP4 with yuv420p.
        If all four conditions are met, we can skip the normalization
        transcode and just remux (or even pass through directly).
        """
        h264_ok = self.video_codec == "h264"
        aac_ok = self.audio_codec in ("aac", "none")
        pix_ok = self.pixel_format == "yuv420p"
        container_ok = any(
            tag in self.container_format for tag in ("mp4", "mov", "m4a")
        )
        return h264_ok and aac_ok and pix_ok and container_ok


# ---------------------------------------------------------------------------
# Output metadata — serialised into .avd JSON
# ---------------------------------------------------------------------------

@dataclass
class VideoSegmentData:
    """Metadata for the fast-forward concatenated video.

    Each list is parallel-indexed: rates[i] / durations[i] / padded_durations[i]
    describe the same segment.
    """

    rates: list[int]             # playback rates (1, 2, 4, 8, ...)
    durations: list[float]       # actual duration of each segment (seconds)
    padded_durations: list[int]  # integer-second padded duration of each segment

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the compact .avd format."""
        return {"R": self.rates, "D": self.durations, "X": self.padded_durations}


@dataclass
class MontageData:
    """Metadata for the thumbnail montage image."""

    thumb_width: int   # px — width of each thumbnail cell
    thumb_height: int  # px — height of each thumbnail cell
    columns: int       # number of columns in the grid ("breadth")
    thumb_count: int   # total number of thumbnails extracted

    def to_dict(self) -> dict[str, Any]:
        return {
            "W": self.thumb_width,
            "H": self.thumb_height,
            "B": self.columns,
            "N": self.thumb_count,
        }


@dataclass
class ChapterEntry:
    """A single chapter marker."""

    title: str
    start_seconds: float

    def to_list(self) -> list:
        """Serialize to the [title, seconds] pair used in .avd files."""
        return [self.title, self.start_seconds]


@dataclass
class VideoMetadata:
    """Top-level .avd output structure."""

    video_id: str
    video_data: VideoSegmentData | None = None
    montage_data: MontageData | None = None
    chapters: list[ChapterEntry] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"I": self.video_id}
        if self.video_data:
            result["V"] = self.video_data.to_dict()
        if self.montage_data:
            result["M"] = self.montage_data.to_dict()
        if self.chapters:
            result["C"] = [c.to_list() for c in self.chapters]
        return result
