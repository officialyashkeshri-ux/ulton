"""Shared fixtures. Media fixtures are generated locally (espeak-ng speech over an ffmpeg test pattern), so tests
need no downloads and use no third-party footage."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = [
    "Nobody tells you this, but the first job you take matters less than the skills you build there.",
    "I lost two lakh rupees on my first business. Here is the mistake I made.",
    "I trusted a supplier without a written contract. Never do that.",
    "So what should you do instead? Write everything down, even with friends.",
    "Let's talk about something different now. Morning routines are overrated.",
    "What actually works is a fixed time to start, not a perfect routine.",
    "I tested this for ninety days and my output doubled.",
    "Here is a practical tip. Put your phone in another room for the first hour.",
    "That one change saved me three hours every single day.",
    "And honestly, the biggest secret is consistency, not motivation.",
]


def _need(tool: str):
    if not shutil.which(tool):
        pytest.skip(f"{tool} not installed")


@pytest.fixture(scope="session")
def speech_video(tmp_path_factory) -> Path:
    _need("espeak-ng"); _need("ffmpeg")
    d = tmp_path_factory.mktemp("media")
    parts = []
    for i, line in enumerate(SCRIPT):
        w = d / f"l{i}.wav"
        subprocess.run(["espeak-ng", "-s", "150", "-w", str(w), line], check=True)
        parts.append(w)
        sil = d / f"s{i}.wav"  # a pause between sentences (cut later by the pacing step)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono", "-t",
                        "0.9" if i % 3 else "1.6", str(sil)], check=True)
        parts.append(sil)
    lst = d / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    wav = d / "speech.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ar", "44100",
                    str(wav)], check=True)
    out = d / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30", "-i", str(wav),
                    "-shortest", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
                   check=True)
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERLORD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("OVERLORD_WHISPER_MODEL", os.environ.get("OVERLORD_TEST_WHISPER", "tiny"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    from overlord.config import settings
    from overlord.db import DB

    st = settings()
    return st, DB(st.db_path)
