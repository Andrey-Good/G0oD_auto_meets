"""Windows-only window discovery, isolated Chromium launch and capture sources."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import psutil

ELEMENTS = ["wasapi2src", "d3d11screencapturesrc", "d3d11download"]


def user32():
    if sys.platform != "win32":
        raise RuntimeError("Window capture requires Windows 11; use backend=test for smoke tests")
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.IsWindow.argtypes = [wintypes.HWND]
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.IsIconic.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    return u


def windows() -> list[dict]:
    u = user32()
    result = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd, _):
        length = u.GetWindowTextLengthW(hwnd)
        if u.IsWindowVisible(hwnd) and length:
            title = ctypes.create_unicode_buffer(length + 1)
            u.GetWindowTextW(hwnd, title, length + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            result.append({"window": int(hwnd), "pid": pid.value, "title": title.value,
                           "minimized": bool(u.IsIconic(hwnd))})
        return True

    u.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    if not u.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


def preflight(profile, target):
    u = user32()
    if sys.getwindowsversion().build < 22000:
        raise RuntimeError("Windows 11 or newer is required")
    pid, hwnd = target.get("pid", 0), target.get("window", 0)
    if type(pid) is not int or type(hwnd) is not int or pid <= 0 or hwnd <= 0:
        raise ValueError("Provide a browser PID and HWND from the browser/windows command")
    if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
        raise ValueError("Capture window is missing, hidden or minimized")
    actual = wintypes.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(actual))
    root = psutil.Process(pid)
    if actual.value not in {pid, *(p.pid for p in root.children(recursive=True))}:
        raise ValueError("HWND does not belong to the selected PID/process tree")
    # PID identity is checked again inside the detached worker, not only in the CLI.
    created = target.get("created")
    if created is not None and abs(root.create_time() - created) > 0.01:
        raise ValueError("Selected browser process has changed; discover it again")


def sources(profile, target):
    c = profile["capture"]
    audio = [["wasapi2src", "loopback=true", "loopback-mode=include-process-tree",
              f"loopback-target-pid={target['pid']}"]]
    if c["microphone"]:
        mic = ["wasapi2src", "loopback=false"]
        if c["microphone_device"]:
            from ..capture import prop
            mic.append(prop("device", c["microphone_device"]))
        audio.append(mic)
    x, y, width, height = c["crop"]
    video = ["d3d11screencapturesrc", "capture-api=wgc", f"window-handle={target['window']}",
             "show-cursor=false", "show-border=true", f"crop-x={x}", f"crop-y={y}",
             f"crop-width={width}", f"crop-height={height}", "!",
             f"video/x-raw(memory:D3D11Memory),framerate=1/{c['frame_seconds']}",
             "!", "d3d11download"]
    return audio, video


@contextmanager
def awake():
    user32()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    kernel.SetThreadExecutionState.restype = wintypes.DWORD
    # Keep both system and display awake; this does not unlock Windows or defeat policy.
    if not kernel.SetThreadExecutionState(0x80000003):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel.SetThreadExecutionState(0x80000000)


def open_browser(profile: dict, url: str) -> dict:
    user32()
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("Browser URL must use http or https")
    directory = Path(profile["browser"]["directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    exe = profile["browser"]["executable"]
    if not exe:
        candidates = [Path(os.environ.get(key, "C:/")) / suffix
                      for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
                      for suffix in ("Microsoft/Edge/Application/msedge.exe",
                                     "Google/Chrome/Application/chrome.exe")]
        exe = next((str(p) for p in candidates if p.is_file()), "")
    if not exe or not Path(exe).is_file():
        raise ValueError("Set browser.executable to msedge.exe, chrome.exe or Chromium")
    arg = f"--user-data-dir={directory}"
    child = subprocess.Popen([exe, arg, "--no-first-run", "--new-window", url],
                             close_fds=True)
    for _ in range(40):
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                args = p.info["cmdline"] or []
                if any(a.casefold() == arg.casefold() for a in args) and not any(
                        a.startswith("--type=") for a in args):
                    visible = [w for w in windows() if w["pid"] == p.pid]
                    if visible:
                        return {"pid": p.pid, "created": p.create_time(),
                                "windows": visible, "browser_directory": str(directory)}
            except (psutil.Error, OSError):
                continue
        time.sleep(0.25)
    child.poll()
    raise RuntimeError("Browser opened, but no window was discovered; run auto-meets windows")
