"""Run the bundled recorder in a persistent, user-owned Python environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_URL = "https://github.com/Andrey-Good/G0oD_auto_meets.git"


def is_repository(path: Path) -> bool:
    return (path / ".git").exists() and (path / "pyproject.toml").is_file() and (path / "auto_meets").is_dir()


def user_home() -> Path:
    override = os.environ.get("AUTO_MEETS_HOME")
    if override:
        home = Path(override).expanduser().resolve()
    elif is_repository(Path.cwd()):
        home = Path.cwd().resolve()
    else:
        home = (Path.home() / "Documents" / "G0oD_auto_meets").resolve()
    if home == PLUGIN_ROOT or PLUGIN_ROOT in home.parents:
        raise ValueError("AUTO_MEETS_HOME must be outside the installed plugin")
    return home


def ensure_repository(home: Path) -> None:
    if is_repository(home):
        return
    if home.exists():
        raise ValueError(f"Workspace exists but is not a G0oD_auto_meets repository: {home}")
    home.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "clone", "--depth", "1", REPOSITORY_URL, str(home)],
                            capture_output=True, text=True)
    if result.returncode or not is_repository(home):
        raise RuntimeError("Could not clone the repository: " + result.stderr.strip())


def paths(home: Path) -> dict[str, str]:
    return {
        "plugin_root": str(PLUGIN_ROOT),
        "repository": str(home),
        "home": str(home),
        "settings": str(home / "data/recorder.toml"),
        "data": str(home / "data"),
        "sessions": str(home / "data/sessions"),
        "runtime": str(home / "data/runtime"),
        "private_skills": str(Path.home() / ".agents/skills"),
        "runner": str(Path(__file__).resolve()),
    }


def venv_python(home: Path) -> Path:
    return home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install(home: Path) -> Path:
    runtime = home / "data" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    python = venv_python(home)
    bundle_id = (PLUGIN_ROOT / "bundle-id.txt").read_text(encoding="ascii").strip()
    marker = runtime / "plugin-installed-bundle-id"
    if python.exists() and marker.exists() and marker.read_text(encoding="ascii").strip() == bundle_id:
        return python
    if not python.exists():
        result = subprocess.run([sys.executable, "-m", "venv", str(home / ".venv")],
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError("Could not create Python environment: " + result.stderr.strip())
    result = subprocess.run([
        str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--upgrade", "--force-reinstall", str(PLUGIN_ROOT),
    ], capture_output=True, text=True)
    log = runtime / "plugin-install.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"Recorder installation failed; see {log}")
    marker.write_text(bundle_id + "\n", encoding="ascii")
    return python


def run(python: Path, home: Path, command: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(python), "-m", "auto_meets", command, *args],
                          cwd=home, capture_output=True, text=True)


def ensure_settings(python: Path, home: Path) -> None:
    settings = home / "data/recorder.toml"
    if settings.exists():
        return
    result = run(python, home, "init", "--settings", str(settings))
    if result.returncode:
        raise RuntimeError("Could not create recorder settings: " + result.stderr.strip())


def main(argv: list[str]) -> int:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")
    if not argv:
        raise ValueError("Choose paths, setup, or an auto-meets command")
    home = user_home()
    command, *args = argv
    if command == "paths":
        print(json.dumps(paths(home), indent=2))
        return 0
    ensure_repository(home)
    python = install(home)
    if command == "setup":
        ensure_settings(python, home)
        result = run(python, home, "doctor", "--settings", paths(home)["settings"])
        try:
            diagnosis = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RuntimeError("Doctor produced invalid output: " + result.stderr.strip()) from None
        print(json.dumps({**paths(home), "ready": bool(diagnosis.get("ok")),
                          "doctor": diagnosis}, indent=2))
        return 0
    if command == "init" and (home / "data/recorder.toml").exists():
        print(json.dumps(paths(home), indent=2))
        return 0
    if command in {"init", "doctor", "browser", "start", "list", "import-wav"}:
        if command != "init":
            ensure_settings(python, home)
        if "--settings" not in args and "--profile" not in args:
            args.extend(["--settings", paths(home)["settings"]])
    result = run(python, home, command, *args)
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
