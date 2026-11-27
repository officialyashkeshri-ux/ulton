"""Per-clip YouTube metadata. Uses Claude when configured; otherwise a transparent extractive fallback built only
from the clip's own words (never invented claims). Output is validated against YouTube limits either way."""
from __future__ import annotations

import re

from .analyze import CUES, _tokens

STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "on", "is", "it", "that", "this", "for", "you", "i", "we",
        "hai", "ka", "ki", "ke", "ko", "se", "me", "mein", "aur", "to", "toh", "bhi", "ye", "wo", "hi", "na", "kya",
        "है", "का", "की", "के", "को", "से", "में", "और", "तो", "भी", "ये", "वो", "ही", "ना", "क्या"}


def units(text: str) -> list[str]:
    """Phrases: split at sentence punctuation and before clause-starting words (recognisers often omit
    punctuation, Hindi especially)."""
    from .analyze import CLAUSE_START

    out, cur = [], []
    for w in text.split():
        if cur and (w.lower().strip(",.") in CLAUSE_START and len(cur) >= 4):
            out.append(" ".join(cur)); cur = []
        cur.append(w)
        if re.search(r"[.!?।]$", w):
            out.append(" ".join(cur)); cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def lead_phrase(text: str) -> str:
    """The phrase most likely to stop a scroll: numbers and stakes/surprise cues, early in the clip."""
    us = units(text) or [text]
    cue_words = {c for g in CUES.values() for c in g}

    def rank(i_u):
        i, u = i_u
        toks = _tokens(u)
        return (sum(t in cue_words for t in toks) + 2 * len(re.findall(r"\d+", u)) - 0.3 * i, -abs(len(toks) - 9))
    return max(enumerate(us[:5]), key=rank)[1]


def fallback(clip_text: str, glossary: list[str], hashtags: list[str], channel: str = "") -> dict:
    lead = lead_phrase(clip_text)
    title = lead.strip().rstrip(".")
    if len(title) > 68:
        title = title[:68].rsplit(" ", 1)[0] + "…"
    toks = [t for t in _tokens(clip_text) if t not in STOP and len(t) > 2]
    freq: dict[str, int] = {}
    for t in toks:
        freq[t] = freq.get(t, 0) + 1
    tags = [g for g in glossary][:5] + [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])][:8]
    desc = (clip_text[:280].rsplit(" ", 1)[0] + "…") if len(clip_text) > 280 else clip_text
    if channel:
        desc += f"\n\nFull video on {channel}."
    return validate({"title": title, "description": desc, "tags": tags, "hashtags": hashtags,
                     "hook_text": _hook(lead), "source": "extractive"})


def _hook(s: str) -> str:
    words = s.split()
    return " ".join(words[:7]) + ("…" if len(words) > 7 else "")


def validate(m: dict, avoid: list[str] | None = None) -> dict:
    avoid = [a.lower() for a in (avoid or [])]
    title = re.sub(r"[<>]", "", m.get("title", "")).strip()[:100]
    desc = re.sub(r"[<>]", "", m.get("description", ""))
    tags, total = [], 0
    for t in dict.fromkeys(x.strip().lstrip("#") for x in m.get("tags", []) if x and x.strip()):
        t = re.sub(r"[<>,]", "", t)[:60]
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > 480 or t.lower() in avoid:
            continue
        tags.append(t)
        total += cost
    hashtags = []
    for h in m.get("hashtags", []):
        h = "#" + re.sub(r"[^\w]", "", h.lstrip("#"))
        if len(h) > 1 and h.lower() not in (x.lower() for x in hashtags):
            hashtags.append(h)
    hashtags = hashtags[:4]
    if hashtags and not any(h in desc for h in hashtags):
        desc = desc.rstrip() + "\n\n" + " ".join(hashtags)
    while len(desc.encode("utf-8")) > 4900:
        desc = desc[:-50]
    for a in avoid:
        if a and (a in title.lower() or a in desc.lower()):
            raise ValueError(f"metadata contains avoided term {a!r}")
    return {**m, "title": title or "Clip", "description": desc, "tags": tags, "hashtags": hashtags}
