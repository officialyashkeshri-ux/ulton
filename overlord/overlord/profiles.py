"""Client editing profiles and publishing policies.

Each client has its own profile row. Updates are merged into a copy of that client's profile and stored as a new
version (profile_versions keeps every previous version), so one client's change can never touch another client,
and any change can be rolled back.
"""
from __future__ import annotations

import copy
import json
import time
import uuid

from .db import DB

DEFAULT_PROFILE: dict = {
    "captions": {
        "font": "Inter Tight", "font_file": None, "size": 96, "uppercase": False, "max_words": 3,
        "color": "#FFFFFF", "highlight": "#D4FF3F", "outline": "#000000", "outline_px": 6,
        "position_y": 0.64,  # fraction of frame height for the caption baseline (clear of face and app buttons)
    },
    "hook_title": {"enabled": True, "font": "Anton", "size": 110, "position_y": 0.12, "seconds": 2.5},
    "reframe": {"mode": "face", "smoothing": 0.85, "eye_line": 0.38},
    "pacing": {"max_gap": 0.35, "keep_gap": 0.12},  # silences longer than max_gap shrink to keep_gap
    "punch_in": {"enabled": True, "scale": 1.10, "every_s": 6.0},
    "audio": {"loudness_lufs": -14.0, "denoise": True},
    "language": None,  # None = autodetect
    "glossary": [],  # names/brands to spell correctly in captions and metadata
    "avoid_words": [],
    "score_weights": {},  # per-dimension overrides; see scoring.DEFAULT_WEIGHTS
    "min_score": 0.45,
    "references": {"approved_examples": [], "rejected_examples": []},
}

DEFAULT_PUBLISH: dict = {
    "autonomous": False,  # approval mode until the owner explicitly enables autonomous publishing
    "max_per_day": 3,
    "windows": ["12:30", "18:30", "21:00"],  # local times
    "timezone": "Asia/Kolkata",
    "privacy_after_publish": "public",
    "category_id": "24",  # Entertainment; overridable per client
    "made_for_kids": False,
    "hashtags": ["#shorts"],
}


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def create_client(db: DB, name: str, profile: dict | None = None, publish: dict | None = None) -> str:
    cid = uuid.uuid4().hex[:10]
    prof = _merge(DEFAULT_PROFILE, profile or {})
    pub = _merge(DEFAULT_PUBLISH, publish or {})
    now = time.time()
    db.run("INSERT INTO clients(id,name,created,profile_json,profile_version,publish_json) VALUES(?,?,?,?,1,?)",
           cid, name, now, json.dumps(prof), json.dumps(pub))
    db.run("INSERT INTO profile_versions(client_id,version,profile_json,note,created) VALUES(?,?,?,?,?)",
           cid, 1, json.dumps(prof), "created", now)
    db.audit("api", "client.create", cid, name=name)
    return cid


def get_client(db: DB, cid: str) -> dict:
    r = db.one("SELECT * FROM clients WHERE id=?", cid)
    if not r:
        raise KeyError(f"unknown client {cid}")
    r["profile"] = json.loads(r.pop("profile_json"))
    r["publish"] = json.loads(r.pop("publish_json"))
    return r


def update_profile(db: DB, cid: str, patch: dict, note: str = "", actor: str = "api") -> int:
    c = get_client(db, cid)
    new = _merge(c["profile"], patch)
    ver = c["profile_version"] + 1
    db.run("UPDATE clients SET profile_json=?, profile_version=? WHERE id=? AND profile_version=?",
           json.dumps(new), ver, cid, c["profile_version"])
    db.run("INSERT INTO profile_versions(client_id,version,profile_json,note,created) VALUES(?,?,?,?,?)",
           cid, ver, json.dumps(new), note, time.time())
    db.audit(actor, "profile.update", cid, version=ver, note=note, keys=sorted(patch))
    return ver


def rollback_profile(db: DB, cid: str, version: int, actor: str = "api") -> int:
    r = db.one("SELECT profile_json FROM profile_versions WHERE client_id=? AND version=?", cid, version)
    if not r:
        raise KeyError(f"no version {version} for {cid}")
    return update_profile(db, cid, json.loads(r["profile_json"]), note=f"rollback to v{version}", actor=actor)


def update_publish(db: DB, cid: str, patch: dict, actor: str = "api") -> dict:
    c = get_client(db, cid)
    new = _merge(c["publish"], patch)
    db.run("UPDATE clients SET publish_json=? WHERE id=?", json.dumps(new), cid)
    db.audit(actor, "publish_policy.update", cid, keys=sorted(patch), autonomous=new.get("autonomous"))
    return new
