FROM public.ecr.aws/lambda/python:3.12

# Static ffmpeg + ffprobe binaries
COPY --from=mwader/static-ffmpeg:latest /ffmpeg /usr/local/bin/ffmpeg
COPY --from=mwader/static-ffmpeg:latest /ffprobe /usr/local/bin/ffprobe

# Python dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Application code
COPY *.py ${LAMBDA_TASK_ROOT}/
COPY video_prep/ ${LAMBDA_TASK_ROOT}/video_prep/

CMD ["handler.handler"]
