"""Detached recorder lifecycle. Stop uses a file, not broad process-name termination."""
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import time

from .audio import AudioQueue, audio_size, collect_transcript, new_audio, repair_audio
from .capture import Capture, frame_event
from .frames import FrameSelector
from .platforms import get_backend
from .report import render
from .storage import (identity, lock, metadata, new_session, now, process, read_json,
                      spawned_identity, status, write_json, write_text)


def start(profile: dict, title: str, target: dict, event_key="", wait=20) -> dict:
    root = Path(profile["storage"]["root"])
    with lock(root / ".start.lock"):
        for path in sorted(root.glob("*/session.json")):
            existing = read_json(path)
            if event_key and existing.get("event_key") == event_key:
                return status(path.parent) | {"already_exists": True}
            s = status(path.parent)
            if s["capture_alive"] or (s["worker_alive"] and s["phase"] in ("starting", "recording")):
                raise RuntimeError(f"Another capture is active; inspect/stop {path.parent}")
        get_backend(profile["capture"]["backend"]).preflight(profile, target)
        if target.get("pid"):
            target = target | {"created": identity(target["pid"])["created"]}
        folder = new_session(profile, title, event_key, target)
        options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
        with (folder / "worker.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen([sys.executable, "-m", "auto_meets", "_worker",
                                      "--session", str(folder)], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, close_fds=True, **options)
        write_json(folder / "launch.json", spawned_identity(child.pid))
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        s = status(folder)
        if child.poll() is not None and s["phase"] == "starting":
            write_json(folder / "state.json", {"phase": "failed", "updated_at": now(),
                       "error": "Worker exited before initialization; see worker.log"})
            return status(folder)
        if s["phase"] != "starting":
            return s
        time.sleep(0.2)
    return status(folder)


def _stop_orphan(record, folder: Path):
    p = process(record)
    if p is None:
        return
    # Require both creation time and an argument inside this exact session directory.
    prefix = folder.resolve().as_posix() + "/"
    if not any(prefix in a.replace("\\", "/") for a in p.cmdline()):
        raise RuntimeError("Refusing to stop a process without a matching session argument")
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        # Recheck identity before escalating; do not touch unrelated recycled PIDs.
        p = process(record)
        if p is not None:
            p.kill()
            p.wait(timeout=10)


def stop(folder: Path, wait=0) -> dict:
    s = status(folder)
    write_text(folder / "stop.request", now() + "\n")
    if not s["worker_alive"]:
        _stop_orphan(s.get("capture"), folder)
        _stop_orphan(read_json(folder / "asr-process.json"), folder)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        s = status(folder)
        if not s["worker_alive"]:
            break
        time.sleep(0.25)
    return status(folder)


def _replay_frames(folder: Path, selector: FrameSelector):
    log = folder / "capture.log"
    if log.exists():
        with log.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                event = frame_event(line)
                if event and event[0] > selector.data["last_index"]:
                    path = folder / "spool" / f"frame-{event[0]:06d}.png"
                    if path.exists():
                        selector.observe(*event)


