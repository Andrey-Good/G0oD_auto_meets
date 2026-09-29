"""Local, session-scoped receiver for messages observed by the browser extension."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.parse import urlparse

from .storage import metadata, now, process, read_json, spawned_identity, write_json, write_text

HOST = "127.0.0.1"
PORT = 18765
MAX_BODY = 16_384
EXTENSION_VERSION = "0.2.1"


def _files(folder: Path) -> tuple[Path, Path]:
    return folder / "chat-service.json", folder / "chat-stop.request"


def _count(folder: Path) -> int:
    path = folder / "chat.jsonl"
    if not path.exists():
        return 0
    with path.open("rb") as stream:
        return sum(1 for _ in stream)


def status(folder: Path) -> dict:
    metadata(folder)
    info = read_json(_files(folder)[0], {})
    connection = read_json(folder / "chat-connection.json", {})
    running = process(info.get("process")) is not None
    try:
        recent = (datetime.now(timezone.utc) - datetime.fromisoformat(
            connection["attached_at"])).total_seconds() < 15
    except (KeyError, TypeError, ValueError):
        recent = False
    return {"session": str(folder.resolve()), "running": running,
            "connected": running and recent and not connection.get("error"),
            "messages": _count(folder), "chat_file": str(folder / "chat.jsonl"),
            "port": info.get("port"), "extension_version": EXTENSION_VERSION,
            "connection": connection}


def start(folder: Path, selector: str, url: str, wait: float = 5) -> dict:
    metadata(folder)
    if not selector.strip() or len(selector) > 500:
        raise ValueError("--selector must be a nonempty CSS selector up to 500 characters")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("--url must be the meeting page's HTTP(S) URL")
    if not 0 <= wait <= 30:
        raise ValueError("--wait must be between 0 and 30 seconds")
    info_path, stop_path = _files(folder)
    previous = read_json(info_path, {})
    if process(previous.get("process")) is not None:
        raise RuntimeError(f"Chat collector already running for {folder}")
    stop_path.unlink(missing_ok=True)
    (folder / "chat-ready").unlink(missing_ok=True)
    (folder / "chat-connection.json").unlink(missing_ok=True)
    token = secrets.token_urlsafe(32)
    write_json(info_path, {"session": str(folder.resolve()), "selector": selector,
                           "url": url, "token": token, "port": PORT, "started_at": now()})
    options = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == "nt" else {"start_new_session": True})
    with (folder / "chat-service.log").open("a", encoding="utf-8") as log:
        child = subprocess.Popen([sys.executable, "-m", "auto_meets", "_chat-server",
                                  "--session", str(folder)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, close_fds=True, **options)
    previous = read_json(info_path)
    previous["process"] = spawned_identity(child.pid)
    write_json(info_path, previous)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"Chat collector exited; see {folder / 'chat-service.log'}")
        if (folder / "chat-ready").exists():
            break
        time.sleep(0.1)
    result = status(folder)
    result["ready"] = (folder / "chat-ready").exists() and result["running"]
    result["extension"] = "The browser extension activates in the matching tab within 30 seconds"
    return result


def stop(folder: Path, wait: float = 5) -> dict:
    metadata(folder)
    if not 0 <= wait <= 30:
        raise ValueError("--wait must be between 0 and 30 seconds")
    write_text(_files(folder)[1], now() + "\n")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and status(folder)["running"]:
        time.sleep(0.1)
    return status(folder)


def serve(folder: Path) -> None:
    metadata(folder)
    info_path, stop_path = _files(folder)
    info = read_json(info_path)
    if not info or Path(info["session"]).resolve() != folder.resolve():
        raise ValueError("Chat service configuration does not match session")
    url = urlparse(info["url"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _reply(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local(self) -> bool:
            return self.client_address[0] == HOST and self.headers.get("Host") == f"{HOST}:{PORT}"

        def do_GET(self):
            if not self._local() or self.path != "/config":
                return self._reply(404, {"error": "not found"})
            if stop_path.exists() or (folder / "stop.request").exists():
                return self._reply(410, {"active": False})
            version = self.headers.get("X-Extension-Version")
            if version != EXTENSION_VERSION:
                write_json(folder / "chat-connection.json", {
                    "seen_at": now(), "extension_version": version,
                    "error": f"Reload browser extension version {EXTENSION_VERSION} in the browser"})
                return self._reply(409, {"error": "extension update required"})
            state = read_json(folder / "chat-connection.json", {})
            write_json(folder / "chat-connection.json", state | {
                "seen_at": now(), "extension_version": version})
            return self._reply(200, {"active": True, "selector": info["selector"],
                                     "url": info["url"], "token": info["token"]})

        def do_POST(self):
            if not self._local() or self.path not in ("/messages", "/diagnostic"):
                return self._reply(404, {"error": "not found"})
            if stop_path.exists() or (folder / "stop.request").exists():
                return self._reply(410, {"error": "stopped"})
            if self.headers.get("Authorization") != f"Bearer {info['token']}":
                return self._reply(403, {"error": "forbidden"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("invalid length")
                item = json.loads(self.rfile.read(size))
                if self.path == "/diagnostic":
                    if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                        raise ValueError("invalid diagnostic")
                    source = urlparse(item["url"])
                    if (source.scheme != url.scheme or source.netloc != url.netloc
                            or not source.path.startswith(url.path)):
                        raise ValueError("wrong page")
                    error = item.get("error")
                    if error is not None and not isinstance(error, str):
                        raise ValueError("invalid diagnostic error")
                    state = read_json(folder / "chat-connection.json", {})
                    state.update(checked_at=now(), error=error[:500] if error else None)
                    if not error:
                        state["attached_at"] = now()
                        state["url"] = item["url"]
                    write_json(folder / "chat-connection.json", state)
                    return self._reply(200, {"ok": True})
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    raise ValueError("invalid message")
                text = " ".join(item["text"].split())[:4000]
                page_url = item.get("url", "")
                if not isinstance(page_url, str):
                    raise ValueError("invalid URL")
                source = urlparse(page_url)
                if (not text or source.scheme != url.scheme or source.netloc != url.netloc
                        or not source.path.startswith(url.path)):
                    raise ValueError("wrong page or empty message")
                record = {"id": f"m{_count(folder) + 1:06d}", "observed_at": now(),
                          "text": text, "url": page_url}
                with (folder / "chat.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                self._reply(200, {"saved": record["id"]})
            except (ValueError, TypeError, json.JSONDecodeError):
                self._reply(400, {"error": "invalid message"})

    ready = folder / "chat-ready"
    ready.unlink(missing_ok=True)
    with HTTPServer((HOST, PORT), Handler) as server:
        server.timeout = 0.5
        write_text(ready, now() + "\n")
        try:
            while not stop_path.exists() and not (folder / "stop.request").exists():
                server.handle_request()
        finally:
            ready.unlink(missing_ok=True)
