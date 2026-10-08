"""Small AWS-facing API for upload, enqueue, and job status.

Run behind HTTPS (API Gateway, ALB, or a reverse proxy). The browser uploads
straight to S3 using a short-lived presigned POST policy; the API never
buffers the video in memory.

Endpoints
    GET  /health                     liveness probe
    POST /v1/uploads                 presigned S3 POST policy (size limited)
    POST /v1/jobs                    enqueue a job on SQS
    GET  /v1/jobs/{job_id}           job status (from jobs/<id>/status.json)
    GET  /v1/jobs/{job_id}/output    presigned download of the finished video

AWS credentials always come from the environment / instance role. No access
key is ever read from, or written into, a request body.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

LOG = logging.getLogger("recap.api")

app = FastAPI(title="Recap Jobs API", version="0.2.0")

allowed_origins = [item.strip() for item in os.getenv("RECAP_ALLOWED_ORIGINS", "*").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials="*" not in allowed_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept"],
    max_age=600,
)

AWS_REGION = (
    os.getenv("AWS_REGION")
    or os.getenv("AWS_DEFAULT_REGION")
    or os.getenv("RECAP_AWS_REGION")
    or ""
).strip()
if not AWS_REGION:
    # Fail fast and say why, instead of a botocore NoRegionError deep in boto3.
    raise RuntimeError("AWS_REGION (or AWS_DEFAULT_REGION) must be set before the API starts")
s3 = boto3.client("s3", region_name=AWS_REGION)
sqs = boto3.client("sqs", region_name=AWS_REGION)

MEDIA_BUCKET = os.getenv("RECAP_MEDIA_BUCKET", "").strip()
QUEUE_URL = os.getenv("RECAP_SQS_QUEUE_URL", "").strip()
MAX_UPLOAD_BYTES = int(os.getenv("RECAP_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
UPLOAD_TTL_SECONDS = int(os.getenv("RECAP_UPLOAD_TTL_SECONDS", "900"))
DOWNLOAD_TTL_SECONDS = int(os.getenv("RECAP_DOWNLOAD_TTL_SECONDS", "3600"))
ALLOWED_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".mp3", ".wav", ".m4a"}
JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}")
CONTENT_TYPE_PATTERN = re.compile(r"[A-Za-z0-9!#$&^_/+.+-]{1,120}")


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str = Field(default="application/octet-stream", max_length=120)
    size_bytes: int = Field(default=0, ge=0)
    voice: str = Field(default="my-MM-ThihaNeural", max_length=80)


class EnqueueRequest(BaseModel):
    job_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]{8,80}$")
    voice: str = Field(default="my-MM-ThihaNeural", max_length=80)


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
    """Never leak a traceback, a bucket name, or a stack frame to the client."""
    LOG.exception("unhandled API error")
    return JSONResponse(status_code=500, content={"detail": "the server could not finish that request"})


def _settings() -> None:
    if not MEDIA_BUCKET or not QUEUE_URL:
        raise HTTPException(status_code=503, detail="the service is still being configured, try again later")


def _client_error(exc: BaseException) -> str:
    """The AWS error code, or "" for a BotoCoreError that never reached AWS."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str(response.get("Error", {}).get("Code", ""))


def _safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        raise HTTPException(status_code=400, detail="that file name cannot be used")
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported file type: {suffix or 'none'}. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    return name


def _safe_content_type(content_type: str) -> str:
    value = (content_type or "application/octet-stream").split(";", 1)[0].strip()
    if not CONTENT_TYPE_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="that content type cannot be used")
    return value


def _status_key(job_id: str) -> str:
    return f"jobs/{job_id}/status.json"


def _presigned_download(job_id: str, output_key: str) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": MEDIA_BUCKET, "Key": output_key, "ResponseContentDisposition": f'attachment; filename="{job_id}.mp4"'},
        ExpiresIn=DOWNLOAD_TTL_SECONDS,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "bucket_configured": str(bool(MEDIA_BUCKET)), "queue_configured": str(bool(QUEUE_URL))}


