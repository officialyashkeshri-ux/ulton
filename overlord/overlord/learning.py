"""Analytics ingestion and the feedback loop.

collect():  pulls YouTube Analytics for this client's published videos (only videos older than 2 days: YouTube's
            reporting delay) and stores daily snapshots in `metrics`.
recommend(): compares each clip's retention against the channel's own median and asks which selection dimensions
            moved with better retention (Spearman rank correlation). It proposes small, bounded weight changes only when
            at least MIN_SAMPLES published clips exist. Correlation is reported as correlation, never as cause.
apply_recommendation(): writes the change as a new profile version (rollback-able), so learning never silently
            overwrites the client's settings; with autonomous learning off it only reports.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import numpy as np

from .db import DB
from .profiles import get_client, update_profile
from .scoring import DEFAULT_WEIGHTS, DIMENSIONS

MIN_SAMPLES = 12
MAX_STEP = 0.15  # at most ±15% change to any weight per update


def collect(db: DB, client_id: str, yt) -> int:
    rows = db.all("SELECT u.video_id, u.publish_at FROM uploads u WHERE u.client_id=? AND u.video_id IS NOT NULL AND "
                  "u.state IN ('scheduled','published')", client_id)
    today = date.today()
    ready = [r["video_id"] for r in rows if r["publish_at"] and
             date.fromisoformat(r["publish_at"][:10]) <= today - timedelta(days=2)]
    if not ready:
        return 0
    start = min(r["publish_at"][:10] for r in rows if r["video_id"] in ready)
    data = yt.analytics(ready, start, (today - timedelta(days=1)).isoformat())
    for d in data:
        db.run("INSERT OR REPLACE INTO metrics(video_id,day,data_json,fetched) VALUES(?,?,?,?)",
               d.get("video"), today.isoformat(), json.dumps(d), time.time())
        db.run("UPDATE uploads SET state='published', updated=? WHERE video_id=? AND state='scheduled'",
               time.time(), d.get("video"))
    return len(data)


def _spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 3:
        return 0.0
    rx, ry = np.argsort(np.argsort(x)), np.argsort(np.argsort(y))
    c = np.corrcoef(rx, ry)[0, 1]
    return float(0.0 if np.isnan(c) else c)


def dataset(db: DB, client_id: str) -> list[dict]:
    rows = db.all("""SELECT c.scores_json, c.end - c.start AS dur, u.publish_at, m.data_json FROM uploads u
                     JOIN clips c ON c.id=u.clip_id JOIN metrics m ON m.video_id=u.video_id
                     WHERE u.client_id=? AND m.day=(SELECT MAX(day) FROM metrics WHERE video_id=u.video_id)""", client_id)
    out = []
    for r in rows:
        m = json.loads(r["data_json"])
        out.append({"scores": json.loads(r["scores_json"]), "dur": r["dur"], "publish_at": r["publish_at"],
                    "retention": float(m.get("averageViewPercentage", 0) or 0), "views": float(m.get("views", 0) or 0)})
    return out


def recommend(db: DB, client_id: str) -> dict:
    data = dataset(db, client_id)
    rep = {"samples": len(data), "min_samples": MIN_SAMPLES, "correlations": {}, "proposal": {}, "notes": []}
    if len(data) < MIN_SAMPLES:
        rep["notes"].append(f"need {MIN_SAMPLES - len(data)} more published clips with analytics before changing weights")
        return rep
    ret = [d["retention"] for d in data]
    base = float(np.median(ret))
    rep["channel_median_retention"] = base
    client = get_client(db, client_id)
    w = {**DEFAULT_WEIGHTS, **(client["profile"].get("score_weights") or {})}
    for dim in DIMENSIONS:
        r = _spearman([d["scores"].get(dim, 0) for d in data], ret)
        rep["correlations"][dim] = round(r, 3)
        if abs(r) >= 0.25:  # ignore weak relationships
            rep["proposal"][dim] = round(w[dim] * (1 + MAX_STEP * np.sign(r) * min(1, abs(r) / 0.5)), 3)
    durs = [d["dur"] for d in data]
    rep["correlations"]["duration"] = round(_spearman(durs, ret), 3)
    hours: dict[int, list[float]] = {}
    for d in data:
        if d["publish_at"]:
            hours.setdefault(int(d["publish_at"][11:13]), []).append(d["retention"])
    rep["retention_by_publish_hour_utc"] = {h: round(float(np.median(v)), 1) for h, v in sorted(hours.items()) if len(v) >= 3}
    rep["notes"].append("correlations describe this channel's past clips; they are not proof of cause")
    return rep


def apply_recommendation(db: DB, client_id: str, rep: dict, actor: str = "learning") -> int | None:
    if not rep.get("proposal"):
        return None
    return update_profile(db, client_id, {"score_weights": rep["proposal"]},
                          note=f"learning update from {rep['samples']} clips: {rep['correlations']}", actor=actor)
