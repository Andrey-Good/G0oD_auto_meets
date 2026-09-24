import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

from PIL import Image
import pytest

from auto_meets import audio
from auto_meets.capture import frame_event, pipeline, prop
from auto_meets.cli import demo, main
from auto_meets.config import load_profile, validate
from auto_meets.frames import FrameSelector
from auto_meets.report import render, validate_summary
from auto_meets.storage import identity, lock, process, read_json, write_json
from auto_meets.worker import run


@pytest.mark.parametrize("patch", [
    {"typo": {}}, {"asr": {"thread": 4}}, {"capture": {"microphone": "false"}},
    {"asr": {"chunk_seconds": 0}}, {"asr": {"threads": True}},
    {"asr": {"threads": 1.5}}, {"capture": {"change_threshold": float("nan")}},
    {"capture": {"crop": [1, 2, 3]}}, {"capture": {"crop": [0, 0, 2, 0]}},
    {"capture": {"crop": [False, 0, 0, 0]}}, {"schedule": {"include": [42]}},
    {"report": {"kind": "other"}}, {"capture": {"backend": "../../evil"}},
])
def test_invalid_profile(patch):
    with pytest.raises(ValueError):
        validate(patch)


def test_profile_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    folder = tmp_path / "profiles"
    folder.mkdir()
    path = folder / "a.toml"
    path.write_text('[asr]\nmodel="../models/test.bin"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    p = load_profile(path)
    assert p["asr"]["model"] == str(tmp_path / "models" / "test.bin")
    assert p["storage"]["root"] == str(tmp_path / "data")


def test_gst_quote_and_windows_sources(profile, tmp_path):
    p = copy.deepcopy(profile)
    p["capture"].update(backend="windows", microphone=True, microphone_device='a" ! filesink')
    command = pipeline(p, {"pid": 42, "window": 2**40}, tmp_path / "with spaces")
    assert "loopback-target-pid=42" in command
    assert "loopback-mode=include-process-tree" in command
    assert f"window-handle={2**40}" in command
    assert "capture-api=wgc" in command
    assert "append=true" in command
    assert 'device="a\\" ! filesink"' in command
    assert "shell" not in command
    with pytest.raises(ValueError):
        prop("location", "path\n! evil")


@pytest.mark.parametrize("line,expected", [
    ('Got message #1: GstMultiFileSink, index=(int)12, timestamp=(guint64)1, running-time=(guint64)2300000000;', (12, 2300)),
    ('GstMultiFileSink, index=(int)0, running-time=(guint64)0;', (0, 0)),
    ('GstMultiFileSink, index=(int)0, running-time=(guint64)18446744073709551615;', None),
    ('GstMessageStateChanged, index=(int)1;', None),
])
def test_frame_bus_timestamps(line, expected):
    assert frame_event(line) == expected


def test_wav_repair_keeps_samples(tmp_path):
    path = tmp_path / "audio.wav"
    audio.new_audio(path)
    payload = b"\x02\x01" * 1000
    with path.open("ab") as f:
        f.write(payload + b"\0")  # Simulate a torn final sample.
    audio.repair_audio(path)
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() == 1000
        assert wav.getframerate() == 16000
        assert wav.readframes(1000) == payload
    with pytest.raises(FileExistsError):
        audio.new_audio(path)


def fake_whisper(monkeypatch, calls, mode="ok"):
    class Child:
        pid = os.getpid()
        returncode = None

        def __init__(self, args, **kwargs):
            calls.append(args)
            with wave.open(args[args.index("-f") + 1], "rb") as wav:
                duration = wav.getnframes() * 1000 // wav.getframerate()
            data = {"transcription": [{"text": " Test speech", "offsets": {"from": 0, "to": duration},
                                     "tokens": [{"text": "Test", "p": 0.2}, {"text": "<|end|>", "p": 1}]}]}
            if mode == "malformed":
                data["transcription"][0]["text"] = None
            output = Path(args[args.index("-of") + 1]).with_suffix(".json")
            output.write_text("bad JSON" if mode == "invalid" else json.dumps(data), encoding="utf-8")

        def wait(self, timeout=None):
            if mode == "timeout" and self.returncode is None:
                raise subprocess.TimeoutExpired("fake whisper", timeout)
            self.returncode = 3 if mode == "exit" else (self.returncode or 0)
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(audio.subprocess, "Popen", Child)


def test_asr_timestamps_tail_cache_and_command(session, profile, monkeypatch):
    calls = []
    fake_whisper(monkeypatch, calls)
    settings = profile["asr"] | {"use_gpu": False}
    before = (session / "audio.wav").read_bytes()
    for index in range(3):
        result = audio.transcribe_chunk(session, settings, index, audio.audio_size(session / "audio.wav"))
        assert result["error"] is None
        assert result["segments"][0]["start_ms"] == index * 1000
        assert result["segments"][0]["uncertain"] is True
    assert result["duration_ms"] == 500
    assert result["segments"][0]["end_ms"] == 2500
    audio.transcribe_chunk(session, settings, 0, 80000)
    assert len(calls) == 3  # Successful cache reused.
    assert "-ng" in calls[0] and "-ojf" in calls[0] and "ru" in calls[0]
    Path(settings["model"]).write_bytes(b"changed model")
    audio.transcribe_chunk(session, settings, 0, 80000)
    assert len(calls) == 4
    assert not list((session / "spool").glob("asr-*"))
    assert (session / "audio.wav").read_bytes() == before
    assert len(audio.collect_transcript(session)) == 3


@pytest.mark.parametrize("mode", ["invalid", "malformed", "exit", "timeout"])
def test_asr_failures_are_recoverable(session, profile, monkeypatch, mode):
    calls = []
    fake_whisper(monkeypatch, calls, mode)
    result = audio.transcribe_chunk(session, profile["asr"], 0, 80000)
    assert result["error"]
    assert audio.collect_transcript(session)[0]["uncertain"]
    fake_whisper(monkeypatch, calls)
    result = audio.transcribe_chunk(session, profile["asr"], 0, 80000)
    assert result["error"] is None


def test_process_retries_and_changed_chunk_size(session, profile, monkeypatch):
    settings = profile["asr"] | {"enabled": False}
    assert run(session, capture=False, asr_settings=settings)["phase"] == "needs_retry"
    fake_whisper(monkeypatch, [])
    settings = profile["asr"] | {"chunk_seconds": 2}
    result = run(session, capture=False, asr_settings=settings)
    assert result["phase"] == "ready_for_summary"
    assert len(list((session / "chunks").glob("*.json"))) == 2
    assert read_json(session / "transcript.json")[-1]["end_ms"] == 2500
    assert (session / "report.html").exists()
    assert (session / "AGENT_BRIEF.md").exists()


def test_repeated_frames_keep_return_times(session, profile):
    selector = FrameSelector(session, profile["capture"])
    for i, color in enumerate(("white", "white", "black", "white")):
        Image.new("RGB", (640, 360), color).save(session / "spool" / f"frame-{i:06d}.png")
        selector.observe(i, i * 5000)
    assert len(selector.data["slides"]) == 2
    assert selector.data["slides"][0]["seen_at_ms"] == [0, 15000]
    assert selector.data["last_index"] == 3
    assert not list((session / "spool").glob("*.png"))


def test_locks_and_recycled_pid(tmp_path):
    with lock(tmp_path / "x.lock"):
        with pytest.raises(RuntimeError):
            with lock(tmp_path / "x.lock"):
                pass
    with lock(tmp_path / "x.lock"):
        pass
    record = identity(os.getpid())
    assert process(record) is not None
    assert process(record | {"created": record["created"] - 60}) is None


def test_demo_safe_html_and_stale_summary(tmp_path):
    result = demo(tmp_path)
    folder = Path(result["session"])
    summary = read_json(folder / "summary.json")
    summary["brief"][0]["text"] = '<script>alert("XSS")</script>'
    write_json(folder / "summary.json", summary)
    html = render(folder).read_text(encoding="utf-8")
    assert '&lt;script&gt;alert' in html
    assert '<script>alert' not in html
    assert 'src="https://' not in html
    segments = read_json(folder / "transcript.json")
    slides = read_json(folder / "frames.json")["slides"]
    summary["tasks"][0]["sources"] = ["invented-segment"]
    with pytest.raises(ValueError, match="Unknown"):
        validate_summary(summary, segments, slides)
    segments[0]["text"] = "changed source"
    write_json(folder / "transcript.json", segments)
    with pytest.raises(ValueError, match="stale"):
        render(folder)
    assert "Черновик" in render(folder, strict=False).read_text(encoding="utf-8")


def test_bad_slide_path_rejected(tmp_path):
    result = demo(tmp_path)
    folder = Path(result["session"])
    frames = read_json(folder / "frames.json")
    frames["slides"][0]["file"] = "../../secret.png"
    write_json(folder / "frames.json", frames)
    with pytest.raises(ValueError, match="slide path"):
        render(folder)


def test_init_does_not_overwrite(tmp_path, capsys):
    profile = tmp_path / "profiles" / "study.toml"
    assert main(["init", "--profile", str(profile)]) == 0
    original = profile.read_bytes()
    assert main(["init", "--profile", str(profile)]) == 2
    assert profile.read_bytes() == original
    assert (tmp_path / "data" / "agent-state.json").exists()


def test_recording_requires_consent(tmp_path):
    profile = tmp_path / "p.toml"
    profile.write_text("", encoding="utf-8")
    assert main(["start", "--profile", str(profile), "--title", "test"]) == 2


def test_cli_demo_in_subprocess(tmp_path):
    r = subprocess.run([sys.executable, "-m", "auto_meets", "demo", "--output", str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert Path(result["report"]).is_file()