@app.post("/v1/uploads")
def create_upload(request: UploadRequest) -> dict[str, Any]:
    _settings()
    filename = _safe_filename(request.filename)
    content_type = _safe_content_type(request.content_type)
    if request.size_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"that file is too large; the limit is {MAX_UPLOAD_BYTES} bytes",
        )
    job_id = uuid4().hex
    key = f"uploads/{job_id}/{filename}"
    # A presigned POST (not PUT) is what lets S3 itself reject an oversized
    # upload, so the size limit cannot be bypassed by a modified browser.
    try:
        policy = s3.generate_presigned_post(
            Bucket=MEDIA_BUCKET,
            Key=key,
            Fields={"Content-Type": content_type, "success_action_status": "201"},
            Conditions=[
                {"Content-Type": content_type},
                {"success_action_status": "201"},
                ["content-length-range", 1, MAX_UPLOAD_BYTES],
            ],
            ExpiresIn=UPLOAD_TTL_SECONDS,
        )
    except Exception as exc:
        # Deliberately broad: signing raises ClientError for a bad request, but a
        # missing IAM role surfaces as NoCredentialsError and even a bare
        # AttributeError inside botocore. All of them mean "not configured yet".
        LOG.error("could not sign an upload for %s: %s", key, exc)
        raise HTTPException(status_code=502, detail="could not create the upload link, try again") from exc
    return {
        "job_id": job_id,
        "input_bucket": MEDIA_BUCKET,
        "input_key": key,
        "upload_url": policy["url"],
        "upload_fields": policy["fields"],
        "expires_in": UPLOAD_TTL_SECONDS,
        "max_bytes": MAX_UPLOAD_BYTES,
        "voice": request.voice,
    }


@app.post("/v1/jobs")
def enqueue_job(request: EnqueueRequest) -> dict[str, Any]:
    _settings()
    # job_id is validated by the model above, so it can only ever address
    # uploads/<safe-id>/ - it cannot walk out of that prefix.
    prefix = f"uploads/{request.job_id}/"
    try:
        listed = s3.list_objects_v2(Bucket=MEDIA_BUCKET, Prefix=prefix, MaxKeys=2)
    except (ClientError, BotoCoreError) as exc:
        LOG.error("could not list %s: %s", prefix, exc)
        raise HTTPException(status_code=502, detail="could not check the uploaded file, try again") from exc
    objects = listed.get("Contents", [])
    if not objects:
        raise HTTPException(
            status_code=404,
            detail="the uploaded file is not in S3 yet; upload it before starting the job",
        )
    input_key = objects[0]["Key"]
    message = {
        "job_id": request.job_id,
        "input_bucket": MEDIA_BUCKET,
        "input_key": input_key,
        "output_bucket": MEDIA_BUCKET,
        "output_prefix": f"jobs/{request.job_id}",
        "voice": request.voice,
    }
    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(message, ensure_ascii=False))
    except (ClientError, BotoCoreError) as exc:
        LOG.error("could not enqueue %s: %s", request.job_id, exc)
        raise HTTPException(status_code=502, detail="could not start the job, try again") from exc
    return {"job_id": request.job_id, "state": "queued", "status_key": _status_key(request.job_id)}


def _read_status(job_id: str) -> dict[str, Any]:
    try:
        result = s3.get_object(Bucket=MEDIA_BUCKET, Key=_status_key(job_id))
        payload = json.loads(result["Body"].read())
    except (s3.exceptions.NoSuchKey, s3.exceptions.NoSuchBucket):
        return {"job_id": job_id, "state": "queued"}
    except (ClientError, BotoCoreError) as exc:
        if _client_error(exc) in {"NoSuchKey", "404", "NotFound"}:
            return {"job_id": job_id, "state": "queued"}
        LOG.error("status read failed for %s: %s", job_id, exc)
        raise HTTPException(status_code=502, detail="could not read the job status, try again") from exc
    except (BotoCoreError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOG.error("status read failed for %s: %s", job_id, exc)
        raise HTTPException(status_code=502, detail="could not read the job status, try again") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="the job status file is not valid")
    payload.setdefault("job_id", job_id)
    return payload


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    _settings()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="invalid job id")
    status = _read_status(job_id)
    output_key = str(status.get("output_key") or "")
    if status.get("state") == "completed" and output_key:
        try:
            status["output_url"] = _presigned_download(job_id, output_key)
        except (ClientError, BotoCoreError) as exc:
            LOG.error("could not sign %s: %s", output_key, exc)
    return status


@app.get("/v1/jobs/{job_id}/output")
def get_output(job_id: str) -> dict[str, Any]:
    _settings()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="invalid job id")
    status = _read_status(job_id)
    if status.get("state") != "completed":
        raise HTTPException(status_code=409, detail=f"the job is {status.get('state')}, there is no output yet")
    output_key = str(status.get("output_key") or "")
    if not output_key:
        raise HTTPException(status_code=404, detail="the job finished without a final video")
    try:
        return {"job_id": job_id, "output_key": output_key, "output_url": _presigned_download(job_id, output_key)}
    except (ClientError, BotoCoreError) as exc:
        LOG.error("could not sign %s: %s", output_key, exc)
        raise HTTPException(status_code=502, detail="could not create the download link, try again") from exc
