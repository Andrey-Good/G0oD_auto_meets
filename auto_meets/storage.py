"""Disk is the queue and source of truth. No database or resident web service."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

import psutil


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def lock(path: Path):
    """OS releases the lock even after a crash. Never delete lock files (inode race)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+b")
    if path.stat().st_size == 0:
        f.write(b"0")
        f.flush()
    f.seek(0)
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as e:
            raise RuntimeError(f"Already in use: {path}") from e
        yield
    finally:
        if acquired:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def identity(pid: int) -> dict:
    return {"pid": pid, "created": psutil.Process(pid).create_time()}


def spawned_identity(pid: int):
    # A very short-lived subprocess can exit before psutil observes it on Windows.
    try:
        return identity(pid)
    except psutil.NoSuchProcess:
        return None


def process(record: dict | None):
    """A recycled PID is not our process."""
    if not record:
        return None
    try:
        p = psutil.Process(record["pid"])
        if (abs(p.create_time() - record["created"]) < 0.01
                and p.status() != psutil.STATUS_ZOMBIE):
            return p
    except (psutil.Error, KeyError, TypeError):
        pass
    return None


def new_session(profile: dict, title: str, event_key: str = "", target=None) -> Path:
    root = Path(profile["storage"]["root"])
    root.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    folder = root / name
    folder.mkdir()
    for sub in ("frames", "chunks", "spool"):
        (folder / sub).mkdir()
    write_json(folder / "session.json", {
        "schema_version": 1, "title": title, "created_at": now(), "event_key": event_key,
        "profile": profile, "target": target or {}, "recording_authorized": True,
    })
    write_json(folder / "state.json", {"phase": "starting", "updated_at": now()})
    return folder


def metadata(folder: Path) -> dict:
    m = read_json(folder / "session.json")
    if not isinstance(m, dict) or m.get("schema_version") != 1:
        raise ValueError(f"Not a supported session: {folder}")
    return m


def status(folder: Path) -> dict:
    metadata(folder)
    state = read_json(folder / "state.json", {})
    active = process(state.get("worker") or read_json(folder / "launch.json")) is not None
    return {"session": str(folder.resolve()), **state, "worker_alive": active,
            "capture_alive": process(state.get("capture")) is not None,
            "interrupted": state.get("phase") in ("starting", "recording", "processing")
            and not active, "summary_present": (folder / "summary.json").exists(),
            "stop_requested": (folder / "stop.request").exists()}
