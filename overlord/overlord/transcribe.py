"""Speech to word-level timestamps with faster-whisper (local, CPU or GPU).

Output: {"language": "hi", "words": [{"w","s","e","p"}...], "duration": float}
Speaker diarisation is NOT done here (see docs/overlord/ROADMAP.md); clips are selected from one transcript stream.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

_MODEL_CACHE: dict = {}


def normalize_words(words: list[dict]) -> list[dict]:
    """Rejoin numbers the recogniser splits into pieces: '30', '-60', ',000' -> '30-60,000'."""
    import re

    out: list[dict] = []
    for w in words:
        if out and re.match(r"^[-,.:/]\d", w["w"]) and re.search(r"\d$", out[-1]["w"]):
            prev = out[-1]
            out[-1] = {**prev, "w": prev["w"] + w["w"], "e": w["e"], "p": min(prev.get("p", 1), w.get("p", 1))}
        else:
            out.append(dict(w))
    return out


def extract_audio(media: Path, out: Path) -> Path:
    if not out.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000",
                        str(out)], check=True)
    return out


def transcribe(media: Path, workdir: Path, model: str = "small", compute: str = "int8", language: str | None = None,
               prompt: str | None = None) -> dict:
    from faster_whisper import WhisperModel

    wav = extract_audio(media, workdir / "audio16k.wav")
    key = (model, compute)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(model, device="auto", compute_type=compute)
    m = _MODEL_CACHE[key]
    t0 = time.time()
    segs, info = m.transcribe(str(wav), language=language, word_timestamps=True, vad_filter=True,
                              initial_prompt=prompt or None, condition_on_previous_text=False, beam_size=5)
    words = []
    for s in segs:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                words.append({"w": t, "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
    return {"language": info.language, "language_p": round(info.language_probability, 3), "words": normalize_words(words),
            "duration": round(info.duration, 3), "seconds": round(time.time() - t0, 1), "model": model}
