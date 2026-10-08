# One image, two commands:
#   API    : uvicorn cloud.api:app --host 0.0.0.0 --port 8000   (the default CMD)
#   worker : python cloud/aws_worker.py
#
# No model is downloaded here - faster-whisper fetches its weights on the first
# job - and no secret is baked in: GEMINI_API_KEY and the AWS role come from the
# task definition / instance at runtime.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/var/tmp/hf

# ffmpeg/ffprobe for the render, libgomp1 because ctranslate2 (faster-whisper)
# links against OpenMP and slim images do not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY cloud/ ./cloud/
COPY recap_runtime/ ./recap_runtime/
COPY services/ ./services/

# The container never runs as root; the worker only writes to /tmp.
RUN useradd --create-home --uid 10001 recap \
    && mkdir -p /var/tmp/hf \
    && chown -R recap:recap /app /var/tmp/hf
USER recap

ENV PYTHONPATH=/app/app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "cloud.api:app", "--host", "0.0.0.0", "--port", "8000"]
