"""Small AWS-facing API for upload, enqueue, and job status.

Run behind HTTPS (API Gateway, ALB, or a reverse proxy). The browser uploads
straight to S3 using a short-lived presigned URL; the API never buffers the
video in memory.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import boto3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Recap Jobs API", version="0.1.0")
s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION"))
sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION"))
MEDIA_BUCKET = os.getenv("RECAP_MEDIA_BUCKET", "").strip()
QUEUE_URL = os.getenv("RECAP_SQS_QUEUE_URL", "").strip()
MAX_UPLOAD_BYTES = int(os.getenv("RECAP_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".mp3", ".wav", ".m4a"}


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="application/octet-stream", max_length=120)
    voice: str = Field(default="my-MM-ThihaNeural", max_length=80)


class EnqueueRequest(BaseModel):
    job_id: str = Field(min_length=8, max_length=80)
    voice: str = Field(default="my-MM-ThihaNeural", max_length=80)


def _settings() -> None:
    if not MEDIA_BUCKET or not QUEUE_URL:
        raise HTTPException(status_code=503, detail="AWS media bucket or queue is not configured")


def _safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip(".")
    if not name:
        raise HTTPException(status_code=400, detail="invalid filename")
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"unsupported file type: {suffix or 'none'}")
    return name


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/uploads")
def create_upload(request: UploadRequest) -> dict[str, Any]:
    _settings()
    filename = _safe_filename(request.filename)
    job_id = uuid.uuid4().hex
    key = f"uploads/{job_id}/{filename}"
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": MEDIA_BUCKET, "Key": key, "ContentType": request.content_type},
        ExpiresIn=900,
        HttpMethod="PUT",
    )
    return {"job_id": job_id, "input_bucket": MEDIA_BUCKET, "input_key": key,
            "upload_url": upload_url, "expires_in": 900, "max_bytes": MAX_UPLOAD_BYTES,
            "voice": request.voice}


@app.post("/v1/jobs")
def enqueue_job(request: EnqueueRequest) -> dict[str, Any]:
    _settings()
    # The client must enqueue only a job ID returned by /v1/uploads. The
    # worker will reject a missing object when it tries to download it.
    prefix = f"uploads/{request.job_id}/"
    listed = s3.list_objects_v2(Bucket=MEDIA_BUCKET, Prefix=prefix, MaxKeys=2)
    objects = listed.get("Contents", [])
    if not objects:
        raise HTTPException(status_code=404, detail="uploaded file not found")
    input_key = objects[0]["Key"]
    message = {"job_id": request.job_id, "input_bucket": MEDIA_BUCKET,
               "input_key": input_key, "output_bucket": MEDIA_BUCKET,
               "output_prefix": f"jobs/{request.job_id}", "voice": request.voice}
    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(message, ensure_ascii=False))
    return {"job_id": request.job_id, "state": "queued", "status_key": f"jobs/{request.job_id}/status.json"}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    _settings()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", job_id):
        raise HTTPException(status_code=400, detail="invalid job id")
    try:
        result = s3.get_object(Bucket=MEDIA_BUCKET, Key=f"jobs/{job_id}/status.json")
        return json.loads(result["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {"job_id": job_id, "state": "queued"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="could not read job status") from exc
