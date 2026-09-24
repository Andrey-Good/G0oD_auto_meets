"""Optional real GStreamer test. No meeting, microphone, screen or ASR model involved."""
import os
from pathlib import Path
import shutil
import time
import wave

import pytest

from auto_meets.storage import read_json, status
from auto_meets.worker import start, stop

GST = os.environ.get("AUTO_MEETS_GST") or shutil.which("gst-launch-1.0")


@pytest.mark.skipif(not GST, reason="GStreamer CLI not installed")
def test_detached_capture_stop_and_event_idempotency(profile):
    profile["capture"].update(gst=GST, backend="test", frame_seconds=1)
    profile["asr"]["enabled"] = False
    first = start(profile, "SYNTHETIC INTEGRATION TEST", {}, "integration-event", wait=15)
    folder = Path(first["session"])
    try:
        assert first["phase"] == "recording", first
        second = start(profile, "No duplicate", {}, "integration-event", wait=0)
        assert second["already_exists"]
        assert second["session"] == first["session"]
        time.sleep(2.5)
        stop(folder, wait=30)
        s = status(folder)
        assert not s["capture_alive"], s
        assert not s["worker_alive"], s
        assert s["phase"] == "needs_retry", s  # ASR intentionally disabled, never fake success.
        assert s["audio_seconds"] > 1
        assert s["frames_kept"] >= 1
        with wave.open(str(folder / "audio.wav"), "rb") as f:
            assert f.getnframes() > 16000
        assert read_json(folder / "frames.json")["slides"][0]["at_ms"] >= 0
        assert (folder / "report.html").is_file()
    finally:
        stop(folder, wait=15)
