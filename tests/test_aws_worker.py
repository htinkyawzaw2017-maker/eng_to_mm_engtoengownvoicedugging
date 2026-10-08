"""Cloud worker tests: S3 download, planner + render, upload, status, retry.

The planner and renderer are replaced by a fake runner and S3 by an in-memory
client, so the whole job lifecycle is exercised without AWS, Whisper, Gemini
or FFmpeg.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("AWS_REGION", "ap-southeast-1")

from cloud import aws_worker  # noqa: E402


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploaded: list[str] = []

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_kw: Any) -> None:  # noqa: N803
        self.objects[Key] = Body

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:  # noqa: N803
        Path(Filename).write_bytes(self.objects[Key])

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:  # noqa: N803
        self.uploaded.append(Key)
        self.objects[Key] = Path(Filename).read_bytes()

    def status(self, key: str) -> dict[str, Any]:
        return json.loads(self.objects[key])


def make_message(job_id: str = "job-1") -> dict[str, Any]:
    return {
        "job_id": job_id,
        "input_bucket": "recap-media-test",
        "input_key": f"uploads/{job_id}/movie.mp4",
        "output_bucket": "recap-media-test",
        "output_prefix": f"jobs/{job_id}",
        "voice": "my-MM-ThihaNeural",
    }


def fake_runner_factory(stages: list[str], fail_on: str | None = None):
    """Stand in for the planner/renderer: writes the artifacts they produce."""

    def runner(command: list[str], cwd: Path, env: dict[str, str], log_path: Path, stage: str) -> None:
        stages.append(stage)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{stage} ran\n")
        if fail_on == stage:
            raise RuntimeError(f"{stage} exploded")
        config = json.loads(Path(command[2]).read_text(encoding="utf-8"))
        job_dir = Path(config["job_dir"])
        if stage == "planner":
            (job_dir / "smart_sync_plan.json").write_text(json.dumps({"segments": []}), encoding="utf-8")
        else:
            out = job_dir / "edge_tts_smart_sync"
            out.mkdir(parents=True, exist_ok=True)
            (out / aws_worker.FINAL_VIDEO_NAME).write_bytes(b"mp4-bytes")

    return runner


class ProcessJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s3 = FakeS3()
        self.s3.objects["uploads/job-1/movie.mp4"] = b"source-video"
        os.environ["GEMINI_API_KEY"] = "test-key-not-a-real-one"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)

    def test_a_job_downloads_runs_both_stages_and_completes(self) -> None:
        stages: list[str] = []
        result = aws_worker.process_job(make_message(), s3=self.s3, runner=fake_runner_factory(stages))
        self.assertEqual(stages, ["planner", "render"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["output_key"], "jobs/job-1/edge_tts_smart_sync/final_edge_tts_smart_sync.mp4")
        self.assertEqual(self.s3.status("jobs/job-1/status.json")["state"], "completed")
        self.assertIn("jobs/job-1/run.log", self.s3.uploaded)
        self.assertIn("jobs/job-1/edge_tts_smart_sync/final_edge_tts_smart_sync.mp4", self.s3.uploaded)

    def test_a_broken_render_is_recorded_as_failed_and_raised_again(self) -> None:
        stages: list[str] = []
        with self.assertRaises(RuntimeError):
            aws_worker.process_job(make_message(), s3=self.s3, runner=fake_runner_factory(stages, fail_on="render"))
        status = self.s3.status("jobs/job-1/status.json")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["stage"], "render")
        self.assertIn("exploded", status["error"])

    def test_a_renderer_that_produces_no_video_fails_loudly(self) -> None:
        def runner(command: list[str], cwd: Path, env: dict[str, str], log_path: Path, stage: str) -> None:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{stage}\n")
            if stage == "planner":
                config = json.loads(Path(command[2]).read_text(encoding="utf-8"))
                (Path(config["job_dir"]) / "smart_sync_plan.json").write_text("{}", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            aws_worker.process_job(make_message(), s3=self.s3, runner=runner)
        self.assertEqual(self.s3.status("jobs/job-1/status.json")["state"], "failed")

    def test_a_message_without_a_job_id_is_invalid(self) -> None:
        with self.assertRaises(aws_worker.InvalidJob):
            aws_worker.process_job({"input_bucket": "b", "input_key": "k"}, s3=self.s3)

    def test_output_prefix_may_not_leave_jobs(self) -> None:
        message = {**make_message(), "output_prefix": "../other"}
        with self.assertRaises(aws_worker.InvalidJob):
            aws_worker.process_job(message, s3=self.s3)

    def test_the_worker_refuses_to_run_without_a_gemini_key(self) -> None:
        os.environ.pop("GEMINI_API_KEY", None)
        with self.assertRaises(RuntimeError):
            aws_worker.process_job(make_message(), s3=self.s3, runner=fake_runner_factory([]))
        self.assertEqual(self.s3.status("jobs/job-1/status.json")["state"], "failed")


class RetryTests(unittest.TestCase):
    """SQS retry = the message is left on the queue; the DLQ redrive does the rest."""

    def setUp(self) -> None:
        self.s3 = FakeS3()
        self.s3.objects["uploads/job-1/movie.mp4"] = b"source-video"
        os.environ["GEMINI_API_KEY"] = "test-key-not-a-real-one"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)

    def _handle(self, message: dict[str, Any] | str, runner: Any) -> bool:
        item = {"Body": message if isinstance(message, str) else json.dumps(message), "ReceiptHandle": "r-1"}
        with mock.patch.object(aws_worker, "_run", runner), \
             mock.patch.object(aws_worker, "VisibilityHeartbeat", _NoopHeartbeat):
            return aws_worker._handle_message(object(), self.s3, "queue", item, 60)

    def test_a_successful_job_is_deleted_from_the_queue(self) -> None:
        stages: list[str] = []
        self.assertTrue(self._handle(make_message(), fake_runner_factory(stages)))
        self.assertEqual(stages, ["planner", "render"])
        self.assertEqual(self.s3.status("jobs/job-1/status.json")["state"], "completed")

    def test_a_failed_job_is_left_on_the_queue_for_retry(self) -> None:
        stages: list[str] = []
        self.assertFalse(self._handle(make_message(), fake_runner_factory(stages, fail_on="planner")))
        self.assertEqual(self.s3.status("jobs/job-1/status.json")["state"], "failed")

    def test_a_message_that_is_not_json_is_dropped_not_retried_forever(self) -> None:
        self.assertTrue(self._handle("not json", fake_runner_factory([])))


class _NoopHeartbeat:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_NoopHeartbeat":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

if __name__ == "__main__":
    unittest.main()
