"""Independent quality checks on a rendered clip. Each check returns pass/fail with the measured value.

A clip is publishable only if every CRITICAL check passes. Failures marked recoverable trigger an automatic re-render
with adjusted settings (see pipeline.render_with_retry).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

DANGLING_END = {"and", "but", "so", "the", "a", "an", "to", "of", "because", "with", "for", "aur", "lekin", "toh",
                "ke", "ki", "ko", "ka", "mein", "se", "kyunki", "और", "लेकिन", "तो", "के", "की", "को", "का", "में", "से",
                "कि", "क्योंकि"}
CRITICAL = {"file_exists", "playable", "resolution", "codec", "duration_match", "black_frames", "audio_present",
            "audio_clipping", "loudness", "captions_in_bounds", "captions_overlap", "content_complete",
            "not_misleading"}


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stderr


def check(path: Path, expected_dur: float, ass_path: Path | None, clip_text: str, mapped_words: list[dict],
          misleading: bool = False, target_lufs: float = -14.0) -> dict:
    res: dict[str, dict] = {}

    def put(name: str, ok: bool, value, recoverable: bool = False, note: str = ""):
        res[name] = {"ok": bool(ok), "value": value, "recoverable": recoverable, "note": note}

    put("file_exists", path.exists() and path.stat().st_size > 10_000, path.stat().st_size if path.exists() else 0)
    if not res["file_exists"]["ok"]:
        return _summary(res)
    pr = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                        capture_output=True, text=True)
    put("playable", pr.returncode == 0, pr.returncode)
    if pr.returncode:
        return _summary(res)
    meta = json.loads(pr.stdout)
    v = next((s for s in meta["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in meta["streams"] if s["codec_type"] == "audio"), None)
    dur = float(meta["format"]["duration"])
    put("resolution", (v.get("width"), v.get("height")) == (1080, 1920), f"{v.get('width')}x{v.get('height')}")
    put("codec", v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p" and (a or {}).get("codec_name") == "aac",
        f"{v.get('codec_name')}/{v.get('pix_fmt')}/{(a or {}).get('codec_name')}")
    put("duration_match", abs(dur - expected_dur) < 0.35, round(dur, 2), True, f"expected {expected_dur:.2f}")
    put("duration_limits", 3 <= dur <= 180, round(dur, 2), note="YouTube Shorts accept up to 3 minutes")
    put("audio_present", a is not None, bool(a))

    bd = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", "blackdetect=d=0.3:pix_th=0.08", "-an", "-f", "null", "-"])
    blacks = [float(x) for x in re.findall(r"black_duration:([\d.]+)", bd)]
    put("black_frames", sum(blacks) < 0.3, round(sum(blacks), 2), True)
    fz = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-vf", "freezedetect=n=0.001:d=2.5", "-an", "-f", "null", "-"])
    freezes = len(re.findall(r"freeze_start", fz))
    put("frozen_video", freezes == 0, freezes, note="frozen picture over 2.5 s (corrupt or static source)")

    if a is not None:
        lo = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"])
        i_m = re.findall(r"I:\s+(-?[\d.]+) LUFS", lo)
        p_m = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", lo)
        lufs = float(i_m[-1]) if i_m else -99.0
        peak = float(p_m[-1]) if p_m else 0.0
        put("loudness", abs(lufs - target_lufs) <= 2.0, lufs, True, f"target {target_lufs}")
        put("audio_clipping", peak <= -0.5, peak, True, "true peak dBFS")
        sd = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "silencedetect=n=-40dB:d=1.2", "-vn", "-f", "null", "-"])
        sil = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", sd)]
        put("unexpected_silence", not sil, sil[:5], True, "silence over 1.2 s inside the clip")

    if ass_path and Path(ass_path).exists():
        ev = re.findall(r"^Dialogue: 0,([\d:.]+),([\d:.]+),Cap,", Path(ass_path).read_text(encoding="utf-8"), re.M)
        spans = [(_secs(s), _secs(e)) for s, e in ev]
        overl = sum(1 for k in range(len(spans) - 1) if spans[k][1] > spans[k + 1][0] + 0.01)
        oob = sum(1 for s, e in spans if s < 0 or e > dur + 0.2 or e <= s)
        put("captions_overlap", overl == 0, overl)
        put("captions_in_bounds", oob == 0 and bool(spans), {"cards": len(spans), "out_of_bounds": oob})
        low_conf = [w["w"] for w in mapped_words if w.get("p", 1) < 0.35]
        put("caption_confidence", len(low_conf) <= max(1, len(mapped_words) // 8), low_conf[:8],
            note="words the recogniser was unsure about; check spelling")
    else:
        put("captions_in_bounds", False, None, True, "caption file missing")

    t = clip_text.strip()
    last = (t.split() or [""])[-1].lower().strip(",")
    ends_open = last in DANGLING_END or t.endswith(",")
    put("content_complete", not ends_open, t[-40:],
        note="clip must not end on a connective or mid-clause (language-agnostic; transcripts often lack full stops)")
    put("not_misleading", not misleading, misleading, note="model flagged that clipping changes the meaning")
    return _summary(res)


def _secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _summary(res: dict) -> dict:
    failed = [k for k, v in res.items() if not v["ok"]]
    crit = [k for k in failed if k in CRITICAL]
    return {"checks": res, "failed": failed, "critical_failed": crit, "pass": not crit,
            "recoverable": bool(crit) and all(res[k]["recoverable"] for k in crit)}
