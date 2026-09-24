import copy
from pathlib import Path

import pytest

from auto_meets.audio import new_audio, repair_audio
from auto_meets.config import DEFAULTS
from auto_meets.storage import new_session


@pytest.fixture
def profile(tmp_path):
    p = copy.deepcopy(DEFAULTS)
    p["storage"]["root"] = str(tmp_path / "data")
    p["capture"].update(backend="test", frame_seconds=1)
    p["asr"].update(model=str(tmp_path / "model.bin"), chunk_seconds=1)
    Path(p["asr"]["model"]).write_bytes(b"fake model used only by tests")
    return p


@pytest.fixture
def session(profile):
    folder = new_session(profile, "Test meeting")
    new_audio(folder / "audio.wav")
    with (folder / "audio.wav").open("ab") as f:
        f.write(b"\1\0" * 40000)  # 2.5 seconds, including a short final chunk.
    repair_audio(folder / "audio.wav")
    return folder
