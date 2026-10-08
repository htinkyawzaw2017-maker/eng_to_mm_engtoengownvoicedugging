"""Web upload page checks: the order of the flow and the absence of secrets.

The page is plain HTML + a small script, so the test reads it as text and
asserts the steps happen in the right order (presign -> S3 -> enqueue -> poll
-> download) and that no credential can reach a browser.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def at(text: str) -> int:
    index = PAGE.find(text)
    assert index >= 0, f"{text!r} is missing from web/index.html"
    return index


class UploadFlowTests(unittest.TestCase):
    def test_the_steps_run_in_the_right_order(self) -> None:
        presign = at("call('/v1/uploads'")
        s3 = at("await uploadToS3(upload, chosen)")
        enqueue = at("call('/v1/jobs',")
        poll = at("call('/v1/jobs/' + encodeURIComponent(upload.job_id))")
        self.assertLess(presign, s3, "the file must be uploaded only after the presign")
        self.assertLess(s3, enqueue, "the job may only start once S3 has the file")
        self.assertLess(enqueue, poll, "status polling comes last")

    def test_the_s3_upload_is_a_multipart_post_with_file_last(self) -> None:
        self.assertIn("request.open('POST', upload.upload_url, true)", PAGE)
        self.assertIn("upload.upload_fields", PAGE)
        form_append = at("for (const [key, value] of Object.entries(upload.upload_fields || {})) form.append(key, value)")
        file_append = at("form.append('file', blob)")
        self.assertLess(form_append, file_append, "S3 requires 'file' to be the last form field")

    def test_the_size_limit_is_checked_in_the_browser_too(self) -> None:
        self.assertIn("upload.max_bytes", PAGE)
        self.assertIn("size_bytes: chosen.size", PAGE)

    def test_both_terminal_states_are_handled(self) -> None:
        self.assertIn("state.state === 'completed'", PAGE)
        self.assertIn("state.state === 'failed'", PAGE)
        self.assertIn("state.error", PAGE)
        self.assertIn("state.stage", PAGE)

    def test_a_finished_job_offers_a_download_link(self) -> None:
        self.assertIn("state.output_url", PAGE)
        self.assertIn("Download the Myanmar video", PAGE)
        self.assertIn("link.href = url", PAGE)

    def test_polling_cannot_run_forever(self) -> None:
        self.assertIn("MAX_POLLS", PAGE)

    def test_the_api_address_is_configurable_without_a_rebuild(self) -> None:
        self.assertIn("window.RECAP_API_BASE", PAGE)
        self.assertIn("params.get('api')", PAGE)
        self.assertIn("localStorage.getItem('recapApiBase')", PAGE)

    def test_no_aws_secret_is_present_in_the_page(self) -> None:
        lowered = PAGE.lower()
        for forbidden in ("akia", "aws_access_key", "aws_secret", "secret_access", "signature", "x-amz-credential"):
            self.assertNotIn(forbidden, lowered, forbidden)

    def test_the_inline_script_is_valid_javascript(self) -> None:
        """Real check: hand the script to node --check when node is available."""
        script = PAGE.split("<script>", 1)[1].split("</script>", 1)[0]
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "page.js"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
