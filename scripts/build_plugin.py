"""Build or verify the self-contained plugin bundle from repository sources."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "good-auto-meets"
SKILLS = ("setup", "schedule", "capture", "report")


def contents(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


def sources() -> dict[Path, bytes]:
    result = {
        Path("pyproject.toml"): contents(ROOT / "pyproject.toml"),
        Path("LICENSE"): contents(ROOT / "LICENSE"),
    }
    for source in (ROOT / "auto_meets").rglob("*"):
        if source.is_file() and source.suffix in {".py", ".html", ".toml"}:
            result[source.relative_to(ROOT)] = contents(source)
    for source in (ROOT / "browser-extension").glob("*"):
        if source.is_file() and source.suffix in {".json", ".js"}:
            result[source.relative_to(ROOT)] = contents(source)
    for name in ("USAGE.md", "TESTING.md"):
        result[Path("docs") / name] = contents(ROOT / "docs" / name)
    for name in SKILLS:
        source = ROOT / ".agents" / "skills" / f"auto-meets-{name}" / "SKILL.md"
        result[Path("skills/good-auto-meets/references") / f"{name}.md"] = contents(source)
    return result


def bundle_id(files: dict[Path, bytes]) -> bytes:
    digest = hashlib.sha256()
    for path, contents in sorted(files.items()):
        digest.update(path.as_posix().encode("utf-8") + b"\0")
        digest.update(contents)
    return (digest.hexdigest() + "\n").encode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed bundle is stale")
    args = parser.parse_args()
    files = sources()
    files[Path("bundle-id.txt")] = bundle_id(files)
    stale = []
    for relative, contents in files.items():
        target = PLUGIN / relative
        if not target.is_file() or target.read_bytes().replace(b"\r\n", b"\n") != contents:
            stale.append(str(relative))
            if not args.check:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
    if args.check and stale:
        print("Plugin bundle is stale: " + ", ".join(stale), file=sys.stderr)
        return 1
    print(f"Plugin bundle {'verified' if args.check else 'built'}: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
