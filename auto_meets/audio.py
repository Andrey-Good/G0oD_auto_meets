"""Repairable WAV recording, bounded ASR jobs, cached timestamped transcript."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess

from .storage import spawned_identity, read_json, write_json, write_text

RATE = 16000
BYTES_SECOND = RATE * 2
HEADER = 44


def wav_header(size: int) -> bytes:
    if not 0 <= size <= 0xFFFFFFFF - 36:
        raise ValueError("Recording is too large for WAV (4 GiB)")
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", size + 36, b"WAVE", b"fmt ",
                       16, 1, 1, RATE, BYTES_SECOND, 2, 16, b"data", size)


def new_audio(path: Path):
    with path.open("xb") as f:
        f.write(wav_header(0))


def audio_size(path: Path) -> int:
    if not path.exists():
        return 0
    return max(0, path.stat().st_size - HEADER) // 2 * 2


def repair_audio(path: Path):
    """Payload survives a killed recorder; fix its size fields after the writer exits."""
    size = audio_size(path)
    with path.open("r+b") as f:
        f.write(wav_header(size))
        f.truncate(HEADER + size)
        f.flush()


def timestamp(ms: int) -> str:
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def normalize(raw: dict, index: int, start_ms: int, duration_ms: int, threshold: float):
    if not isinstance(raw, dict) or not isinstance(raw.get("transcription"), list):
        raise ValueError("whisper-cli returned no transcription array")
    segments = []
    for i, entry in enumerate(raw["transcription"]):
        text = entry["text"].strip()
        offsets = entry["offsets"]
        a, b = offsets["from"], offsets["to"]
        if (type(a) not in (int, float) or type(b) not in (int, float)
                or not math.isfinite(a) or not math.isfinite(b) or a < 0 or b < a):
            raise ValueError("Invalid whisper timestamp")
        if not text or a >= duration_ms:
            continue
        probs = [t["p"] for t in entry.get("tokens", [])
                 if not t.get("text", "").startswith("<|")
                 and type(t.get("p")) in (int, float) and 0 <= t["p"] <= 1]
        confidence = sum(probs) / len(probs) if probs else None
        segments.append({"id": f"c{index:06d}s{i:04d}", "start_ms": start_ms + int(a),
                         "end_ms": start_ms + min(int(b), duration_ms), "text": text,
                         "uncertain": confidence is not None and confidence < threshold,
                         "mean_token_probability": confidence})
    return segments


def transcribe_chunk(folder: Path, settings: dict, index: int, size: int, force=False):
    length = settings["chunk_seconds"] * BYTES_SECOND
    offset = index * length
    count = min(length, size - offset)
    start_ms, duration_ms = offset * 1000 // BYTES_SECOND, count * 1000 // BYTES_SECOND
    result_path = folder / "chunks" / f"{index:06d}.json"
    with (folder / "audio.wav").open("rb") as f:
        f.seek(HEADER + offset)
        pcm = f.read(count)
    if len(pcm) != count or count <= 0:
        raise ValueError("Audio changed during processing")
    model = Path(settings["model"])
    stat = model.stat() if settings["model"] and model.is_file() else None
    signature = hashlib.sha256(pcm + json.dumps(settings, sort_keys=True).encode()
                               + str((stat.st_size, stat.st_mtime_ns) if stat else None).encode()).hexdigest()
    cached = read_json(result_path)
    if cached and cached.get("signature") == signature and not cached.get("error") and not force:
        return cached
    result = {"index": index, "signature": signature, "start_ms": start_ms,
              "duration_ms": duration_ms, "segments": [], "error": None}
    stem = folder / "spool" / f"asr-{index:06d}"
    wav, output = stem.with_suffix(".wav"), stem.with_suffix(".json")
    try:
        if not settings["enabled"]:
            raise ValueError("ASR disabled; enable it and run process with a configured profile")
        if not stat:
            raise ValueError("ASR model not found; set asr.model in the profile")
        # Exact digital silence does not need a model and should not hallucinate speech.
        if any(pcm):
            wav.write_bytes(wav_header(len(pcm)) + pcm)
            output.unlink(missing_ok=True)
            args = [settings["executable"], "-m", str(model), "-f", str(wav),
                    "-l", settings["language"], "-t", str(settings["threads"]),
                    "-ojf", "-of", str(stem)]
            if not settings["use_gpu"]:
                args.append("-ng")
            with (folder / "asr.log").open("a", encoding="utf-8") as log:
                log.write(f"\n--- chunk {index}, {start_ms} ms ---\n")
                log.flush()
                child = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                         close_fds=True)
                write_json(folder / "asr-process.json", spawned_identity(child.pid))
                try:
                    code = child.wait(timeout=settings["timeout_seconds"])
                    if code:
                        raise subprocess.CalledProcessError(code, args)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=10)
                    write_json(folder / "asr-process.json", None)
            raw = read_json(output)
            result["segments"] = normalize(raw, index, start_ms, duration_ms,
                                            settings["uncertain_probability"])
            if not result["segments"]:
                result["segments"] = [{"id": f"c{index:06d}missing", "start_ms": start_ms,
                                        "end_ms": start_ms + duration_ms, "uncertain": True,
                                        "text": "[Нет распознанной речи; проверьте аудио]"}]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        wav.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
    write_json(result_path, result)
    return result


def collect_transcript(folder: Path) -> list[dict]:
    segments = []
    for path in sorted((folder / "chunks").glob("*.json")):
        r = read_json(path)
        if r.get("error"):
            segments.append({"id": f"c{r['index']:06d}error", "start_ms": r["start_ms"],
                             "end_ms": r["start_ms"] + r["duration_ms"], "uncertain": True,
                             "text": "[Фрагмент не распознан]", "error": r["error"]})
        else:
            segments.extend(r["segments"])
    segments.sort(key=lambda s: (s["start_ms"], s["id"]))
    write_json(folder / "transcript.json", segments)
    write_text(folder / "transcript.md", "# Транскрипция\n\n" + "\n\n".join(
        f"[{timestamp(s['start_ms'])}–{timestamp(s['end_ms'])}] `{s['id']}` "
        + ("**Проверить:** " if s["uncertain"] else "") + s["text"] for s in segments) + "\n")
    return segments


class AudioQueue:
    """At most one ASR subprocess; backlog stays on disk, not in memory."""
    def __init__(self, folder: Path, settings: dict, force=False):
        self.folder, self.settings, self.force = folder, settings, force
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.index = 0
        self.errors = 0

    def tick(self, final=False) -> bool:
        if self.future:
            if not self.future.done():
                return False
            result = self.future.result()
            self.errors += bool(result.get("error"))
            self.index += 1
            self.future = None
            collect_transcript(self.folder)
        size = audio_size(self.folder / "audio.wav")
        length = self.settings["chunk_seconds"] * BYTES_SECOND
        total = (size + length - 1) // length if final else size // length
        if self.index < total:
            self.future = self.pool.submit(transcribe_chunk, self.folder, self.settings,
                                           self.index, size, self.force)
            return False
        return True

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
