"""Read one TOML profile. Resolve paths once, relative to that profile, not the shell."""
from pathlib import Path
import math
import tomllib

DEFAULTS = {
    "storage": {"root": "../data"},
    "schedule": {"source": "", "include": [], "exclude": [], "check_minutes": 5},
    "report": {"kind": "lecture", "detail": "detailed", "instructions": ""},
    "capture": {
        "backend": "windows", "gst": "gst-launch-1.0", "inspect": "gst-inspect-1.0",
        "frame_seconds": 5, "change_threshold": 0.025, "duplicate_threshold": 0.01,
        "microphone": False, "microphone_device": "", "crop": [0, 0, 0, 0],
        "max_hours": 6, "min_free_mb": 512, "stall_seconds": 30,
    },
    "asr": {
        "enabled": True, "executable": "whisper-cli", "model": "",
        "language": "ru", "chunk_seconds": 120, "threads": 4, "use_gpu": True,
        "timeout_seconds": 900, "uncertain_probability": 0.5,
    },
    "browser": {"executable": "", "directory": "../data/browser"},
}


def validate(p: dict) -> dict:
    """Strict keys/types catch misspelled settings before a meeting starts."""
    if not isinstance(p, dict) or set(p) - set(DEFAULTS):
        raise ValueError("Unknown profile section")
    out = {}
    for section, defaults in DEFAULTS.items():
        values = p.get(section, {})
        if not isinstance(values, dict) or set(values) - set(defaults):
            raise ValueError(f"Unknown settings in [{section}]")
        out[section] = defaults | values
        for key, default in defaults.items():
            value = out[section][key]
            if type(default) in (int, float):
                valid = type(value) in (int, float) and math.isfinite(value)
            else:
                valid = isinstance(value, type(default))
            if not valid:
                raise ValueError(f"Invalid type: {section}.{key}")
    for section, key, low, high in [
        ("capture", "frame_seconds", 1, 300), ("capture", "change_threshold", 0, 1),
        ("capture", "duplicate_threshold", 0, 1), ("capture", "max_hours", 0.001, 24),
        ("capture", "min_free_mb", 1, 1000000), ("capture", "stall_seconds", 5, 600),
        ("asr", "chunk_seconds", 1, 1800), ("asr", "threads", 1, 256),
        ("asr", "timeout_seconds", 1, 86400), ("asr", "uncertain_probability", 0, 1),
        ("schedule", "check_minutes", 1, 120),
    ]:
        if not low <= out[section][key] <= high:
            raise ValueError(f"Out of range: {section}.{key} ({low}..{high})")
    for section, key in [("capture", "frame_seconds"), ("asr", "chunk_seconds"),
                         ("asr", "threads"), ("schedule", "check_minutes")]:
        if type(out[section][key]) is not int:
            raise ValueError(f"Expected an integer: {section}.{key}")
    crop = out["capture"]["crop"]
    if len(crop) != 4 or any(type(n) is not int or n < 0 for n in crop):
        raise ValueError("capture.crop must be [x, y, width, height], nonnegative integers")
    if bool(crop[2]) != bool(crop[3]):
        raise ValueError("Specify both crop width and height, or both zero")
    for key in ("include", "exclude"):
        if not all(isinstance(x, str) for x in out["schedule"][key]):
            raise ValueError(f"schedule.{key} must contain strings")
    if out["report"]["kind"] not in ("lecture", "meeting"):
        raise ValueError("report.kind must be lecture or meeting")
    if out["report"]["detail"] not in ("brief", "detailed"):
        raise ValueError("report.detail must be brief or detailed")
    if out["capture"]["backend"] not in ("windows", "test"):
        raise ValueError("Implemented capture backends: windows, test")
    return out


def load_profile(path: Path) -> dict:
    path = path.resolve()
    with path.open("rb") as f:
        p = validate(tomllib.load(f))
    for section, key in [("storage", "root"), ("browser", "directory"), ("asr", "model")]:
        value = p[section][key]
        if value:
            p[section][key] = str((path.parent / Path(value).expanduser()).resolve())
    for section, key in [("capture", "gst"), ("capture", "inspect"),
                         ("asr", "executable"), ("browser", "executable")]:
        value = p[section][key]
        if "/" in value or "\\" in value:
            p[section][key] = str((path.parent / Path(value).expanduser()).resolve())
    return p
