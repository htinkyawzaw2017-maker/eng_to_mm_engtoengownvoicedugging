"""AWS SQS worker for the recap pipeline.

The desktop UI remains the local entry point. This worker is the first cloud
entry point: it receives one JSON job from SQS, downloads the source video
from S3, runs the existing planner and renderer, then uploads the job
artifacts and status back to S3.

Expected message JSON::

    {"job_id": "job-123", "input_bucket": "...",
     "input_key": "uploads/job-123/source.mp4",
     "output_prefix": "jobs/job-123", "voice": "my-MM-ThihaNeural"}

AWS credentials are read from the normal boto3 credential chain. No keys are
accepted in the queue message.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

LOG = logging.getLogger("recap.aws_worker")
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"


class InvalidJob(ValueError):
    pass


def _required(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise InvalidJob(f"missing job field: {key}")
    return value


def _status(s3: Any, bucket: str, prefix: str, payload: dict[str, Any]) -> None:
    key = f"{prefix.rstrip('/')}/status.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")


def _upload_tree(s3: Any, bucket: str, prefix: str, root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            key = f"{prefix.rstrip('/')}/{path.relative_to(root).as_posix()}"
            s3.upload_file(str(path), bucket, key)


def _run(command: list[str], cwd: Path, env: dict[str, str], log_path: Path) -> None:
    LOG.info("running %s", " ".join(command))
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if process.returncode != 0:
        raise RuntimeError(f"pipeline command failed ({process.returncode}); see {log_path.name}")


def process_job(message: dict[str, Any], *, s3: Any | None = None) -> dict[str, Any]:
    """Process one S3-backed job and return its completed status.

    The function is intentionally independent of the SQS polling loop so it
    can be tested with a fake S3 client and called by another worker runtime.
    """
    job_id = _required(message, "job_id")
    input_bucket = _required(message, "input_bucket")
    input_key = _required(message, "input_key")
    output_bucket = str(message.get("output_bucket") or input_bucket)
    output_prefix = str(message.get("output_prefix") or f"jobs/{job_id}").strip("/")
    voice = str(message.get("voice") or "my-MM-ThihaNeural")
    s3 = s3 or boto3.client("s3")

    with tempfile.TemporaryDirectory(prefix=f"recap-{job_id}-") as temp:
        work = Path(temp)
        source = work / Path(input_key).name
        job_dir = work / "job"
        job_dir.mkdir()
        status = {"job_id": job_id, "state": "running", "started_at": int(time.time())}
        _status(s3, output_bucket, output_prefix, status)
        try:
            s3.download_file(input_bucket, input_key, str(source))
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join([str(APP), env.get("PYTHONPATH", "")])
            env_file = work / "runtime.env"
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is required in the worker environment")
            env_file.write_text(f"GEMINI_API_KEY={api_key}\n", encoding="utf-8")
            planner_config = {
                "stop_file": str(work / "stop"),
                "runtime_root": str(ROOT / "recap_runtime"),
                "gemini_env_path": str(env_file),
                "ffprobe_path": shutil.which("ffprobe") or "ffprobe",
                "source_path": str(source),
                "job_dir": str(job_dir),
                "source_title": Path(input_key).stem,
                "reuse": True,
            }
            planner_json = work / "planner.json"
            planner_json.write_text(json.dumps(planner_config), encoding="utf-8")
            log_path = work / "run.log"
            _run([os.environ.get("PYTHON", "python"), str(APP / "planner_worker.py"), str(planner_json)], ROOT, env, log_path)

            plan_path = job_dir / "smart_sync_plan.json"
            render_config = {
                "stop_file": str(work / "stop"),
                "job_dir": str(job_dir),
                "source_path": str(source),
                "plan_path": str(plan_path),
                "voice": voice,
                "ffmpeg_path": shutil.which("ffmpeg") or "ffmpeg",
                "ffprobe_path": shutil.which("ffprobe") or "ffprobe",
                "render_final_video": True,
                "mirror_video": False,
            }
            render_json = work / "render.json"
            render_json.write_text(json.dumps(render_config), encoding="utf-8")
            _run([os.environ.get("PYTHON", "python"), str(APP / "render_worker.py"), str(render_json)], ROOT, env, log_path)
            _upload_tree(s3, output_bucket, output_prefix, job_dir)
            s3.upload_file(str(log_path), output_bucket, f"{output_prefix}/run.log")
            result = {**status, "state": "completed", "finished_at": int(time.time()), "output_prefix": output_prefix}
            _status(s3, output_bucket, output_prefix, result)
            return result
        except Exception as exc:
            LOG.exception("job %s failed", job_id)
            failure = {**status, "state": "failed", "finished_at": int(time.time()), "error": str(exc)}
            _status(s3, output_bucket, output_prefix, failure)
            raise


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    queue_url = os.environ.get("RECAP_SQS_QUEUE_URL", "").strip()
    if not queue_url:
        raise SystemExit("RECAP_SQS_QUEUE_URL is required")
    sqs = boto3.client("sqs")
    s3 = boto3.client("s3")
    LOG.info("waiting for recap jobs")
    while True:
        response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20, VisibilityTimeout=3600)
        for item in response.get("Messages", []):
            try:
                process_job(json.loads(item["Body"]), s3=s3)
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=item["ReceiptHandle"])
            except (InvalidJob, ClientError, RuntimeError, json.JSONDecodeError):
                LOG.exception("job failed; message remains available for retry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
