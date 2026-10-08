"""Cloud API contract tests: presigned upload policy, enqueue, status, download.

Everything AWS-facing is replaced by a fake client, so no credentials, no
network and no cost are involved.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("AWS_REGION", "ap-southeast-1")
os.environ.setdefault("RECAP_MEDIA_BUCKET", "recap-media-test")
os.environ.setdefault("RECAP_SQS_QUEUE_URL", "https://sqs.ap-southeast-1.amazonaws.com/123456789012/recap-jobs")

import boto3  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cloud.api as api  # noqa: E402

_EXCEPTIONS = boto3.client("s3", region_name="ap-southeast-1").exceptions


class FakeS3:
    """Just enough of the S3 surface used by cloud/api.py."""

    exceptions = _EXCEPTIONS

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.presigned_posts: list[dict[str, Any]] = []
        self.presigned_gets: list[dict[str, Any]] = []
        self.fail_list = False

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.presigned_posts.append(kwargs)
        return {"url": "https://recap-media-test.s3.amazonaws.com/", "fields": {"key": kwargs["Key"], "policy": "x"}}

    def generate_presigned_url(self, ClientMethod: str, **kwargs: Any) -> str:  # noqa: N803
        self.presigned_gets.append({"method": ClientMethod, **kwargs})
        return "https://recap-media-test.s3.amazonaws.com/signed.mp4"

    def list_objects_v2(self, Bucket: str, Prefix: str, MaxKeys: int = 1000) -> dict[str, Any]:  # noqa: N803
        if self.fail_list:
            raise _EXCEPTIONS.ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")
        keys = [key for key in sorted(self.objects) if key.startswith(Prefix)][:MaxKeys]
        return {"Contents": [{"Key": key, "Size": len(self.objects[key])} for key in keys]}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _EXCEPTIONS.NoSuchKey({"Error": {"Code": "NoSuchKey", "Message": "no"}}, "GetObject")

        class _Body:
            def __init__(self, raw: bytes) -> None:
                self.raw = raw

            def read(self) -> bytes:
                return self.raw

        return {"Body": _Body(self.objects[Key])}


class FakeSQS:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_message(self, QueueUrl: str, MessageBody: str) -> dict[str, str]:  # noqa: N803
        self.sent.append({"QueueUrl": QueueUrl, "Body": json.loads(MessageBody)})
        return {"MessageId": "m-1"}


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s3 = FakeS3()
        self.sqs = FakeSQS()
        api.s3, api.sqs = self.s3, self.sqs
        api.MEDIA_BUCKET = "recap-media-test"
        api.QUEUE_URL = "https://sqs.ap-southeast-1.amazonaws.com/123456789012/recap-jobs"
        self.client = TestClient(api.app, raise_server_exceptions=False)

    def test_health_is_open_without_aws(self) -> None:
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["bucket_configured"], "True")

    def test_upload_returns_a_size_limited_presigned_post(self) -> None:
        response = self.client.post("/v1/uploads", json={"filename": "movie.mp4", "content_type": "video/mp4"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["job_id"]), 32)
        self.assertTrue(body["input_key"].startswith("uploads/"))
        self.assertIn("upload_fields", body)
        policy = self.s3.presigned_posts[0]
        self.assertIn(["content-length-range", 1, api.MAX_UPLOAD_BYTES], policy["Conditions"])

    def test_upload_rejects_an_unsupported_extension(self) -> None:
        response = self.client.post("/v1/uploads", json={"filename": "evil.exe"})
        self.assertEqual(response.status_code, 415)
        self.assertIn("unsupported file type", response.json()["detail"])

    def test_upload_rejects_a_path_traversal_filename(self) -> None:
        response = self.client.post("/v1/uploads", json={"filename": "../../etc/passwd.mp4"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("..", response.json()["input_key"])

    def test_upload_rejects_an_oversized_file_before_signing(self) -> None:
        response = self.client.post("/v1/uploads", json={"filename": "big.mp4", "size_bytes": api.MAX_UPLOAD_BYTES + 1})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.s3.presigned_posts, [])

    def test_enqueue_says_the_file_is_missing_instead_of_500(self) -> None:
        response = self.client.post("/v1/jobs", json={"job_id": "a" * 32})
        self.assertEqual(response.status_code, 404)
        self.assertIn("not in S3 yet", response.json()["detail"])

    def test_enqueue_sends_only_paths_and_a_voice_to_sqs(self) -> None:
        job_id = "b" * 32
        self.s3.objects[f"uploads/{job_id}/movie.mp4"] = b"video"
        response = self.client.post("/v1/jobs", json={"job_id": job_id, "voice": "my-MM-NilarNeural"})
        self.assertEqual(response.status_code, 200, response.text)
        message = self.sqs.sent[0]["Body"]
        self.assertEqual(message["job_id"], job_id)
        self.assertEqual(message["input_key"], f"uploads/{job_id}/movie.mp4")
        self.assertEqual(message["output_prefix"], f"jobs/{job_id}")
        self.assertEqual(sorted(message), ["input_bucket", "input_key", "job_id", "output_bucket", "output_prefix", "voice"])
        # no credential may travel in the queue message
        blob = json.dumps(message).lower()
        for word in ("aws_access_key", "secret", "token", "password", "akia"):
            self.assertNotIn(word, blob)

    def test_enqueue_refuses_a_job_id_that_could_leave_the_uploads_prefix(self) -> None:
        response = self.client.post("/v1/jobs", json={"job_id": "aaaa/../../../secret"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.sqs.sent, [])

    def test_status_is_queued_while_no_status_file_exists(self) -> None:
        body = self.client.get("/v1/jobs/" + "c" * 32).json()
        self.assertEqual(body["state"], "queued")

    def test_completed_status_carries_a_download_link(self) -> None:
        job_id = "d" * 32
        self.s3.objects[f"jobs/{job_id}/status.json"] = json.dumps(
            {"job_id": job_id, "state": "completed", "output_key": f"jobs/{job_id}/edge_tts_smart_sync/final.mp4"}
        ).encode()
        body = self.client.get(f"/v1/jobs/{job_id}").json()
        self.assertEqual(body["state"], "completed")
        self.assertIn("output_url", body)
        self.assertEqual(self.s3.presigned_gets[0]["method"], "get_object")

    def test_output_endpoint_waits_for_a_completed_job(self) -> None:
        response = self.client.get("/v1/jobs/" + "e" * 32 + "/output")
        self.assertEqual(response.status_code, 409)

    def test_invalid_job_id_is_rejected_before_touching_s3(self) -> None:
        self.assertEqual(self.client.get("/v1/jobs/short").status_code, 400)
        self.assertEqual(self.client.get("/v1/jobs/" + "f" * 81).status_code, 400)

    def test_an_s3_outage_is_reported_as_a_friendly_502(self) -> None:
        self.s3.fail_list = True
        response = self.client.post("/v1/jobs", json={"job_id": "g" * 32})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("AccessDenied", response.json()["detail"])

    def test_a_missing_configuration_is_503_not_a_crash(self) -> None:
        api.MEDIA_BUCKET = ""
        response = self.client.post("/v1/uploads", json={"filename": "movie.mp4"})
        self.assertEqual(response.status_code, 503)
        api.MEDIA_BUCKET = "recap-media-test"


class NoHardcodedCredentialsTests(unittest.TestCase):
    def test_no_aws_key_is_written_in_the_source(self) -> None:
        for name in ("cloud/api.py", "cloud/aws_worker.py", "web/index.html"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("AKIA", text, name)
            self.assertNotIn("aws_access_key_id", text, name)
            self.assertNotIn("aws_secret_access_key", text, name)


if __name__ == "__main__":
    unittest.main()
