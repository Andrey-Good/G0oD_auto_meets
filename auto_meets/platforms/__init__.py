"""Platform boundary: preflight(profile, target), sources(profile, target), awake()."""
from importlib import import_module

BACKENDS = {"windows": "windows", "test": "test"}


def get_backend(name: str):
    if name not in BACKENDS:
        raise ValueError(f"Unsupported capture backend: {name}")
    return import_module(f"auto_meets.platforms.{BACKENDS[name]}")
