"""Candidate generation, 12-dimension explainable scoring, ranking and de-duplication.

Scores are proxies computed from measurable features (vocal energy, cue words, questions, numbers, sentence
completeness, speech rate, lexical novelty). They are labelled "heuristic". When an Anthropic credential is present,
llm.py adds a model judgement for the semantic dimensions and the final score blends both, with the source of every
number recorded. Weights are per-client (profile["score_weights"]) and later adjusted from real analytics
(learning.py), never assumed universal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .analyze import CLAUSE_START, Sentence, tfidf_vectors

DIMENSIONS = ["hook", "curiosity", "relevance", "emotion", "information", "completeness", "retention", "shareability",
              "replay", "standalone", "originality", "pacing"]
DEFAULT_WEIGHTS = {"hook": 1.6, "curiosity": 1.3, "relevance": 0.8, "emotion": 1.0, "information": 1.0,
                   "completeness": 1.4, "retention": 1.2, "shareability": 0.9, "replay": 0.6, "standalone": 1.5,
                   "originality": 0.7, "pacing": 0.8}
DANGLING_START = {"and", "but", "so", "because", "which", "that", "aur", "lekin", "toh", "kyunki", "jo", "जो",
                  "और", "लेकिन", "तो", "जिसके", "jiske", "isliye", "it", "this", "he", "she", "they", "woh", "वो"} | CLAUSE_START


@dataclass
class Candidate:
    start_i: int
    end_i: int
    s: float
    e: float
    text: str
    scores: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)
    total: float = 0.0
    source: str = "heuristic"

    @property
    def dur(self) -> float:
        return self.e - self.s


# Leading connectives an editor would simply trim ("So...", "And...", "जहाँ पर..."): cutting them does not change
# what is said. Contrast/cause words (but, because, lekin, kyunki) are NOT trimmed, since dropping them can change
# the meaning; those starts stay penalised instead.
TRIMMABLE_LEAD = {"and", "so", "then", "aur", "toh", "to", "तो", "और", "फिर", "phir", "जहाँ", "jahan", "जिसके",
                  "jiske", "जिसमें", "jisme"}
TRIM_FOLLOWERS = {"पर", "par", "लिए", "liye", "में", "mein"}


def _trim_lead(words: list[dict]) -> int:
    k = 0
    while k < min(3, len(words) - 4):
        t = words[k]["w"].lower().strip(",.")
        if t in TRIMMABLE_LEAD or (k > 0 and t in TRIM_FOLLOWERS and words[k - 1]["w"].lower() in TRIMMABLE_LEAD):
            k += 1
        else:
            break
    return k


def generate(sents: list[Sentence], min_len: float, max_len: float, pad: float = 0.15) -> list[Candidate]:
    """All sentence-aligned windows whose length is within [min_len, max_len]. Clips start and end on sentence
    boundaries, so no clip begins or ends mid-word or mid-sentence; trimmable leading connectives are cut."""
    out = []
    for a in range(len(sents)):
        for b in range(a, len(sents)):
            dur = sents[b].e - sents[a].s
            if dur > max_len:
                break
            if dur >= min_len:
                words = [w for x in sents[a:b + 1] for w in x.words]
                k = _trim_lead(words) if words else 0
                start = words[k]["s"] if k else sents[a].s
                text = " ".join(w["w"] for w in words[k:]) if words else " ".join(x.text for x in sents[a:b + 1])
                c = Candidate(a, b, max(0.0, start - pad), sents[b].e + pad, text)
                if k:
                    c.reasons["trimmed_lead"] = " ".join(w["w"] for w in words[:k])
                if c.dur >= min_len * 0.85:
                    out.append(c)
    return out


def _clip01(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def score(c: Candidate, sents: list[Sentence], idf_novelty: float, profile: dict) -> None:
    ss = sents[c.start_i:c.end_i + 1]
    first = ss[0]
    hook_window = [s for s in ss if s.s - first.s < 3.0]  # what the viewer hears in the first ~3 s
    cue = lambda ks, group: sum(s.cues.get(k, 0) for s in group for k in ks)  # noqa: E731
    energy = float(np.mean([s.energy_db for s in ss]))
    hook_energy = float(np.mean([s.energy_db for s in hook_window]))
    first_tok = c.text.split()[0].lower().strip(",.") if c.text.split() else ""
    dangling = first_tok in DANGLING_START
    last_complete = ss[-1].text.rstrip().endswith((".", "!", "?", "।"))
    q_then_answer = any(s.question for s in ss[:-1])
    avg_rate = float(np.mean([s.rate for s in ss]))
    filler = float(np.mean([s.filler_ratio for s in ss]))
    gaps = [ss[k + 1].s - ss[k].e for k in range(len(ss) - 1)]
    dead_air = sum(g for g in gaps if g > 0.6)
    conf = float(np.mean([s.conf for s in ss]))
    glossary = {g.lower() for g in profile.get("glossary", [])}
    rel_hits = sum(1 for g in glossary if g and g in c.text.lower())

    S, R = {}, {}
    S["hook"] = _clip01(0.35 + 0.12 * hook_energy / 3 + 0.18 * cue(("surprise", "stakes", "contrast"), hook_window)
                        + 0.15 * cue(("address",), hook_window) + 0.12 * any(s.question for s in hook_window)
                        + 0.08 * sum(s.numbers > 0 for s in hook_window) - 0.35 * dangling)
    R["hook"] = f"first 3s energy {hook_energy:+.1f}dB, cues {cue(('surprise','stakes','contrast'), hook_window)}, " \
                f"{'dangling start' if dangling else 'clean start'}"
    S["curiosity"] = _clip01(0.3 + 0.25 * q_then_answer + 0.12 * cue(("surprise", "contrast"), ss) / max(1, len(ss) / 3))
    R["curiosity"] = f"question→answer {q_then_answer}, contrast/surprise cues {cue(('surprise','contrast'), ss)}"
    S["relevance"] = _clip01(0.5 + 0.15 * rel_hits + 0.1 * cue(("address",), ss) / max(1, len(ss)))
    R["relevance"] = f"glossary hits {rel_hits}, direct address {cue(('address',), ss)}"
    S["emotion"] = _clip01(0.35 + 0.1 * energy / 2 + 0.15 * cue(("emotion", "surprise"), ss) / max(1, len(ss) / 2))
    R["emotion"] = f"mean energy {energy:+.1f}dB vs speaker baseline, emotion cues {cue(('emotion','surprise'), ss)}"
    S["information"] = _clip01(0.3 + 0.08 * sum(s.numbers > 0 for s in ss) + 0.12 * cue(("instruction", "stakes"), ss)
                               / max(1, len(ss) / 2))
    R["information"] = f"sentences with numbers {sum(s.numbers > 0 for s in ss)}, instruction/stakes cues " \
                       f"{cue(('instruction','stakes'), ss)}"
    S["completeness"] = _clip01(0.4 + 0.35 * last_complete + 0.25 * (not dangling))
    R["completeness"] = f"ends on full stop {last_complete}, starts cleanly {not dangling}"
    S["retention"] = _clip01(0.85 - 0.25 * max(0.0, (c.dur - 35) / 25) - 0.4 * dead_air / max(c.dur, 1) - filler)
    R["retention"] = f"duration {c.dur:.1f}s, dead air {dead_air:.1f}s, filler ratio {filler:.2f}"
    S["shareability"] = _clip01(0.3 + 0.2 * cue(("opinion", "surprise", "stakes"), ss) / max(1, len(ss) / 2)
                                + 0.1 * cue(("address",), ss) / max(1, len(ss)))
    R["shareability"] = f"opinion/surprise/stakes cues {cue(('opinion','surprise','stakes'), ss)}"
    S["replay"] = _clip01(0.3 + 0.3 * (c.dur < 30) + 0.1 * sum(s.numbers > 0 for s in ss))
    R["replay"] = f"short {c.dur < 30}, dense numbers {sum(s.numbers > 0 for s in ss)}"
    S["standalone"] = _clip01(0.85 - 0.45 * dangling - 0.15 * (len({s.topic_id for s in ss}) - 1))
    R["standalone"] = f"dangling reference at start {dangling}, topics spanned {len({s.topic_id for s in ss})}"
    S["originality"] = _clip01(idf_novelty)
    R["originality"] = f"lexical novelty vs rest of recording {idf_novelty:.2f}"
    S["pacing"] = _clip01(1 - abs(avg_rate - 2.8) / 2.8 - 0.3 * dead_air / max(c.dur, 1))
    R["pacing"] = f"{avg_rate:.1f} words/s, transcript confidence {conf:.2f}"

    w = {**DEFAULT_WEIGHTS, **(profile.get("score_weights") or {})}
    keep = {k: v for k, v in c.reasons.items() if k == "trimmed_lead"}
    c.scores, c.reasons = S, {**R, **keep}
    c.total = sum(S[d] * w[d] for d in DIMENSIONS) / sum(w[d] for d in DIMENSIONS)
    if conf < 0.5:  # transcript too unreliable to trust the clip's words
        c.total *= 0.8
        c.reasons["penalty"] = f"low transcript confidence {conf:.2f}"


def novelty(cands: list[Candidate], sents: list[Sentence]) -> list[float]:
    """How distinctive each candidate's words are relative to the whole recording (1 - similarity to centroid)."""
    if not cands:
        return []
    vecs = tfidf_vectors([c.text for c in cands] + [" ".join(s.text for s in sents)])
    centroid = vecs[-1]
    return [float(1 - vecs[i] @ centroid) for i in range(len(cands))]


