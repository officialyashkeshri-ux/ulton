"""Clip rendering: 1080x1920 H.264/AAC vertical video from a source moment.

Steps (each serves clarity or retention, nothing decorative):
  1. tighten: silences longer than pacing.max_gap inside the clip shrink to pacing.keep_gap (word-boundary cuts only)
  2. reframe: faces are detected per kept segment (OpenCV Haar cascade on sampled frames); each segment's 9:16 crop
     is centred on the median face position, so the camera only moves at cuts (natural jump-cut reframing, no
     jitter). No face found in most samples -> "fit" layout (whole frame over a blurred fill) instead of a blind crop
  3. punch-in: optional subtle scale-up on alternating segments longer than punch_in.every_s
  4. captions: word-timed ASS subtitles, max_words per card, one highlighted keyword per card, safe-zone positioning
  5. hook title: short text at the top for the first seconds
  6. audio: optional FFT denoise + EBU R128 loudness normalisation to the profile target
"""
from __future__ import annotations

import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H = 1080, 1920
FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"


@dataclass
class Seg:
    a: float  # source start
    b: float  # source end
    cx: float = 0.5  # crop centre, fraction of source width
    face: bool = False
    zoom: float = 1.0


def keep_segments(words: list[dict], s: float, e: float, max_gap: float, keep_gap: float) -> list[Seg]:
    # a word belongs to the clip if its midpoint is inside the (padded) range; keeps trimmed lead-ins out
    ws = [w for w in words if s + 0.1 <= (w["s"] + w["e"]) / 2 <= e - 0.05]
    if not ws:
        return [Seg(s, e)]
    segs, a = [], max(s, ws[0]["s"] - 0.08)
    for k in range(len(ws) - 1):
        gap = ws[k + 1]["s"] - ws[k]["e"]
        if gap > max_gap:
            segs.append(Seg(a, ws[k]["e"] + keep_gap / 2))
            a = ws[k + 1]["s"] - keep_gap / 2
    segs.append(Seg(a, min(e, ws[-1]["e"] + 0.2)))
    return [x for x in segs if x.b - x.a > 0.05]


YUNET = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"


def _faces_x(video: Path, times: list[float]) -> list[float | None]:
    """Horizontal centre (0..1) of the largest confident face at each time, via OpenCV's YuNet detector."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    det = None
    out = []
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            out.append(None)
            continue
        h, w = frame.shape[:2]
        scale = 640 / max(w, h)
        small = cv2.resize(frame, (int(w * scale), int(h * scale)))
        if det is None:
            det = cv2.FaceDetectorYN.create(str(YUNET), "", (small.shape[1], small.shape[0]), 0.7, 0.3, 50)
        _, faces = det.detect(small)
        if faces is not None and len(faces):
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
            out.append(float((x + fw / 2) / small.shape[1]))
        else:
            out.append(None)
    cap.release()
    return out


def reframe(video: Path, segs: list[Seg], src_w: int, src_h: int) -> str:
    """Fill each segment's crop centre. Returns layout: 'native' (already vertical), 'crop' or 'fit'."""
    if src_w / src_h <= 9 / 16 + 0.02:
        return "native"
    found = total = 0
    for sg in segs:
        n = max(2, min(6, int((sg.b - sg.a) / 0.75)))
        xs = _faces_x(video, list(np.linspace(sg.a + 0.05, sg.b - 0.05, n)))
        hits = [x for x in xs if x is not None]
        total += len(xs)
        found += len(hits)
        if hits:
            sg.cx, sg.face = float(np.median(hits)), True
    if total and found / total < 0.4:
        return "fit"
    last = 0.5
    for sg in segs:  # segments without a face keep the previous framing instead of jumping to centre
        if sg.face:
            last = sg.cx
        else:
            sg.cx = last
    return "crop"


