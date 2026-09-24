"""Public commands print JSON for agents. `python -m auto_meets` is equivalent."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import wave

from PIL import Image, ImageDraw
import psutil

from .audio import collect_transcript, new_audio, repair_audio
from .capture import doctor
from .config import DEFAULTS, load_profile
from .frames import FrameSelector
from .report import TEMPLATES, render, source_digest
from .storage import metadata, new_session, read_json, status, write_json
from . import worker


def demo(root: Path) -> dict:
    profile = copy.deepcopy(DEFAULTS)
    profile["storage"]["root"] = str(root.resolve())
    profile["capture"]["backend"] = "test"
    profile["asr"]["enabled"] = False
    folder = new_session(profile, "Демонстрация — синтетические данные, не реальная встреча")
    new_audio(folder / "audio.wav")
    with (folder / "audio.wav").open("ab") as f:
        f.write(b"\0" * 32000 * 4)
    repair_audio(folder / "audio.wav")
    frames = FrameSelector(folder, profile["capture"])
    for index, fill in enumerate(("white", "gray", "white")):
        image = Image.new("RGB", (960, 540), fill)
        draw = ImageDraw.Draw(image)
        draw.text((60, 60), "DEMO / SYNTHETIC SLIDE", fill="black", font_size=32)
        draw.rectangle((60, 180, 200 + index % 2 * 500, 400), outline="black", width=4)
        image.save(folder / "spool" / f"frame-{index:06d}.png")
        frames.observe(index, index * 1000)
    segments = [
        {"id": "c000000s0000", "start_ms": 0, "end_ms": 2000, "uncertain": False,
         "text": "Пример: отделяем данные от их интерпретации."},
        {"id": "c000000s0001", "start_ms": 2000, "end_ms": 4000, "uncertain": True,
         "text": "Пример задания: подготовить сравнение подходов. Точный срок не назван."},
    ]
    write_json(folder / "chunks" / "000000.json", {"index": 0, "segments": segments,
               "start_ms": 0, "duration_ms": 4000, "error": None, "synthetic": True})
    collect_transcript(folder)
    write_json(folder / "state.json", {"phase": "ready_for_summary", "asr_errors": 0,
                                       "synthetic": True, "audio_seconds": 4})
    render(folder)
    summary = read_json(folder / "summary.example.json")
    summary["brief"] = [{"text": "Это демонстрационный отчёт; его текст не получен из аудио.",
                          "sources": [segments[0]["id"]]}]
    summary["sections"] = [{"title": "Основной материал", "text": "Запись, транскрипция и пересказ хранятся отдельно. Повторный кадр объединён с первым, но время его возвращения сохранено.",
                            "sources": [segments[0]["id"]], "frames": ["f000000", "f000001"]}]
    summary["tasks"] = [{"text": "Подготовить сравнение подходов (вымышленный пример).", "owner": "",
                         "deadline": "", "sources": [segments[1]["id"]]}]
    summary["uncertainties"] = [{"text": "Распознавание речи и Windows-захват в этой демонстрации не выполняются.",
                                 "sources": []}]
    summary["source_digest"] = source_digest(segments, frames.data["slides"])
    write_json(folder / "summary.json", summary)
    return {"session": str(folder), "report": str(render(folder)), "synthetic": True}


def parser():
    p = argparse.ArgumentParser(prog="auto-meets", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "browser", "start", "list", "import-wav"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--profile", type=Path, default=Path("profiles/study.toml"))
        if name == "browser":
            cmd.add_argument("--url", required=True)
        if name in ("start", "import-wav"):
            cmd.add_argument("--title", required=True)
        if name == "start":
            cmd.add_argument("--pid", type=int, default=0)
            cmd.add_argument("--window", type=lambda s: int(s, 0), default=0)
            cmd.add_argument("--event-key", default="")
            cmd.add_argument("--consent", action="store_true", help="Recording authorized by the user")
            cmd.add_argument("--wait", type=float, default=20)
        if name == "import-wav":
            cmd.add_argument("--file", type=Path, required=True)
    sub.add_parser("windows")
    cmd = sub.add_parser("demo")
    cmd.add_argument("--output", type=Path, default=Path("data"))
    for name in ("status", "stop", "process", "render", "_worker"):
        cmd = sub.add_parser(name, help=argparse.SUPPRESS if name == "_worker" else None)
        cmd.add_argument("--session", type=Path, required=True)
        if name == "stop":
            cmd.add_argument("--wait", type=float, default=0)
        if name == "process":
            cmd.add_argument("--profile", type=Path, help="Use this profile's ASR settings only")
            cmd.add_argument("--force", action="store_true", help="Ignore successful ASR cache")
    return p


def execute(args):
    command = args.command
    if command == "init":
        path = args.profile.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as f:
            f.write((TEMPLATES / "profile.toml").read_text(encoding="utf-8"))
        profile = load_profile(path)
        root = Path(profile["storage"]["root"])
        root.mkdir(parents=True, exist_ok=True)
        memory = root / "agent-state.json"
        if not memory.exists():
            write_json(memory, {"next_event": None, "wakeup_id": None, "active_session": None,
                                "last_checked_at": None, "notes": []})
        return {"profile": str(path), "agent_state": str(memory)}
    if command == "demo":
        return demo(args.output)
    if command == "windows":
        from .platforms.windows import windows
        return windows()
    if command in ("doctor", "browser", "start", "list", "import-wav"):
        profile = load_profile(args.profile)
        if command == "doctor":
            return doctor(profile)
        if command == "list":
            return [status(p.parent) for p in sorted(Path(profile["storage"]["root"]).glob("*/session.json"))]
        if command == "browser":
            from .platforms.windows import open_browser
            return open_browser(profile, args.url)
        if command == "start":
            if not args.consent:
                raise ValueError("Recording requires explicit authorization: pass --consent after obtaining it")
            if not 0 <= args.wait <= 300:
                raise ValueError("--wait must be between 0 and 300 seconds")
            if not shutil.which(profile["capture"]["gst"]):
                raise ValueError("GStreamer not found; configure the profile and run doctor")
            return worker.start(profile, args.title, {"pid": args.pid, "window": args.window},
                                args.event_key, args.wait)
        with wave.open(str(args.file.resolve()), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("Import requires PCM WAV, mono, 16-bit, 16000 Hz; convert externally first")
            folder = new_session(profile, args.title)
            new_audio(folder / "audio.wav")
            with (folder / "audio.wav").open("ab") as dest:
                while data := source.readframes(16000 * 60):
                    dest.write(data)
        repair_audio(folder / "audio.wav")
        return worker.run(folder, capture=False)
    folder = args.session.resolve()
    metadata(folder)
    if command == "status":
        return status(folder)
    if command == "stop":
        if not 0 <= args.wait <= 3600:
            raise ValueError("--wait must be between 0 and 3600 seconds")
        return worker.stop(folder, args.wait)
    if command == "render":
        return {"report": str(render(folder))}
    if command == "process":
        settings = load_profile(args.profile)["asr"] if args.profile else read_json(
            folder / "asr-settings.json", metadata(folder)["profile"]["asr"])
        return worker.run(folder, capture=False, asr_settings=settings, force=args.force)
    if command == "_worker":
        return worker.run(folder)
    raise ValueError(f"Unknown command: {command}")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if isinstance(result, dict) and (result.get("ok") is False or result.get("phase") in ("failed", "needs_retry")):
            return 2
        return 0
    except (OSError, ValueError, RuntimeError, wave.Error, psutil.Error) as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False), file=sys.stderr)
        return 2
