import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

from PIL import Image, ImageDraw, ImageFont
import pytest

from auto_meets import audio
from auto_meets.capture import frame_event, pipeline, prop
from auto_meets.cli import demo, main
from auto_meets.config import load_profile, validate
from auto_meets.frames import FrameSelector
from auto_meets.report import render, validate_summary
from auto_meets.storage import identity, lock, new_session, process, read_json, write_json
from auto_meets.worker import run


def test_cli_json_survives_legacy_windows_console_encoding(monkeypatch):
    import auto_meets.cli as cli

    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(output, encoding="cp1252"))
    monkeypatch.setattr(cli, "execute", lambda args: {"title": "A\u200b title"})
    assert main(["windows"]) == 0
    sys.stdout.flush()
    assert json.loads(output.getvalue().decode("cp1252"))["title"] == "A\u200b title"


def test_browser_waits_for_new_window_when_profile_is_reused(tmp_path, monkeypatch):
    from auto_meets.platforms import windows as platform

    p = validate({"browser": {"executable": sys.executable,
                              "directory": str(tmp_path / "browser")}})
    arg = f"--user-data-dir={tmp_path / 'browser'}"
    old = {"pid": 123, "window": 1, "title": "Old meeting", "minimized": False}
    new = {"pid": 123, "window": 2, "title": "New meeting", "minimized": False}
    calls = 0

    def fake_windows():
        nonlocal calls
        calls += 1
        return [old] if calls <= 2 else [old, new]

    class Browser:
        pid = 123
        info = {"cmdline": [sys.executable, arg, "--new-window"]}

        def create_time(self):
            return 100.0

    monkeypatch.setattr(platform, "user32", lambda: None)
    monkeypatch.setattr(platform, "windows", fake_windows)
    monkeypatch.setattr(platform.psutil, "process_iter", lambda attrs: [Browser()])
    monkeypatch.setattr(platform.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(platform.time, "sleep", lambda seconds: None)

    result = platform.open_browser(p, "https://example.test/meeting")
    assert result["pid"] == 123
    assert result["windows"] == [new]


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
    assert p["storage"]["root"] == str(tmp_path / "data" / "sessions")


def test_session_folder_has_readable_stable_shape(tmp_path):
    profile = validate({"storage": {"root": str(tmp_path / "data" / "sessions")}})
    folder = new_session(profile, "Лекция: анализ / 2026")
    assert folder.parent == tmp_path / "data" / "sessions"
    assert folder.name.startswith("20")
    assert "лекция-анализ-2026" in folder.name
    assert (folder / "session.json").is_file()


def test_repository_settings_keep_created_files_inside_gitignored_data(tmp_path):
    (tmp_path / ".git").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    settings = data / "recorder.toml"
    settings.write_text('[storage]\nroot="../../elsewhere"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="storage.root"):
        load_profile(settings)
    settings.write_text('[storage]\nroot="../data/sessions"\n[browser]\ndirectory="../../elsewhere"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="browser.directory"):
        load_profile(settings)


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


def test_whisper_blank_audio_marker_is_not_spoken_text():
    raw = {"transcription": [{"text": "[BLANK_AUDIO]", "offsets": {"from": 0, "to": 1000},
                              "tokens": [{"text": "[BLANK_AUDIO]", "p": 1.0}]}]}
    assert audio.normalize(raw, 0, 0, 1000, 0.5) == []


def test_atomic_json_write_recovers_from_transient_windows_access_denied(tmp_path, monkeypatch):
    import auto_meets.storage as storage

    path = tmp_path / "state.json"
    storage.write_json(path, {"phase": "starting"})
    original = storage.os.replace
    calls = 0

    def transient_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(5, "The destination is temporarily open")
        return original(src, dst)

    monkeypatch.setattr(storage.os, "replace", transient_replace)
    storage.write_json(path, {"phase": "recording"})
    assert calls == 2
    assert storage.read_json(path) == {"phase": "recording"}


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
            if mode == "boundary":
                data["transcription"] = [
                    {"text": "Before boundary", "offsets": {"from": 0, "to": 500}},
                    {"text": "Crosses boundary", "offsets": {"from": 500, "to": duration}},
                ]
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
        assert result["segments"][0]["start_ms"] == max(0, index - 1) * 1000
        assert result["segments"][0]["uncertain"] is True
    assert result["duration_ms"] == 1500
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


def test_asr_overlap_omits_segments_wholly_before_boundary(session, profile, monkeypatch):
    fake_whisper(monkeypatch, [], mode="boundary")
    result = audio.transcribe_chunk(session, profile["asr"], 1,
                                    audio.audio_size(session / "audio.wav"))
    assert result["start_ms"] == 0
    assert [segment["text"] for segment in result["segments"]] == ["Crosses boundary"]


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


def test_added_bullet_is_not_lost_as_unchanged_slide(session, profile):
    selector = FrameSelector(session, profile["capture"])
    font = ImageFont.load_default(size=32)
    for index, extra in enumerate(("", "New requirement", "")):
        image = Image.new("RGB", (1280, 720), "white")
        draw = ImageDraw.Draw(image)
        draw.text((80, 70), "Lecture topic", fill="black", font=font)
        draw.text((100, 180), "First point", fill="black", font=font)
        draw.text((100, 260), "Second point", fill="black", font=font)
        if extra:
            draw.text((100, 340), extra, fill="black", font=font)
        image.save(session / "spool" / f"frame-{index:06d}.png")
        selector.observe(index, index * 5000)
    assert len(selector.data["slides"]) == 2
    assert selector.data["slides"][0]["seen_at_ms"] == [0, 10000]


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
    assert (tmp_path / "data" / "runtime" / "agent-state.json").exists()


def test_default_init_creates_machine_settings_without_calendar(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    path = tmp_path / "data" / "recorder.toml"
    assert path.is_file()
    contents = path.read_text(encoding="utf-8")
    assert "[capture]" in contents and "[asr]" in contents
    assert "[schedule]" not in contents and "[report]" not in contents
    assert load_profile(path)["storage"]["root"] == str(tmp_path / "data" / "sessions")


def test_start_uses_per_recording_report_options(tmp_path, monkeypatch):
    import auto_meets.cli as cli

    settings = tmp_path / "data" / "recorder.toml"
    settings.parent.mkdir()
    settings.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli.shutil, "which", lambda command: command)
    seen = {}

    def fake_start(profile, title, target, event_key, wait):
        seen.update(profile["report"])
        return {"phase": "recording"}

    monkeypatch.setattr(cli.worker, "start", fake_start)
    args = cli.parser().parse_args([
        "start", "--settings", str(settings), "--title", "Team", "--kind", "meeting",
        "--detail", "brief", "--instructions", "Decisions first", "--consent",
    ])
    assert cli.execute(args)["phase"] == "recording"
    assert seen == {"kind": "meeting", "detail": "brief", "instructions": "Decisions first"}


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
