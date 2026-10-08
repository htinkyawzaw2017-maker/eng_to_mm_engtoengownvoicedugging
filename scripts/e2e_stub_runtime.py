"""Runs the REAL app/planner_worker.py and app/render_worker.py with only the
outside world replaced.

``cloud/aws_worker.py`` starts those two scripts as subprocesses. This file is
used as the ``PYTHON`` of that subprocess during the local end-to-end test, so
the worker scripts themselves - their argument handling, config loading,
``@@VRS_EVENT@@`` output and exit codes - really run. Only the classes that
need Whisper/Gemini/Edge TTS/FFmpeg are swapped:

    recap_planner.RecapPlanBuilder -> a builder that writes a one-line plan
    core.EdgeSmartSync             -> a renderer that writes a stub .mp4

Set ``E2E_FAIL_STAGE=render`` to make the render step fail, which is how the
test proves a failed job is recorded and retried.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "app"))

FAIL_STAGE = os.environ.get("E2E_FAIL_STAGE", "")


def _run_planner(config_path: Path) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))

    class StubPlanBuilder:
        """Stands in for Whisper + Gemini. Same call signature as the real one."""

        def __init__(self, *, log=None, **_kwargs):
            self._log = log or (lambda _message: None)

        def build(self, source, job_dir, source_title="", reuse=True):
            job_dir = Path(job_dir)
            job_dir.mkdir(parents=True, exist_ok=True)
            self._log(f"stub plan for {source}")
            plan = {
                "source_title": source_title,
                "segments": [{"video_start": 0.0, "video_end": 5.0, "narration": "စမ်းသပ် စာကြောင်း။"}],
            }
            path = job_dir / "smart_sync_plan.json"
            path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            return path, plan

    import recap_planner

    recap_planner.RecapPlanBuilder = StubPlanBuilder
    if FAIL_STAGE == "planner":
        raise RuntimeError("the stub planner was asked to fail")

    import planner_worker

    sys.argv = [str(REPO / "app" / "planner_worker.py"), str(config_path)]
    return planner_worker.main()


def _run_render(config_path: Path) -> int:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))

    class StubSmartSync:
        """Stands in for Edge TTS + FFmpeg."""

        def __init__(self, *, job_dir, log=None, **_kwargs):
            self.job_dir = Path(job_dir)
            self._log = log or (lambda _message: None)
            self.voice_volume_percent = 100
            self.short_pauses = False

        def run(self):
            if FAIL_STAGE == "render":
                raise RuntimeError("the stub renderer was asked to fail")
            out = self.job_dir / "edge_tts_smart_sync"
            out.mkdir(parents=True, exist_ok=True)
            final = out / "final_edge_tts_smart_sync.mp4"
            final.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"recap-e2e-stub-video")
            self._log(f"stub final video at {final}")
            return {"final_video": str(final)}

    import core

    core.EdgeSmartSync = StubSmartSync

    import render_worker

    sys.argv = [str(REPO / "app" / "render_worker.py"), str(config_path)]
    return render_worker.main()


def main() -> int:
    target = Path(sys.argv[1]).name
    config_path = Path(sys.argv[2]).resolve()
    if target == "planner_worker.py":
        return _run_planner(config_path)
    if target == "render_worker.py":
        return _run_render(config_path)
    print(f"e2e_stub_runtime: unknown worker {target}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
