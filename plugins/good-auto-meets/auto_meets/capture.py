"""One GStreamer child: continuous PCM audio and timestamped PNGs on one clock."""
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading

from .platforms import get_backend
from .storage import spawned_identity

COMMON_ELEMENTS = ["audiomixer", "audiotestsrc", "audioconvert", "audioresample", "queue",
                   "filesink", "videoconvert", "videorate", "pngenc", "multifilesink"]
CAPS = "audio/x-raw,format=S16LE,rate=16000,channels=1,layout=interleaved"


def prop(key: str, value: str) -> str:
    """Quote for GStreamer's parser too: shell=False alone is not enough here."""
    if any(c in value for c in "\n\r\0"):
        raise ValueError(f"Invalid control character in {key}")
    return key + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def pipeline(profile: dict, target: dict, folder: Path) -> list[str]:
    c = profile["capture"]
    audio, video = get_backend(c["backend"]).sources(profile, target)
    # A live silence source maintains elapsed time even when a browser stops emitting audio.
    # Mixing may clip very loud simultaneous voices; microphone gain can be adjusted in OS.
    args = [c["gst"], "-e", "-m", "--no-position", "--gst-debug-no-color",
            "audiomixer", "name=mix", "force-live=true", "ignore-inactive-pads=true",
            "min-upstream-latency=100000000", "!", CAPS, "!", "filesink",
            prop("location", (folder / "audio.wav").as_posix()),
            "append=true", "buffer-mode=unbuffered", "sync=false"]
    silence = ["audiotestsrc", "is-live=true", "wave=silence"]
    for source in [silence, *audio]:
        args += source + ["!", "audioconvert", "!", "audioresample", "!", CAPS,
                          "!", "queue", "!", "mix."]
    args += video + ["!", "videoconvert", "!", "videorate", "drop-only=true", "!",
                     f"video/x-raw,format=RGB,framerate=1/{c['frame_seconds']}", "!",
                     "pngenc", "!", "multifilesink", "name=frames", "post-messages=true",
                     "sync=false", prop("location", (folder / "spool" / "frame-%06d.png").as_posix())]
    return args


def frame_event(line: str):
    """Use GstMultiFileSink bus PTS, never filename count or file modification times."""
    if "GstMultiFileSink," not in line:
        return None
    index = re.search(r"\bindex=\(int\)(\d+)", line)
    stamp = re.search(r"\brunning-time=\(guint64\)(\d+)", line)
    if index and stamp and int(stamp[1]) < 2**64 - 1:
        return int(index[1]), int(stamp[1]) // 1_000_000
    return None


def doctor(profile: dict) -> dict:
    """Read-only dependency checks; actual Windows capture needs the acceptance test."""
    c = profile["capture"]
    checks = []
    for name, command in [("gstreamer", [c["gst"], "--version"]),
                          ("gst-inspect", [c["inspect"], "--version"])]:
        try:
            r = subprocess.run(command, capture_output=True, text=True, timeout=15,
                               encoding="utf-8", errors="replace")
            version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", r.stdout)
            ok = r.returncode == 0 and version is not None and tuple(
                map(int, version.groups())) >= (1, 24, 0)
            checks.append({"check": name, "ok": ok, "detail": (r.stdout + r.stderr)[-2000:]})
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append({"check": name, "ok": False, "detail": str(e)})
    for name in dict.fromkeys(COMMON_ELEMENTS + get_backend(c["backend"]).ELEMENTS):
        try:
            r = subprocess.run([c["inspect"], name], capture_output=True, timeout=15)
            checks.append({"check": name, "ok": r.returncode == 0})
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append({"check": name, "ok": False, "detail": str(e)})
    if profile["asr"]["enabled"]:
        a = profile["asr"]
        checks.append({"check": "ASR model", "ok": bool(a["model"]) and Path(a["model"]).is_file()})
        try:
            r = subprocess.run([a["executable"], "--help"], capture_output=True,
                               timeout=15, text=True, encoding="utf-8", errors="replace")
            checks.append({"check": "whisper-cli JSON support",
                           "ok": "output-json-full" in r.stdout + r.stderr})
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append({"check": "whisper-cli", "ok": False, "detail": str(e)})
    return {"ok": all(c["ok"] for c in checks), "checks": checks,
            "note": "Does not verify meeting audio, GPU speed, window capture or permissions"}


class Capture:
    def __init__(self, profile: dict, target: dict, folder: Path):
        self.events = queue.Queue()
        self.folder = folder
        env = os.environ | {"GST_DEBUG_NO_COLOR": "1", "LC_ALL": "C"}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        self.child = subprocess.Popen(pipeline(profile, target, folder), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                      errors="replace", env=env, close_fds=True, **options)
        self.record = spawned_identity(self.child.pid)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        with (self.folder / "capture.log").open("a", encoding="utf-8") as log:
            for line in self.child.stdout:
                log.write(line)
                log.flush()
                event = frame_event(line)
                if event:
                    self.events.put(event)
        self.child.stdout.close()

    def stop(self):
        if self.child.poll() is None:
            # Windows detached processes have no console Ctrl-C handler. Raw PCM + a
            # repairable WAV header deliberately avoid needing EOS to finalize a muxer.
            if os.name == "nt":
                self.child.terminate()
            else:
                self.child.send_signal(signal.SIGINT)
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=10)
        self.reader.join(timeout=5)
