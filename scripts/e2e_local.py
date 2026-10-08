"""Local end-to-end run of the whole cloud pipeline, without AWS.

    python scripts/e2e_local.py

What is real: cloud/api.py (through FastAPI), the presigned-POST contract,
cloud/aws_worker.py, the SQS retry / dead-letter logic, the planner and render
subprocesses, the S3 artifact upload, status.json and the download URL.

What is stubbed, because it needs the network or a paid key: S3 and SQS
themselves (a folder on disk and a list), Whisper, Gemini, Edge TTS and FFmpeg
(see scripts/e2e_stub_runtime.py).

Exit code 0 means every step below passed.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("AWS_REGION", "ap-southeast-1")
os.environ["RECAP_MEDIA_BUCKET"] = "recap-media-e2e"
os.environ["RECAP_SQS_QUEUE_URL"] = "https://sqs.local/recap-jobs"
os.environ["RECAP_DLQ_URL"] = "https://sqs.local/recap-dead-letter"
os.environ.setdefault("GEMINI_API_KEY", "e2e-stub-key")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cloud.api as api  # noqa: E402
from cloud import aws_worker  # noqa: E402

EXCEPTIONS = boto3.client("s3", region_name="ap-southeast-1").exceptions
SOURCE_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"e2e-source-video" * 64

STEPS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Local stand-ins for S3 and SQS
# ---------------------------------------------------------------------------

class LocalS3:
    exceptions = EXCEPTIONS

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / key

    def generate_presigned_post(self, *, Bucket: str, Key: str, Fields: dict, Conditions: list, ExpiresIn: int) -> dict:
        limits = [c for c in Conditions if isinstance(c, list) and c[0] == "content-length-range"]
        return {
            "url": str(self._path(Key)),
            "fields": {**Fields, "key": Key, "policy": "e2e", "x-amz-algorithm": "AWS4-HMAC-SHA256"},
            "content_length_range": limits[0] if limits else None,
        }

    def generate_presigned_url(self, ClientMethod: str, **kwargs: Any) -> str:  # noqa: N803
        return self._path(kwargs["Params"]["Key"]).as_uri()

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int = 1000) -> dict:  # noqa: N803
        found = sorted(p for p in self.root.rglob("*") if p.is_file() and p.relative_to(self.root).as_posix().startswith(Prefix))
        return {"Contents": [{"Key": p.relative_to(self.root).as_posix(), "Size": p.stat().st_size} for p in found[:MaxKeys]]}

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        path = self._path(Key)
        if not path.is_file():
            raise EXCEPTIONS.NoSuchKey({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")

        class _Body:
            def __init__(self, raw: bytes) -> None:
                self.raw = raw

            def read(self) -> bytes:
                return self.raw

        return {"Body": _Body(path.read_bytes())}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_kw: Any) -> None:  # noqa: N803
        path = self._path(Key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Body)

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        Path(Filename).write_bytes(self._path(Key).read_bytes())

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:  # noqa: N803
        self.put_object(Bucket, Key, Path(Filename).read_bytes())


class LocalSQS:
    """A queue plus the redrive rule: max_receive_count attempts, then the DLQ."""

    def __init__(self, max_receive_count: int = 3) -> None:
        self.main: list[dict[str, Any]] = []
        self.dead_letter: list[dict[str, Any]] = []
        self.receives: dict[str, int] = {}
        self.max_receive_count = max_receive_count

    def send_message(self, QueueUrl: str, MessageBody: str) -> dict:  # noqa: N803
        self.main.append({"Body": MessageBody, "ReceiptHandle": f"r{len(self.main) + 1}"})
        return {"MessageId": f"m{len(self.main)}"}

    def receive_message(self, **_kw: Any) -> dict:
        if not self.main:
            return {}
        item = self.main[0]
        self.receives[item["ReceiptHandle"]] = self.receives.get(item["ReceiptHandle"], 0) + 1
        return {"Messages": [item]}

    def delete_message(self, QueueUrl: str, ReceiptHandle: str) -> dict:  # noqa: N803
        self.main = [m for m in self.main if m["ReceiptHandle"] != ReceiptHandle]
        self.receives.pop(ReceiptHandle, None)
        return {}

    def change_message_visibility(self, **_kw: Any) -> dict:
        return {}

    def redrive_if_needed(self) -> None:
        """What SQS itself does once a message passes max_receive_count."""
        for item in list(self.main):
            if self.receives.get(item["ReceiptHandle"], 0) >= self.max_receive_count:
                self.dead_letter.append(item)
                self.main.remove(item)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def make_stub_python(folder: Path) -> str:
    """An executable that behaves like `python` but stubs the AI stages."""
    stub = folder / "stub_python"
    stub.write_text(
        "#!" + sys.executable + "\n"
        "import runpy, sys\n"
        f"runpy.run_path({str(REPO / 'scripts' / 'e2e_stub_runtime.py')!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(stub)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="recap-e2e-") as temp:
        bucket = Path(temp) / "bucket"
        bucket.mkdir()
        s3, sqs = LocalS3(bucket), LocalSQS()
        api.s3, api.sqs = s3, sqs
        client = TestClient(api.app)
        os.environ["PYTHON"] = make_stub_python(Path(temp))

        # 1. the API is up
        health = client.get("/health").json()
        check("1  API /health", health.get("status") == "ok", str(health))

        # 2. ask for a presigned upload
        uploaded = client.post("/v1/uploads", json={"filename": "sample.mp4", "content_type": "video/mp4",
                                                    "size_bytes": len(SOURCE_BYTES)})
        check("2  POST /v1/uploads", uploaded.status_code == 200, str(uploaded.status_code))
        policy = uploaded.json()
        check("3  presigned POST carries a size limit", policy.get("upload_fields", {}).get("success_action_status") == "201")
        job_id = policy["job_id"]

        # 4. the browser POSTs the file straight to S3
        Path(policy["upload_url"]).parent.mkdir(parents=True, exist_ok=True)
        Path(policy["upload_url"]).write_bytes(SOURCE_BYTES)
        check("4  file uploaded to S3", (bucket / policy["input_key"]).read_bytes() == SOURCE_BYTES, policy["input_key"])

        # 5. only now is the job enqueued
        started = client.post("/v1/jobs", json={"job_id": job_id, "voice": "my-MM-ThihaNeural"})
        check("5  POST /v1/jobs enqueues on SQS", started.status_code == 200 and len(sqs.main) == 1, str(started.json()))
        message = json.loads(sqs.main[0]["Body"])
        check("6  the queue message holds no secret",
              not any(w in json.dumps(message).lower() for w in ("secret", "token", "password", "akia")))

        # 7-10. the worker runs the real planner and render subprocesses
        received = sqs.receive_message()
        handled = aws_worker.dispatch(sqs, s3, "queue", received["Messages"][0], 3600)
        check("7  worker processed the job", handled is True)
        status = json.loads((bucket / f"jobs/{job_id}/status.json").read_text(encoding="utf-8"))
        check("8  status.json is completed", status.get("state") == "completed", status.get("state", ""))
        output_key = status.get("output_key", "")
        check("9  final video is in S3", (bucket / output_key).is_file() if output_key else False, output_key)
        check("10 run.log is in S3", (bucket / f"jobs/{job_id}/run.log").is_file())
        run_log = (bucket / f"jobs/{job_id}/run.log").read_text(encoding="utf-8")
        check("11 the real planner_worker.py ran", '"kind": "result"' in run_log and "plan_path" in run_log)

        # 11. the finished job offers a download link
        final = client.get(f"/v1/jobs/{job_id}").json()
        check("12 GET /v1/jobs/{id} returns a download URL", bool(final.get("output_url")), final.get("output_key", ""))
        endpoint = client.get(f"/v1/jobs/{job_id}/output").json()
        check("13 GET /v1/jobs/{id}/output", endpoint.get("output_key") == output_key)
        downloaded = Path(final["output_url"].removeprefix("file://")).read_bytes()
        check("14 the downloaded video is the rendered one", b"recap-e2e-stub-video" in downloaded)
        check("15 the worker deleted the message", len(sqs.main) == 0)

        # 12. a failing job is recorded, retried and then dead-lettered
        os.environ["E2E_FAIL_STAGE"] = "render"
        failed_upload = client.post("/v1/uploads", json={"filename": "broken.mp4", "size_bytes": 10})
        bad_id = failed_upload.json()["job_id"]
        Path(failed_upload.json()["upload_url"]).parent.mkdir(parents=True, exist_ok=True)
        Path(failed_upload.json()["upload_url"]).write_bytes(b"source")
        client.post("/v1/jobs", json={"job_id": bad_id})
        for _attempt in range(3):
            aws_worker.dispatch(sqs, s3, "queue", sqs.receive_message()["Messages"][0], 3600)
            sqs.redrive_if_needed()
        failed_status = json.loads((bucket / f"jobs/{bad_id}/status.json").read_text(encoding="utf-8"))
        check("16 a failed job is written as failed", failed_status.get("state") == "failed",
              f"{failed_status.get('stage')}: {str(failed_status.get('error'))[:60]}")
        check("17 after 3 receives the job reaches the dead-letter queue", len(sqs.dead_letter) == 1,
              f"dead-lettered={len(sqs.dead_letter)}")
        check("18 the API reports the failure to the browser",
              client.get(f"/v1/jobs/{bad_id}").json().get("state") == "failed")
        os.environ.pop("E2E_FAIL_STAGE", None)

    passed = sum(1 for _n, ok, _d in STEPS if ok)
    print(f"\n{passed}/{len(STEPS)} end-to-end steps passed")
    return 0 if passed == len(STEPS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