def rank_and_select(cands: list[Candidate], k: int, min_score: float, max_overlap: float = 0.25,
                    max_similarity: float = 0.55) -> tuple[list[Candidate], dict]:
    """Greedy selection over the ranked pool: skip any candidate that overlaps an already selected clip in time by
    more than max_overlap of the shorter clip, or whose text is semantically too similar (TF-IDF cosine)."""
    pool = sorted(cands, key=lambda c: c.total, reverse=True)
    chosen: list[Candidate] = []
    rejected = {"below_threshold": 0, "time_overlap": 0, "duplicate_topic": 0}
    vec_cache: dict[int, np.ndarray] = {}
    for c in pool:
        if len(chosen) >= k:
            break
        if c.total < min_score:
            rejected["below_threshold"] += 1
            continue
        if any(_overlap(c, x) > max_overlap for x in chosen):
            rejected["time_overlap"] += 1
            continue
        v = tfidf_vectors([c.text] + [x.text for x in chosen])
        if chosen and float(np.max(v[1:] @ v[0])) > max_similarity:
            rejected["duplicate_topic"] += 1
            continue
        chosen.append(c)
    report = {"pool": len(cands), "requested": k, "selected": len(chosen), "rejected": rejected,
              "shortfall": max(0, k - len(chosen))}
    if report["shortfall"]:
        report["shortfall_reason"] = (f"only {len(chosen)} moments reached the quality threshold {min_score:.2f} "
                                      f"without repeating a topic; weaker clips were not manufactured")
    return sorted(chosen, key=lambda c: c.total, reverse=True), report


def _overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.e, b.e) - max(a.s, b.s))
    return inter / max(1e-6, min(a.dur, b.dur))
