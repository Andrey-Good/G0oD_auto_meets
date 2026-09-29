import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auto_meets import chat
from auto_meets.config import validate
from auto_meets.report import render, validate_summary
from auto_meets.storage import new_session, read_json, write_json


def request(port, path, method="GET", payload=None, token="", version=chat.EXTENSION_VERSION):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers["X-Extension-Version"] = version
    with urlopen(Request(f"http://127.0.0.1:{port}{path}", data=body,
                         headers=headers, method=method), timeout=2) as response:
        return json.load(response)


def test_local_chat_receiver_and_report_evidence(tmp_path, monkeypatch):
    profile = validate({"storage": {"root": str(tmp_path / "data" / "sessions")}})
    folder = new_session(profile, "Chat test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(chat, "PORT", port)
    write_json(folder / "chat-service.json", {"session": str(folder.resolve()), "selector": ".message",
                                          "url": "https://meeting.example/room", "token": "test-secret"})
    thread = threading.Thread(target=chat.serve, args=(folder,), daemon=True)
    thread.start()
    for _ in range(30):
        if (folder / "chat-ready").exists():
            break
        time.sleep(.05)
    assert request(port, "/config")["selector"] == ".message"
    assert request(port, "/diagnostic", "POST", {
        "url": "https://meeting.example/room", "error": None}, "test-secret") == {"ok": True}
    assert read_json(folder / "chat-connection.json")["attached_at"]
    try:
        request(port, "/config", version="0.0.1")
        assert False, "Old extension was accepted"
    except HTTPError as error:
        assert error.code == 409
    assert "Reload browser extension" in chat.status(folder)["connection"]["error"]
    message = {"text": "  Homework:  chapter 2  ", "url": "https://meeting.example/room?x=1"}
    try:
        request(port, "/messages", "POST", message, "wrong")
        assert False, "Unauthorized message was saved"
    except HTTPError as error:
        assert error.code == 403
    assert request(port, "/messages", "POST", message, "test-secret") == {"saved": "m000001"}
    item = json.loads((folder / "chat.jsonl").read_text(encoding="utf-8"))
    assert item["text"] == "Homework: chapter 2"
    render(folder)
    summary = read_json(folder / "summary.example.json")
    summary["brief"] = [{"text": "Homework is chapter 2.", "sources": ["m000001"]}]
    validate_summary(summary, [], [], [item])
    write_json(folder / "summary.json", summary)
    assert "Homework is chapter 2." in render(folder).read_text(encoding="utf-8")
    assert 'href="#m000001"' in (folder / "report.html").read_text(encoding="utf-8")
    (folder / "chat-stop.request").write_text("stop\n", encoding="utf-8")
    thread.join(timeout=2)
    assert not thread.is_alive()