def _hex_ass(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _t(x: float) -> str:
    x = max(0.0, x)
    return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"


def map_words(words: list[dict], segs: list[Seg]) -> list[dict]:
    """Translate source-time words onto the edited (tightened) timeline."""
    out, offset = [], 0.0
    for sg in segs:
        for w in words:
            if w["s"] >= sg.a - 0.02 and w["e"] <= sg.b + 0.02:
                out.append({**w, "s": w["s"] - sg.a + offset, "e": min(w["e"], sg.b) - sg.a + offset})
        offset += sg.b - sg.a
    return out


DEVANAGARI = re.compile(r"[ऀ-ॿ]")
SCRIPT_FONTS = {"devanagari": "Noto Sans Devanagari"}


def script_font(text: str, default: str) -> str:
    """Caption fonts rarely cover every script; switch to a font that has the glyphs actually being shown."""
    return SCRIPT_FONTS["devanagari"] if DEVANAGARI.search(text or "") else default


def captions_ass(words: list[dict], prof: dict, hook_text: str | None, total: float, keyword_fn=None) -> str:
    cap, hook = dict(prof["captions"]), dict(prof["hook_title"])
    all_text = " ".join(w["w"] for w in words)
    font = script_font(all_text, cap["font"])
    if font != cap["font"]:  # script fonts draw smaller glyphs at the same point size
        cap["size"] = int(cap["size"] * 1.1)
    hook["font"] = script_font(hook_text or "", hook["font"])
    margin_v = int(H * (1 - cap["position_y"]))
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Cap,{font},{cap['size']},{_hex_ass(cap['color'])},&H00FFFFFF,{_hex_ass(cap['outline'])},"
            f"&H64000000,-1,0,0,0,100,100,0,0,1,{cap['outline_px']},2,2,90,90,{margin_v},1\n"
            f"Style: Hook,{hook['font']},{hook['size']},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,"
            f"0,1,8,0,8,80,80,{int(H * hook['position_y'])},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    if hook.get("enabled") and hook_text:
        # seconds < 0 keeps the title on screen for the whole clip (e.g. campaign "on-screen text must mention X")
        end = total if hook.get("seconds", 2.5) < 0 else min(hook["seconds"], total)
        lines.append(f"Dialogue: 1,{_t(0)},{_t(end)},Hook,,0,0,0,,{{\\fad(80,150)}}"
                     f"{_wrap(hook_text, 18)}")
    hl = _hex_ass(cap["highlight"])
    n = max(1, int(cap["max_words"]))
    groups, cur = [], []
    for w in words:  # a card breaks at max_words, sentence ends, or pauses
        if cur and (len(cur) >= n or w["s"] - cur[-1]["e"] > 0.4 or re.search(r"[.!?।,]$", cur[-1]["w"])):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    for gi, g in enumerate(groups):
        s = g[0]["s"]
        e = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else g[-1]["e"] + 0.3
        e = min(e, g[-1]["e"] + 0.6, total)
        if e <= s:
            continue
        key = keyword_fn(g) if keyword_fn else max(range(len(g)), key=lambda k: len(_clean(g[k]["w"])))
        parts = []
        for k, w in enumerate(g):
            word = _clean(w["w"])
            word = word.upper() if cap.get("uppercase") else word
            parts.append(f"{{\\c{hl}}}{word}{{\\c{_hex_ass(cap['color'])}}}" if k == key else word)
        text = " ".join(parts)
        if len(g) >= 3:  # two lines max: break after the middle word
            mid = len(g) // 2
            text = " ".join(parts[:mid + 1]) + "\\N" + " ".join(parts[mid + 1:])
        lines.append(f"Dialogue: 0,{_t(s)},{_t(e)},Cap,,0,0,0,,{{\\fscx104\\fscy104\\t(0,90,\\fscx100\\fscy100)}}{text}")
    return head + "\n".join(lines) + "\n"


def _clean(w: str) -> str:
    return w.strip().strip(",").replace("{", "").replace("}", "")


def _wrap(text: str, width: int) -> str:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    lines.append(cur)
    return "\\N".join(lines[:2])


def render(video: Path, words: list[dict], s: float, e: float, prof: dict, out: Path, hook_text: str | None,
           src_w: int, src_h: int, keyword_fn=None) -> dict:
    pace = prof["pacing"]
    segs = keep_segments(words, s, e, pace["max_gap"], pace["keep_gap"])
    layout = reframe(video, segs, src_w, src_h)
    pin = prof.get("punch_in", {})
    if pin.get("enabled"):
        for k, sg in enumerate(segs):
            if k % 2 == 1 and sg.b - sg.a >= 1.2:
                sg.zoom = float(pin.get("scale", 1.1))
    total = sum(sg.b - sg.a for sg in segs)
    mapped = map_words(words, segs)
    ass = out.with_suffix(".ass")
    ass.write_text(captions_ass(mapped, prof, hook_text, total, keyword_fn), encoding="utf-8")

    fc, n = [], len(segs)
    for i, sg in enumerate(segs):
        v = f"[0:v]trim={sg.a:.3f}:{sg.b:.3f},setpts=PTS-STARTPTS"
        if layout == "crop":
            cw = int(src_h * 9 / 16 / sg.zoom) // 2 * 2
            ch = int(src_h / sg.zoom) // 2 * 2
            x = int(min(max(sg.cx * src_w - cw / 2, 0), src_w - cw))
            y = int(min(max(src_h * 0.38 - ch * 0.38, 0), src_h - ch))
            v += f",crop={cw}:{ch}:{x}:{y},scale={W}:{H}:flags=lanczos,setsar=1[v{i}]"
        elif layout == "native":
            cw, ch = int(src_w / sg.zoom) // 2 * 2, int(src_h / sg.zoom) // 2 * 2
            v += f",crop={cw}:{ch}:{(src_w - cw) // 2}:{int((src_h - ch) * 0.38)},scale={W}:{H}:flags=lanczos,setsar=1[v{i}]"
        else:  # fit: full frame centred over a blurred, zoomed copy of itself
            v += (f",split[f{i}][b{i}];[b{i}]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                  f"boxblur=24:2[bb{i}];[f{i}]scale={W}:-2:flags=lanczos[ff{i}];[bb{i}][ff{i}]overlay=0:(H-h)/2,"
                  f"setsar=1[v{i}]")
        fc.append(v)
        fade = min(0.012, (sg.b - sg.a) / 4)
        fc.append(f"[0:a]atrim={sg.a:.3f}:{sg.b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d={fade:.3f},"
                  f"afade=t=out:st={sg.b - sg.a - fade:.3f}:d={fade:.3f}[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[cv][ca]")
    fonts = f":fontsdir={FONTS_DIR}" if FONTS_DIR.exists() else ""
    fc.append(f"[cv]subtitles={ass.name}{fonts}[ov]")
    audio = prof.get("audio", {})
    achain = ("afftdn=nf=-25," if audio.get("denoise") else "") + \
             f"loudnorm=I={audio.get('loudness_lufs', -14)}:TP=-1.5:LRA=11,aresample=48000"
    fc.append(f"[ca]{achain}[oa]")
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-filter_complex", ";".join(fc), "-map", "[ov]", "-map",
           "[oa]", "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-r", "30", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", out.name]
    r = subprocess.run(cmd, cwd=out.parent, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg render failed: {r.stderr.strip()[-600:]}")
    thumb = out.with_suffix(".jpg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{min(1.0, total / 3):.2f}", "-i", str(out), "-frames:v",
                    "1", "-q:v", "3", str(thumb)], check=False)
    return {"segments": [(round(x.a, 3), round(x.b, 3), round(x.cx, 3), x.zoom) for x in segs], "layout": layout,
            "duration": round(total, 3), "words": mapped, "ass": str(ass), "thumb": str(thumb) if thumb.exists() else None,
            "cuts": max(0, n - 1), "faces_found": sum(sg.face for sg in segs)}


def keyword_picker(glossary: list[str]):
    """Highlight priority: glossary names > numbers > cue words > longest word."""
    from .analyze import CUES

    cue_words = {w for v in CUES.values() for w in v}
    gl = {g.lower() for g in glossary}

    def pick(group: list[dict]) -> int:
        def rank(k: int) -> tuple:
            w = _clean(group[k]["w"]).lower().strip(".!?")
            return (w in gl, bool(re.search(r"\d", w)), w in cue_words, len(w))
        return max(range(len(group)), key=rank)
    return pick


def probe_video(path: Path) -> tuple[int, int, float]:
    from .ingest import ffprobe

    meta = ffprobe(path)
    v = next(s for s in meta["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    rot = int(v.get("tags", {}).get("rotate", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    return w, h, float(meta["format"].get("duration", 0) or 0)


def math_isfinite(x: float) -> bool:  # tiny helper used by qc
    return math.isfinite(x)
