"""Keep changed slides, merge recurring slides, preserve all their appearance times."""
from pathlib import Path
import shutil

from PIL import Image, ImageChops, ImageStat

from .storage import read_json, write_json


def thumbnail(path: Path):
    with Image.open(path) as image:
        image.load()
        return image.convert("L").resize((160, 90))


def distance(a, b) -> float:
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0] / 255


class FrameSelector:
    def __init__(self, folder: Path, settings: dict):
        self.folder, self.settings = folder, settings
        self.data = read_json(folder / "frames.json", {"schema_version": 1, "last_index": -1,
                                                       "slides": []})
        self.thumbs = [thumbnail(folder / s["file"]) for s in self.data["slides"]]
        self.last = None
        self.last_id = None

    def observe(self, index: int, milliseconds: int):
        path = self.folder / "spool" / f"frame-{index:06d}.png"
        if index <= self.data["last_index"]:
            path.unlink(missing_ok=True)
            return
        thumb = thumbnail(path)
        if self.last is None or distance(thumb, self.last) > self.settings["change_threshold"]:
            duplicate = next((i for i, t in enumerate(self.thumbs)
                              if distance(thumb, t) <= self.settings["duplicate_threshold"]), None)
            if duplicate is None:
                name = f"frames/f{index:06d}.png"
                shutil.copyfile(path, self.folder / name)
                self.data["slides"].append({"id": f"f{index:06d}", "file": name,
                                            "at_ms": milliseconds, "seen_at_ms": [milliseconds]})
                self.thumbs.append(thumb)
                self.last_id = len(self.thumbs) - 1
            else:
                slide = self.data["slides"][duplicate]
                if duplicate != self.last_id:
                    slide["seen_at_ms"].append(milliseconds)
                self.last_id = duplicate
            self.last = thumb
        self.data["last_index"] = index
        write_json(self.folder / "frames.json", self.data)
        path.unlink(missing_ok=True)
