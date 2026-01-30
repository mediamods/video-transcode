# Architecture

## System context

```
┌──────────┐     SQS      ┌──────────────────┐     S3      ┌──────────┐
│  Client  │──────────────▶│  video-transcode  │────────────▶│  CDN S3  │
│  App     │               │  (Lambda)         │             │  Bucket  │
└──────────┘               └────────┬─────────┘             └──────────┘
                                    │
                                    │ HTTP
                                    ▼
                              ┌──────────┐
                              │  Convex  │
                              └──────────┘
```

1. Client app publishes an SQS message with `docId`, `versionId`, and `s3Key`.
2. Lambda downloads the source video from S3.
3. The `video_prep` pipeline processes it (normalize, fast-forward, montage, chapters).
4. Outputs are uploaded to the CDN S3 bucket.
5. Completion status is reported to Convex (`documents:completePublish` mutation).

## Lambda handler flow

```
handler.handler(event, context)
│
├── for each SQS record:
│   │
│   ├── Parse message body → { docId, versionId, s3Key, chapterFile? }
│   │
│   ├── _process_video()
│   │   ├── Download source video from S3
│   │   ├── Download chapter file from S3 (optional)
│   │   ├── video_prep.process_video(config)    ← core pipeline
│   │   └── Upload export_dir/ to S3
│   │
│   ├── report_to_convex(status="published")
│   │
│   └── on error:
│       ├── report_to_convex(status="failed")
│       └── add to batchItemFailures
│
└── return { batchItemFailures }
```

## Video processing pipeline

`video_prep.process_video()` in [video_prep/handler.py](video_prep/handler.py) runs these steps:

```
Input video (any format)
    │
    ▼
┌─────────────────────────────────────┐
│ 1. check_dependencies()             │  Verify ffmpeg + ffprobe on PATH
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 2. probe_video()                    │  Single ffprobe call → VideoProbeInfo
│    duration, resolution, codecs,    │  (replaces 5 old separate functions)
│    pixel format, container format   │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 3. normalize_for_web()              │  Ensure H.264 / AAC / yuv420p / MP4
│    ├── if compatible → remux (fast) │  Stream copy, no quality loss
│    └── if not → transcode (slow)    │  libx264 CRF 18 + AAC 128k
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 4. make_ffwd_concat_video()         │  Exponentially sped-up segments
│    1x → 2x → 4x → 8x → 16x → …   │  until segment < 1 second
│    Each segment padded to integer   │  seconds for client seeking
│    Video: setpts filter             │
│    Audio: chained atempo=2.0        │
│    Final: concat + mux (no re-enc)  │
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 5. make_montage()                   │  One frame per second of video
│    Extract → resize → blur → grid   │  Pillow-based (no ImageMagick)
│    Output: progressive JPEG         │  Grid layout fits JPEG 65500px max
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 6. parse_chapters() (optional)      │  Text file: START=HH:MM:SS.us
│                                     │             TITLE=Chapter Name
└─────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────┐
│ 7. Write .avd metadata              │  Compact JSON with single-letter keys
│    { I, V:{R,D,X}, M:{W,H,B,N}, C }│  for backward compat with client app
└─────────────────────────────────────┘
```

## Output format

### S3 layout

```
video/{docId}/
├── video/
│   └── video.mp4         # Concatenated fast-forward video
├── montage.jpg            # Thumbnail grid JPEG
└── {docId}.avd            # Metadata JSON
```

### .avd metadata schema

```json
{
  "I": "docId",
  "V": {
    "R": [1, 2, 4, 8, 16],
    "D": [120.5, 60.3, 30.1, 15.0, 7.5],
    "X": [122, 62, 32, 17, 9]
  },
  "M": {
    "W": 30,
    "H": 17,
    "B": 12,
    "N": 120
  },
  "C": [
    ["Chapter Title", 540.368]
  ]
}
```

| Key | Meaning |
|---|---|
| `I` | Video / document ID |
| `V.R` | Playback rates per segment (1, 2, 4, 8, ...) |
| `V.D` | Actual duration of each segment (seconds) |
| `V.X` | Padded duration of each segment (integer seconds, for seeking) |
| `M.W` | Thumbnail width (px) |
| `M.H` | Thumbnail height (px) |
| `M.B` | Grid columns ("breadth") |
| `M.N` | Total thumbnail count |
| `C` | Array of `[title, start_seconds]` chapter entries |

## Module responsibilities

| Module | Responsibility |
|---|---|
| `handler.py` | Lambda entry point. SQS event parsing, S3 I/O orchestration, error handling, Convex reporting. |
| `s3_utils.py` | boto3 wrappers for download/upload. LocalStack support for local dev. |
| `convex_client.py` | Best-effort HTTP POST to Convex `documents:completePublish` mutation. |
| `video_prep/handler.py` | Pipeline orchestrator. Calls ffmpeg utilities in sequence, manages temp directory. |
| `video_prep/models.py` | Typed dataclasses for config, probe results, and output metadata. |
| `video_prep/ffmpeg_utils.py` | All ffmpeg/ffprobe subprocess calls. Single `run_ffmpeg()` runner with logging and error handling. |
| `video_prep/ffwd_video_maker.py` | Fast-forward segment generation and concatenation. |
| `video_prep/montager.py` | Pillow-based thumbnail grid generation (replaces ImageMagick). |
| `video_prep/joiner.py` | Multi-video concatenation with silent gaps. |
| `video_prep/chapterer.py` | Chapter text file parser. |

## Key design decisions

**Container image over Lambda layers.** ffmpeg static binaries are ~80 MB. A container image avoids the 250 MB unzipped layer limit and simplifies dependency management. Uses `mwader/static-ffmpeg` as the binary source.

**Pillow over ImageMagick.** The original montage code shelled out to ImageMagick (`convert` + `montage`) for every thumbnail — N+2 subprocesses per video. Pillow handles all image operations in-process with a single pip dependency. No ImageMagick installation needed.

**Stream-copy muxing.** The original code had a bug where `copy_video = ''` caused a full re-encode during the final video+audio mux step. The rewrite uses `-c:v copy -c:a copy` consistently, avoiding unnecessary transcoding.

**Integer-second segment padding.** Each fast-forward segment is padded to `ceil(duration) + 1` seconds. This lets the client app seek to exact second boundaries without frame-level precision, simplifying the player implementation.

**Single ffprobe call.** The original code called ffprobe 5 times per video (duration, resolution, audio, timescale, format). The rewrite probes once and returns a `VideoProbeInfo` dataclass.

**Temp directory auto-cleanup.** All intermediate files live in `tempfile.TemporaryDirectory()` (inside `video_prep`) and `tempfile.mkdtemp()` (in `handler.py`). Both are cleaned up in `finally` blocks, critical for Lambda's limited `/tmp` storage.

**Best-effort Convex reporting.** Convex notification uses stdlib `urllib` (no extra dependency). Failures are logged but never raised — the video processing result is not lost if Convex is temporarily unreachable.
