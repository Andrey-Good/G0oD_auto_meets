"""Synthetic GStreamer sources for smoke tests. Never captures desktop or microphone."""
from contextlib import nullcontext

ELEMENTS = ["audiotestsrc", "videotestsrc"]


def preflight(profile, target):
    return None


def sources(profile, target):
    return [["audiotestsrc", "is-live=true", "wave=sine", "volume=0.03"]], [
        "videotestsrc", "is-live=true", "pattern=ball"]


def awake():
    return nullcontext()
