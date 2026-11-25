"""Job orchestration. Each stage is idempotent and checkpointed:

  ingest -> transcribe -> select -> seo -> render+qc (per clip, with regeneration) -> publish (policy permitting)

A worker that dies mid-job leaves its checkpoints; the next worker to lease the job skips finished stages and
finished clips. One clip failing never fails the others; the job ends 'done' (all clips ok), 'partial' (some clips
failed QC/publishing, details per clip) or 'failed' (no usable output, with the exact error).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from . import analyze, ingest, llm, qc, render, scoring, seo
from .config import Settings
from .db import DB
from .profiles import get_client
from .publish.publisher import file_hash, publish_job

log = logging.getLogger("overlord.pipeline")


class Ctx:
    def __init__(self, db: DB, st: Settings, job: dict, heartbeat=lambda: None):
        self.db, self.st, self.job, self.hb = db, st, job, heartbeat
        self.dir = st.job_dir(job["id"])
        self.client = get_client(db, job["client_id"])
        self.prof = self.client["profile"]

    def stage(self, name: str, progress: float):
        self.db.set_stage(self.job["id"], name, progress)
        self.hb()


def run_job(db: DB, st: Settings, job: dict, yt=None, heartbeat=lambda: None) -> str:
    cx = Ctx(db, st, job, heartbeat)
    t0 = time.time()
    media, pr = stage_ingest(cx)
    words = stage_transcribe(cx, media)
    stage_select(cx, words)
    stage_seo(cx)
    ok, bad = stage_render(cx, media, words)
    if yt is not None:
        cx.stage("publish", 0.95)
        rep = publish_job(db, job["id"], yt)
        db.save_checkpoint(job["id"], "publish", rep)
        if rep.get("needs_user"):
            db.audit("worker", "alert.needs_user", job["id"], reason=rep["needs_user"])
    db.usage(job["id"], "wall_seconds", time.time() - t0)
    cx.stage("done", 1.0)
    if ok == 0:
        rep = (db.checkpoint(job["id"], "select") or {}).get("report", {})
        raise PipelineError(rep.get("shortfall_reason") or f"all {bad} rendered clips failed quality checks")
    return "done" if bad == 0 else "partial"


class PipelineError(RuntimeError):
    pass


def stage_ingest(cx: Ctx):
    cp = cx.db.checkpoint(cx.job["id"], "ingest")
    if cp and Path(cp["media"]).exists():
        return Path(cp["media"]), cp["probe"]
    cx.stage("ingest", 0.02)
    pr = ingest.probe(cx.job["source"])
    media = ingest.fetch(cx.job["source"], pr, cx.dir / "src", heartbeat=cx.hb)
    w, h, dur = render.probe_video(media)
    data = {"media": str(media), "probe": {**pr.__dict__, "width": w, "height": h, "duration": dur}}
    cx.db.save_checkpoint(cx.job["id"], "ingest", data)
    return media, data["probe"]


def stage_transcribe(cx: Ctx, media: Path) -> dict:
    out = cx.dir / "transcript.json"
    if out.exists():
        from .transcribe import normalize_words

        tr = json.loads(out.read_text())
        tr["words"] = normalize_words(tr["words"])
        return tr
    cx.stage("transcribe", 0.1)
    prompt = ", ".join(cx.prof.get("glossary") or []) or None
    tr = __import__("overlord.transcribe", fromlist=["transcribe"]).transcribe(
        media, cx.dir, cx.st.whisper_model, cx.st.whisper_compute, cx.prof.get("language"), prompt)
    out.write_text(json.dumps(tr, ensure_ascii=False))
    cx.db.usage(cx.job["id"], "transcribe_seconds", tr["seconds"])
    cx.db.save_checkpoint(cx.job["id"], "transcribe", {"words": len(tr["words"]), "language": tr["language"]})
    return tr


def stage_select(cx: Ctx, tr: dict) -> None:
    if cx.db.checkpoint(cx.job["id"], "select"):
        return
    cx.stage("analyze", 0.3)
    sents = analyze.sentences(tr["words"], dips=analyze.boundary_dips(tr["words"], cx.dir / "audio16k.wav"))
    info = analyze.annotate(sents, cx.dir / "audio16k.wav")
    k = int(cx.job["clip_count"])
    cands = scoring.generate(sents, cx.job["min_len"], cx.job["max_len"])
    for c in cands:
        scoring.score(c, sents, 0.5, cx.prof)
    pool = sorted(cands, key=lambda c: c.total, reverse=True)[: min(len(cands), max(40 * k, 200))]
    for c, nov in zip(pool, scoring.novelty(pool, sents)):
        scoring.score(c, sents, nov, cx.prof)
    pool.sort(key=lambda c: c.total, reverse=True)
    if cx.st.has_llm() and pool:
        cx.stage("judge", 0.4)
        top = _diverse_top(pool, max(3 * k, 12))
        verdicts = llm.judge(cx.st.llm_model, top, tr["language"], cx.client["name"])
        for i, c in enumerate(top):
            v = verdicts.get(i)
            if not v:
                continue
            for d in llm.SEMANTIC_DIMS:
                c.scores[d] = round(0.5 * c.scores[d] + 0.5 * v[d], 4)
            c.reasons["model"] = v["why"]
            c.source = "heuristic+claude"
            w = {**scoring.DEFAULT_WEIGHTS, **(cx.prof.get("score_weights") or {})}
            c.total = sum(c.scores[d] * w[d] for d in scoring.DIMENSIONS) / sum(w.values())
            if v["misleading"]:
                c.total = 0.0
                c.reasons["rejected"] = "clipping would change the speaker's meaning"
        pool.sort(key=lambda c: c.total, reverse=True)
    chosen, report = scoring.rank_and_select(pool, k, float(cx.prof.get("min_score", 0.45)))
    report.update(info)
    report["scoring_source"] = "heuristic+claude" if cx.st.has_llm() else "heuristic"
    now = time.time()
    for rank, c in enumerate(chosen, 1):
        cx.db.run("INSERT INTO clips(id,job_id,rank,start,end,text,score,scores_json,created) VALUES(?,?,?,?,?,?,?,?,?)",
                  uuid.uuid4().hex[:12], cx.job["id"], rank, c.s, c.e, c.text, round(c.total, 4),
                  json.dumps({**c.scores, "_reasons": c.reasons, "_source": c.source}), now)
    cx.db.save_checkpoint(cx.job["id"], "select", {"report": report, "top_pool": [
        {"s": c.s, "e": c.e, "total": round(c.total, 3), "text": c.text[:160]} for c in pool[:30]]})


def _diverse_top(pool, n):
    out = []
    for c in pool:
        if all(scoring._overlap(c, x) < 0.5 for x in out):
            out.append(c)
        if len(out) >= n:
            break
    return out


def stage_render(cx: Ctx, media: Path, tr: dict) -> tuple[int, int]:
    clips = cx.db.all("SELECT * FROM clips WHERE job_id=? ORDER BY rank", cx.job["id"])
    ing = cx.db.checkpoint(cx.job["id"], "ingest")["probe"]
    ok = bad = 0
    picker = render.keyword_picker(cx.prof.get("glossary") or [])
    for n, c in enumerate(clips):
        cx.stage(f"render {n + 1}/{len(clips)}", 0.5 + 0.35 * n / max(1, len(clips)))
        if c["qc_pass"] == 1 and c["path"] and Path(c["path"]).exists():
            ok += 1
            continue
        res = render_with_retry(cx, media, tr, c, ing, picker)
        ok += res
        bad += 1 - res
    return ok, bad


def render_with_retry(cx: Ctx, media, tr, c, ing, picker, attempts: int = 3) -> int:
    prof = json.loads(json.dumps(cx.prof))
    scores = json.loads(c["scores_json"])
    misleading = "rejected" in scores.get("_reasons", {})
    meta = json.loads(c["seo_json"] or "null") or {}
    hook = meta.get("hook_text") if meta.get("source") == "claude" else teaser_hook(c["text"])
    out = cx.dir / "clips" / f"{c['rank']:02d}_{c['id']}.mp4"
    out.parent.mkdir(exist_ok=True)
    report = {}
    for k in range(attempts):
        t0 = time.time()
        try:
            r = render.render(media, tr["words"], c["start"], c["end"], prof, out, hook, ing["width"], ing["height"], picker)
        except Exception as e:  # render failure for this clip only
            report = {"pass": False, "critical_failed": ["render"], "error": str(e)[-400:], "checks": {}}
            prof["punch_in"]["enabled"] = False
            continue
        finally:
            cx.db.usage(cx.job["id"], "render_seconds", time.time() - t0)
        report = qc.check(out, r["duration"], Path(r["ass"]), c["text"], r["words"], misleading,
                          prof["audio"]["loudness_lufs"])
        report["render"] = {key: r[key] for key in ("layout", "segments", "cuts", "faces_found", "duration")}
        if report["pass"] or not report["recoverable"]:
            break
        failed = set(report["critical_failed"])  # adjust and regenerate
        if failed & {"captions_in_bounds", "captions_overlap"}:
            prof["captions"]["max_words"] = max(1, prof["captions"]["max_words"] - 1)
        if failed & {"loudness", "audio_clipping"}:
            prof["audio"]["denoise"] = False
        if "black_frames" in failed:
            prof["punch_in"]["enabled"] = False
    cx.db.run("UPDATE clips SET path=?, thumb=?, qc_json=?, qc_pass=?, render_attempts=render_attempts+?, content_hash=? "
              "WHERE id=?", str(out) if out.exists() else None,
              str(out.with_suffix(".jpg")) if out.with_suffix(".jpg").exists() else None, json.dumps(report, default=str),
              1 if report.get("pass") else 0, k + 1, file_hash(str(out)) if out.exists() else None, c["id"])
    return 1 if report.get("pass") else 0


def teaser_hook(text: str) -> str | None:
    """Without a model, the on-screen hook is a short teaser of a LATER strong phrase (numbers / stakes), never a
    repeat of the opening words the viewer is already hearing. None when no phrase qualifies."""
    us = seo.units(text)
    if len(us) < 2:
        return None
    lead = seo.lead_phrase(" ".join(us[1:]))
    words = lead.split()
    if not any(ch.isdigit() for ch in lead) and len(words) > 9:
        return None
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


def stage_seo(cx: Ctx) -> None:
    clips = cx.db.all("SELECT * FROM clips WHERE job_id=? AND seo_json IS NULL", cx.job["id"])
    if not clips:
        return
    cx.stage("seo", 0.9)
    lang = (cx.db.checkpoint(cx.job["id"], "transcribe") or {}).get("language", "")
    pub, prof = cx.client["publish"], cx.prof
    for c in clips:
        meta = None
        if cx.st.has_llm():
            meta = llm.seo(cx.st.llm_model, c["text"], lang, cx.client["name"], prof.get("glossary") or [],
                           prof.get("avoid_words") or [])
            if meta:
                meta["hashtags"] = list(dict.fromkeys((meta.get("hashtags") or []) + pub.get("hashtags", [])))
                meta["source"] = "claude"
        try:
            meta = seo.validate(meta, prof.get("avoid_words")) if meta else None
        except ValueError:
            meta = None
        if not meta:
            meta = seo.fallback(c["text"], prof.get("glossary") or [], pub.get("hashtags", []), cx.client["name"])
        cx.db.run("UPDATE clips SET seo_json=? WHERE id=?", json.dumps(meta, ensure_ascii=False), c["id"])