def run(folder: Path, capture=True, asr_settings=None, force=False):
    """Also used by `process`: same finalization, without starting a recorder."""
    m = metadata(folder)
    profile = m["profile"]
    settings = asr_settings or profile["asr"]
    root = Path(profile["storage"]["root"])
    with lock(folder / ".process.lock"):
        old_state = read_json(folder / "state.json", {})
        if not capture:
            if process(old_state.get("capture")) or process(read_json(folder / "asr-process.json")):
                raise RuntimeError("Orphan capture/ASR is still running. Run stop before process")
            if not (folder / "audio.wav").is_file():
                raise ValueError("No recorded audio to process")
        state = old_state | {"phase": "starting" if capture else "processing", "worker": identity(os.getpid()),
                 "capture": None, "error": old_state.get("capture_error"),
                 "capture_error": old_state.get("capture_error"), "asr_errors": 0}
        audio = None
        recorder = None
        selector = FrameSelector(folder, profile["capture"])

        def save():
            state.update(updated_at=now(), audio_seconds=round(audio_size(folder / "audio.wav") / 32000, 2),
                         chunks_completed=audio.index if audio else 0,
                         asr_errors=audio.errors if audio else 0,
                         frames_kept=len(selector.data["slides"]))
            write_json(folder / "state.json", state)

        def request_stop(*_):
            write_text(folder / "stop.request", now() + "\n")

        old_handlers = {}
        if capture:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, request_stop)
        try:
            save()
            previous = read_json(folder / "asr-settings.json")
            if previous and previous["chunk_seconds"] != settings["chunk_seconds"]:
                # Derived caches only; the original audio and old summary stay untouched.
                for path in (folder / "chunks").glob("*.json"):
                    path.unlink()
            write_json(folder / "asr-settings.json", settings)
            audio = AudioQueue(folder, settings, force)
            if capture:
                backend = get_backend(profile["capture"]["backend"])
                with lock(root / ".capture.lock"), backend.awake():
                    backend.preflight(profile, m["target"])
                    new_audio(folder / "audio.wav")
                    started = last_audio = last_frame = last_save = time.monotonic()
                    last_size = 0
                    try:
                        if not (folder / "stop.request").exists():
                            recorder = Capture(profile, m["target"], folder)
                            state["capture"] = recorder.record
                            save()
                        while recorder and not (folder / "stop.request").exists():
                            t = time.monotonic()
                            if recorder.child.poll() is not None:
                                raise RuntimeError(f"GStreamer exited ({recorder.child.returncode}); see capture.log")
                            if t - started >= profile["capture"]["max_hours"] * 3600:
                                state["stop_reason"] = "max_duration"
                                break
                            if shutil.disk_usage(folder).free < profile["capture"]["min_free_mb"] * 1024**2:
                                raise RuntimeError("Low disk space; capture stopped to protect saved material")
                            size = audio_size(folder / "audio.wav")
                            if size > last_size:
                                last_audio, last_size = t, size
                                state["phase"] = "recording"
                            if t - last_audio > profile["capture"]["stall_seconds"]:
                                raise RuntimeError("Audio capture stalled; check capture.log and the device")
                            while True:
                                try:
                                    event = recorder.events.get_nowait()
                                except queue.Empty:
                                    break
                                selector.observe(*event)
                                last_frame = t
                                state["last_frame_ms"] = event[1]
                            state["frame_stale"] = t - last_frame > max(30, 3 * profile["capture"]["frame_seconds"])
                            audio.tick()
                            if t - last_save >= 1:
                                # Revalidate PID/HWND so a closed/replaced window is not silently followed.
                                backend.preflight(profile, m["target"])
                                save()
                                last_save = t
                            time.sleep(0.2)
                        state.setdefault("stop_reason", "requested")
                    except Exception as e:
                        state["capture_error"] = state["error"] = f"{type(e).__name__}: {e}"
                    finally:
                        if recorder:
                            recorder.stop()
                        state["capture_stopped_at"] = now()
            repair_audio(folder / "audio.wav")
            _replay_frames(folder, selector)
            state["phase"] = "processing"
            save()
            while not audio.tick(final=True):
                save()
                time.sleep(0.25)
            collect_transcript(folder)
            if state.get("capture_error") or audio_size(folder / "audio.wav") == 0:
                state["phase"] = "failed"
                state["error"] = state.get("capture_error") or "No audio was captured"
            elif audio.errors:
                state["phase"] = "needs_retry"
                state["error"] = "Some audio chunks were not transcribed; see chunks/*.json and asr.log"
            else:
                state["phase"] = "ready_for_summary"
                state["error"] = None
            save()
            render(folder, strict=False)
        except Exception as e:
            state["phase"] = "failed"
            state["error"] = f"{type(e).__name__}: {e}"
            save()
            raise
        finally:
            if recorder:
                recorder.stop()
            if audio:
                audio.close()
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
    return status(folder)
