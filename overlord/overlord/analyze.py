"""Content understanding: sentences, vocal energy, speech rate, topic boundaries and lexical cues.

Everything here is measured from the transcript and audio, so each later score can be explained by a number.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

END_PUNCT = re.compile(r"[.!?।]$")
TOKEN = re.compile(r"[\wऀ-ॿ']+", re.UNICODE)

# Lexical cue lists (English + Hinglish/Hindi). They feed explainable features, not a verdict.
CUES = {
    "contrast": {"but", "however", "actually", "instead", "except", "lekin", "par", "magar", "balki", "लेकिन", "पर"},
    "surprise": {"shocking", "crazy", "insane", "unbelievable", "secret", "nobody", "never", "suddenly", "wow",
                 "sach", "achanak", "pagal", "kabhi", "koi", "नहीं", "कभी", "सच"},
    "stakes": {"money", "million", "lakh", "crore", "rupees", "dollars", "salary", "free", "lost", "won", "fired",
               "paisa", "paise", "लाख", "करोड़", "रुपये", "फ्री"},
    "emotion": {"love", "hate", "scared", "cried", "angry", "happy", "sad", "pain", "proud", "dar", "pyaar", "gussa",
                "dukh", "khush", "रोया", "डर"},
    "instruction": {"how", "step", "first", "second", "tip", "mistake", "avoid", "should", "must", "kaise", "galti",
                    "zaroor", "ज़रूर", "कैसे", "गलती"},
    "address": {"you", "your", "tum", "tumhe", "aap", "aapko", "तुम", "तुम्हें", "आप", "आपको"},
    "opinion": {"i think", "honestly", "believe", "overrated", "underrated", "wrong", "truth", "mera manna", "sach"},
}
FILLERS = {"um", "uh", "erm", "hmm", "like", "basically", "matlab", "toh", "na", "accha"}


@dataclass
class Sentence:
    i: int
    s: float
    e: float
    text: str
    words: list = field(default_factory=list)
    energy_db: float = -60.0
    rate: float = 0.0  # words per second
    cues: dict = field(default_factory=dict)
    question: bool = False
    numbers: int = 0
    filler_ratio: float = 0.0
    topic_id: int = 0
    conf: float = 1.0


CLAUSE_START = {"so", "and", "but", "because", "then", "which", "where", "to", "toh", "aur", "lekin", "jahan",
                "jiske", "jisme", "kyunki", "phir", "तो", "और", "लेकिन", "जहाँ", "जिसके", "जिसमें", "क्योंकि", "फिर"}


def boundary_dips(words: list[dict], wav: Path, below_db: float = 10.0) -> set[int]:
    """Indexes k where the audio between word k and k+1 drops well below the speaker's level: a breath or pause
    the recogniser's back-to-back word timings hide."""
    db, hop = energy_track(wav)
    if len(db) < 10:
        return set()
    speech = float(np.median(db[db > np.percentile(db, 30)]))
    out = set()
    for k in range(len(words) - 1):
        t = words[k]["e"]
        a, b = max(0, int((t - 0.06) / hop)), int((t + 0.08) / hop) + 1
        if b > a and float(np.min(db[a:b])) < speech - below_db:
            out.add(k)
    return out


def sentences(words: list[dict], max_gap: float = 0.8, max_len: float = 12.0, soft_len: float = 4.5,
              soft_gap: float = 0.12, dips: set[int] | None = None) -> list[Sentence]:
    """Split words into speech units. Breaks at sentence punctuation and long pauses; because many recognisers
    (Hindi especially) emit little punctuation, once a unit passes soft_len it also breaks at the next natural pause
    (time gap, audio dip, comma) or before a clause-starting word."""
    dips = dips or set()
    out, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        dur = cur[-1]["e"] - cur[0]["s"]
        nxt_clause = bool(nxt) and nxt["w"].lower().strip(",.") in CLAUSE_START
        soft = ((dur > 2.0 and nxt_clause) or (dur > soft_len and (gap >= soft_gap or w["w"].endswith(",")))
                or (dur > 7.0 and k in dips))
        if END_PUNCT.search(w["w"]) or gap > max_gap or soft or dur > max_len or nxt is None:
            text = " ".join(x["w"] for x in cur)
            out.append(Sentence(len(out), cur[0]["s"], cur[-1]["e"], text, list(cur),
                                conf=float(np.mean([x.get("p", 1) for x in cur]))))
            cur = []
    return out


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text)]


def energy_track(wav: Path, hop: float = 0.05) -> tuple[np.ndarray, float]:
    import soundfile as sf

    x, sr = sf.read(str(wav), dtype="float32")
    if x.ndim > 1:
        x = x.mean(axis=1)
    n = int(sr * hop)
    frames = len(x) // n
    if frames == 0:
        return np.array([-60.0]), hop
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n) ** 2, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9), hop


def annotate(sents: list[Sentence], wav: Path) -> dict:
    db, hop = energy_track(wav)
    voiced = db[db > np.percentile(db, 30)] if len(db) > 10 else db
    base = float(np.median(voiced)) if len(voiced) else -30.0
    for s in sents:
        a, b = int(s.s / hop), max(int(s.s / hop) + 1, int(s.e / hop))
        seg = db[a:b]
        s.energy_db = float(np.percentile(seg, 75)) - base if len(seg) else 0.0  # relative to the speaker's median
        dur = max(s.e - s.s, 0.3)
        toks = _tokens(s.text)
        s.rate = len(s.words) / dur
        low = " " + " ".join(toks) + " "
        s.cues = {k: sum(1 for c in v if (" " + c + " ") in low) for k, v in CUES.items()}
        s.question = "?" in s.text
        s.numbers = len(re.findall(r"\d", s.text))
        s.filler_ratio = (sum(1 for t in toks if t in FILLERS) / len(toks)) if toks else 0.0
    _topics(sents)
    return {"energy_baseline_db": base, "sentences": len(sents)}


def tfidf_vectors(texts: list[str]) -> np.ndarray:
    docs = [Counter(_tokens(t)) for t in texts]
    vocab = {w: i for i, w in enumerate(sorted({w for d in docs for w in d}))}
    if not vocab:
        return np.zeros((len(texts), 1))
    df = Counter(w for d in docs for w in d)
    n = len(docs)
    m = np.zeros((n, len(vocab)), dtype=np.float32)
    for r, d in enumerate(docs):
        for w, c in d.items():
            m[r, vocab[w]] = (1 + math.log(c)) * math.log((1 + n) / (1 + df[w]) + 1)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.maximum(norms, 1e-9)


def _topics(sents: list[Sentence], window: int = 4, threshold: float = 0.08) -> None:
    """TextTiling-style topic boundaries: low lexical similarity between the windows before and after a sentence
    marks a topic change."""
    if len(sents) < 2 * window:
        return
    texts = [s.text for s in sents]
    tid = 0
    for i in range(len(sents)):
        if window <= i <= len(sents) - window:
            v = tfidf_vectors([" ".join(texts[i - window:i]), " ".join(texts[i:i + window])])
            if float(v[0] @ v[1]) < threshold and i - _last_boundary(sents, i) >= window:
                tid += 1
        sents[i].topic_id = tid


def _last_boundary(sents: list[Sentence], i: int) -> int:
    t = sents[i - 1].topic_id
    j = i - 1
    while j > 0 and sents[j - 1].topic_id == t:
        j -= 1
    return j
