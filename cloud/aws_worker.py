"""AWS SQS worker for the recap pipeline.

The desktop UI remains the local entry point. This worker is the first cloud
entry point: it receives one JSON job from SQS, downloads the source video
from S3, runs the existing planner and renderer, then uploads the job
artifacts and status back to S3.

Expected message JSON::

    {"job_id": "job-123", "input_bucket": "...",
     "input_key": "uploads/job-123/source.mp4",
     "output_prefix": "jobs/job-123", "voice": "my-MM-ThihaNeural"}

AWS credentials are read from the normal boto3 credential chain (IAM role on
ECS/EC2, or the environment of a developer machine). No keys are accepted in
the queue message and none are written into this file.

Failure handling: any exception leaves the SQS message on the queue, so the
redrive policy on the queue (maxReceiveCount = 3, see infra/terraform) moves
a job that keeps failing to the dead-letter queue.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("recap.aws_worker")
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FINAL_VIDEO_NAME = "final_edge_tts_smart_sync.mp4"
DEFAULT_VOICE = "my-MM-ThihaNeural"
HEARTBEAT_SECONDS = 60


class InvalidJob(ValueError):
    pass


class VisibilityHeartbeat:
    """Keep a long job invisible so SQS does not hand it to a second worker.

    The queue visibility timeout is the *minimum* processing budget. Whisper
    plus a full render can outlast it, so the timeout is extended every
    ``HEARTBEAT_SECONDS`` while the job runs.
    """

    def __init__(self, sqs: Any, queue_url: str, receipt_handle: str, timeout: int) -> None:
        self.sqs = sqs
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.timeout = max(60, int(timeout))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latest_handle = receipt_handle

    def __enter__(self) -> "VisibilityHeartbeat":
        self._thread = threading.Thread(target=self._run, name="sqs-visibility", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_SECONDS):
            try:
                response = self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.latest_handle,
                    VisibilityTimeout=self.timeout,
                )
                self.latest_handle = response.get("ReceiptHandle", self.latest_handle)
            except (ClientError, BotoCoreError) as exc:
                LOG.warning("could not extend visibility timeout: %s", exc)
                return


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
    for path in sorted(root.rglob("*")):
        if path.is_file():
            key = f"{prefix.rstrip('/')}/{path.relative_to(root).as_posix()}"
            s3.upload_file(str(path), bucket, key)


def _run(command: list[str], cwd: Path, env: dict[str, str], log_path: Path, stage: str) -> None:
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
        tail = _log_tail(log_path)
        raise RuntimeError(f"{stage} failed ({process.returncode}); see run.log: {tail}")


def _log_tail(log_path: Path, lines: int = 6) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return " | ".join(text[-lines:])[-800:]


def _find_final_video(job_dir: Path) -> Path | None:
    direct = job_dir / "edge_tts_smart_sync" / FINAL_VIDEO_NAME
    if direct.is_file():
        return direct
    for path in sorted(job_dir.rglob(FINAL_VIDEO_NAME)):
        if path.is_file():
            return path
    return None


def process_job(message: dict[str, Any], *, s3: Any | None = None, runner: Callable[..., None] | None = None) -> dict[str, Any]:
    """Process one S3-backed job and return its completed status.

    The function is intentionally independent of the SQS polling loop so it
    can be tested with a fake S3 client and called by another worker runtime.
    """
    job_id = _required(message, "job_id")
    input_bucket = _required(message, "input_bucket")
    input_key = _required(message, "input_key")
    output_bucket = str(message.get("output_bucket") or input_bucket)
    output_prefix = str(message.get("output_prefix") or f"jobs/{job_id}").strip("/")
    if not output_prefix.startswith("jobs/"):
        raise InvalidJob("output_prefix must stay inside jobs/")
    voice = str(message.get("voice") or DEFAULT_VOICE)
    s3 = s3 or boto3.client("s3")

    with tempfile.TemporaryDirectory(prefix=f"recap-{job_id}-") as temp:
        work = Path(temp)
        source = work / Path(input_key).name
        job_dir = work / "job"
        job_dir.mkdir()
        log_path = work / "run.log"
        status: dict[str, Any] = {"job_id": job_id, "state": "running", "started_at": int(time.time())}
        _status(s3, output_bucket, output_prefix, status)
        stage = "download"
        try:
            s3.download_file(input_bucket, input_key, str(source))
            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join([str(APP), env.get("PYTHONPATH", "")])
            env_file = work / "runtime.env"
            api_key = os.environ.get("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is required in the worker environment")
            # The key lives only in this temporary file, which is deleted with
            # the temp directory. It is never part of the SQS message.
            env_file.write_text(f"GEMINI_API_KEY={api_key}\n", encoding="utf-8")
            env_file.chmod(0o600)
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
            stage = "planner"
            (runner or _run)(
                [os.environ.get("PYTHON", "python"), str(APP / "planner_worker.py"), str(planner_json)],
                ROOT, env, log_path, stage,
            )

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
            stage = "render"
            (runner or _run)(
                [os.environ.get("PYTHON", "python"), str(APP / "render_worker.py"), str(render_json)],
                ROOT, env, log_path, stage,
            )

            final_video = _find_final_video(job_dir)
            if final_video is None:
                raise RuntimeError("the renderer finished but produced no final video")
            stage = "upload"
            _upload_tree(s3, output_bucket, output_prefix, job_dir)
            s3.upload_file(str(log_path), output_bucket, f"{output_prefix}/run.log")
            output_key = f"{output_prefix}/{final_video.relative_to(job_dir).as_posix()}"
            result = {
                **status,
                "state": "completed",
                "finished_at": int(time.time()),
                "output_prefix": output_prefix,
                "output_key": output_key,
                "voice": voice,
            }
            _status(s3, output_bucket, output_prefix, result)
            LOG.info("job %s completed -> s3://%s/%s", job_id, output_bucket, output_key)
            return result
        except (ClientError, BotoCoreError) as exc:
            LOG.exception("job %s failed during %s", job_id, stage)
            _fail(s3, output_bucket, output_prefix, status, stage, f"AWS error during {stage}")
            raise
        except Exception as exc:
            LOG.exception("job %s failed during %s", job_id, stage)
            _fail(s3, output_bucket, output_prefix, status, stage, str(exc))
            raise


def _fail(s3: Any, bucket: str, prefix: str, status: dict[str, Any], stage: str, error: str) -> None:
    failure = {
        **status,
        "state": "failed",
        "finished_at": int(time.time()),
        "stage": stage,
        "error": error[:1000],
    }
    try:
        _status(s3, bucket, prefix, failure)
    except (ClientError, BotoCoreError) as exc:  # status write must not mask the real error
        LOG.error("could not write the failed status for %s: %s", prefix, exc)


def _handle_message(sqs: Any, s3: Any, queue_url: str, item: dict[str, str], timeout: int) -> bool:
    """Return True when the message was processed and can be deleted."""
    receipt = item["ReceiptHandle"]
    try:
        message = json.loads(item["Body"])
    except json.JSONDecodeError:
        LOG.error("dropping a message that is not valid JSON")
        return True  # a malformed body will never succeed; let it reach the DLQ on its own path
    if not isinstance(message, dict):
        LOG.error("dropping a message that is not a JSON object")
        return True
    try:
        with VisibilityHeartbeat(sqs, queue_url, receipt, timeout):
            process_job(message, s3=s3)
        return True
    except InvalidJob:
        LOG.exception("the job message is invalid; it will be retried and then dead-lettered")
    except Exception:
        LOG.exception("job failed; the message stays on the queue for retry")
    return False


def _aws_region() -> str:
    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("RECAP_AWS_REGION")
        or ""
    ).strip()
    if not region:
        raise SystemExit("AWS_REGION (or AWS_DEFAULT_REGION) must be set for the worker")
    return region


def dispatch(sqs: Any, s3: Any, queue_url: str, item: dict[str, str], timeout: int) -> bool:
    """Handle one received message and delete it only when the job succeeded.

    This is the whole retry contract in one place: returning False leaves the
    message on the queue, so SQS redelivers it and the redrive policy on the
    queue moves it to the dead-letter queue after ``maxReceiveCount`` tries.
    """
    if not _handle_message(sqs, s3, queue_url, item, timeout):
        return False
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=item["ReceiptHandle"])
    return True


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    queue_url = os.environ.get("RECAP_SQS_QUEUE_URL", "").strip()
    if not queue_url:
        raise SystemExit("RECAP_SQS_QUEUE_URL is required")
    region = _aws_region()
    timeout = int(os.environ.get("RECAP_VISIBILITY_TIMEOUT", "3600"))
    sqs = boto3.client("sqs", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    LOG.info("waiting for recap jobs on %s", queue_url)
    while True:
        try:
            response = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                VisibilityTimeout=timeout,
            )
        except (ClientError, BotoCoreError) as exc:
            LOG.error("receive_message failed, retrying in 5s: %s", exc)
            time.sleep(5)
            continue
        for item in response.get("Messages", []):
            dispatch(sqs, s3, queue_url, item, timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
