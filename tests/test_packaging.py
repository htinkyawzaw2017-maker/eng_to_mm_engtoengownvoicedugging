"""Packaging checks: the Dockerfile and .dockerignore that ship the pipeline.

`docker build` needs a daemon, which CI sandboxes often do not have, so these
tests assert the properties of the image definition itself and prove that the
two documented commands point at real entry points.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8")


class DockerfileTests(unittest.TestCase):
    def test_ffmpeg_and_the_openmp_library_are_installed(self) -> None:
        self.assertIn("ffmpeg", DOCKERFILE)
        self.assertIn("libgomp1", DOCKERFILE, "ctranslate2/faster-whisper needs OpenMP")

    def test_the_default_command_is_the_api(self) -> None:
        self.assertIn('CMD ["uvicorn", "cloud.api:app", "--host", "0.0.0.0", "--port", "8000"]', DOCKERFILE)
        self.assertIn("EXPOSE 8000", DOCKERFILE)
        self.assertTrue((ROOT / "cloud" / "api.py").is_file())

    def test_the_worker_command_is_a_real_entry_point(self) -> None:
        self.assertTrue((ROOT / "cloud" / "aws_worker.py").is_file())
        worker = (ROOT / "cloud" / "start-worker.sh").read_text(encoding="utf-8")
        self.assertIn("python cloud/aws_worker.py", worker)

    def test_no_secret_is_baked_into_the_image(self) -> None:
        lowered = DOCKERFILE.lower()
        for forbidden in ("gemini_api_key=", "aws_access_key", "aws_secret", "akia", "copy .env"):
            self.assertNotIn(forbidden, lowered, forbidden)

    def test_no_heavy_model_is_downloaded_at_build_time(self) -> None:
        lowered = DOCKERFILE.lower()
        for forbidden in ("playwright install", "huggingface-cli download", "download_model", "whisper --model"):
            self.assertNotIn(forbidden, lowered, forbidden)

    def test_the_container_does_not_run_as_root(self) -> None:
        self.assertIn("USER recap", DOCKERFILE)

    def test_only_the_runtime_folders_are_copied(self) -> None:
        self.assertNotIn("COPY . .", DOCKERFILE)
        for folder in ("app/", "cloud/", "recap_runtime/", "services/"):
            self.assertIn(f"COPY {folder}", DOCKERFILE)


class DockerIgnoreTests(unittest.TestCase):
    def test_secrets_and_local_state_stay_out_of_the_context(self) -> None:
        for entry in (".env", ".git", ".venv/", "jobs/", "__pycache__/", "config.json", "*.zip"):
            self.assertIn(entry, DOCKERIGNORE.split(), entry)


if __name__ == "__main__":
    unittest.main()
