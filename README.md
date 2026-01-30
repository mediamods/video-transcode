# video-transcode

AWS Lambda function that processes uploaded videos for web playback. Receives jobs from SQS, downloads source video from S3, runs a multi-step ffmpeg pipeline (normalize, fast-forward segments, thumbnail montage, chapter parsing), uploads results to S3, and reports completion to Convex.

Designed to drop into `functions/video-processor/` in the `lambda-upload-processor` SAM project, replacing the Node.js stub.

## Project structure

```
├── Dockerfile              # Lambda container image (Python 3.12 + static ffmpeg)
├── requirements.txt        # Python dependencies (Pillow)
├── handler.py              # Lambda entry point — SQS batch handler
├── s3_utils.py             # S3 download / upload helpers (boto3)
├── convex_client.py        # Convex HTTP client for status reporting
├── video_prep/             # Core video processing pipeline
│   ├── __init__.py
│   ├── handler.py          # Pipeline orchestrator (probe → normalize → ffwd → montage → metadata)
│   ├── models.py           # Dataclasses: config, probe info, segment data, montage data, chapters
│   ├── ffmpeg_utils.py     # Typed ffmpeg/ffprobe subprocess wrappers
│   ├── ffwd_video_maker.py # Fast-forward video generation (1x, 2x, 4x, 8x…)
│   ├── montager.py         # Thumbnail montage grid (Pillow, no ImageMagick needed)
│   ├── joiner.py           # Multi-video concatenation with silent gaps
│   └── chapterer.py        # Chapter file parser (START=/TITLE= text format)
└── events/
    └── test_event.json     # Sample SQS event for local testing
```

## SQS message format

```json
{
  "docId": "abc123",
  "versionId": "v1",
  "s3Key": "uploads/abc123/video.mp4",
  "chapterFile": "uploads/abc123/chapters.txt"
}
```

`chapterFile` is optional. All other fields are required.

## S3 output structure

After processing, the following files are uploaded to the destination bucket:

```
video/{docId}/
├── video/
│   └── video.mp4       # Fast-forward concatenated video (1x, 2x, 4x, 8x…)
├── montage.jpg          # Thumbnail grid (one frame per second)
└── {docId}.avd          # JSON metadata (segment data, montage dimensions, chapters)
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `SOURCE_BUCKET` | `adapt-uploads` | S3 bucket for source videos |
| `DEST_BUCKET` | `adapt-cdn` | S3 bucket for processed outputs |
| `CONVEX_URL` | _(none)_ | Convex deployment URL for status reporting |

## Integration with lambda-upload-processor

Copy this repo's contents into `functions/video-processor/` in the SAM project. The `template.yaml` changes needed:

```yaml
VideoProcessorFunction:
  Type: AWS::Serverless::Function
  Metadata:
    Dockerfile: Dockerfile
    DockerContext: functions/video-processor/
    DockerTag: v1
  Properties:
    FunctionName: !Sub "adapt-video-processor-${Environment}"
    PackageType: Image
    Timeout: 900
    MemorySize: 3008
    EphemeralStorage:
      Size: 2048
    Events:
      SQSEvent:
        Type: SQS
        Properties:
          Queue: !GetAtt VideoProcessingQueue.Arn
          BatchSize: 1
          FunctionResponseTypes:
            - ReportBatchItemFailures
    Policies:
      - S3ReadPolicy:
          BucketName: !Ref SourceBucketName
      - S3WritePolicy:
          BucketName: !Ref DestBucketName
      - SQSPollerPolicy:
          QueueName: !GetAtt VideoProcessingQueue.QueueName
```

Compared to the current Node.js config:
- `PackageType: Image` replaces `Runtime: nodejs20.x` + `Handler` + `Layers`
- `MemorySize: 3008` (up from 1024) — ffmpeg transcoding is CPU-bound; Lambda allocates proportional vCPU
- `Timeout: 900` (up from 300) — long videos with fast-forward generation need more time
- `FFmpegLayer` removed — ffmpeg is baked into the container image
- No ImageMagick needed — montage uses Pillow (in-process, faster)

## Local development

Build and test with SAM CLI:

```bash
sam build
sam local invoke VideoProcessorFunction -e events/test_event.json --env-vars env.json
```

Or run the pipeline directly (requires ffmpeg on PATH):

```bash
python -c "
from video_prep import process_video, VideoProcessingConfig
from pathlib import Path

config = VideoProcessingConfig(
    video_file=Path('input.mp4'),
    video_id='test',
    export_dir=Path('output/'),
)
process_video(config)
"
```

## Dependencies

- **Python 3.12** (Lambda runtime)
- **ffmpeg / ffprobe** (static binaries via `mwader/static-ffmpeg` Docker image)
- **Pillow** (thumbnail montage — replaces ImageMagick)
- **boto3** (pre-installed in Lambda runtime)
