"""Upload + schedule state machine with verification, idempotency and duplicate protection.

States: pending_approval -> processing -> uploaded -> scheduled -> published | failed | needs_user

Rules
  * A clip is eligible only if QC passed, the job has a rights basis, and either the client's policy has
    autonomous=True or a human approved the clip.
  * uploads.clip_id is UNIQUE and the rendered file's content hash is checked against earlier uploads, so a retry or
    a re-run never uploads the same clip twice. A crash after YouTube accepted the upload but before the video id
    was stored is the one window that could duplicate: state='processing' is written before the call, and a clip
    found still 'processing' with no video id is moved to needs_user instead of being uploaded again.
  * 'scheduled' is written only after videos.list confirms status.publishAt equals the requested time.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from ..db import DB
from ..profiles import get_client
from .scheduler import next_slots
from .youtube import YouTube, YouTubeError


def file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


NEEDS_REFERENCE = {"licensed", "clipping_program", "creator_permission"}


def eligible(db: DB, clip: dict, policy: dict, job: dict | None = None) -> tuple[bool, str]:
    job = job or db.one("SELECT * FROM jobs WHERE id=?", clip["job_id"]) or {}
    if job.get("rights_basis") in NEEDS_REFERENCE and not (job.get("rights_ref") or "").strip():
        return False, "third-party content: add the campaign link or written permission before publishing"
    if not clip.get("qc_pass"):
        return False, "quality checks did not pass"
    if not clip.get("path") or not Path(clip["path"]).exists():
        return False, "rendered file missing"
    if not policy.get("autonomous") and clip.get("approval") != "approved":
        return False, "waiting for approval (approval mode)"
    if clip.get("approval") == "rejected":
        return False, "rejected by reviewer"
    return True, ""


def publish_job(db: DB, job_id: str, yt: YouTube, actor: str = "worker") -> dict:
    job = db.one("SELECT * FROM jobs WHERE id=?", job_id)
    client = get_client(db, job["client_id"])
    policy = client["publish"]
    clips = db.all("SELECT * FROM clips WHERE job_id=? ORDER BY rank", job_id)
    report = {"scheduled": [], "skipped": {}, "failed": {}, "needs_user": None}
    todo = []
    for c in clips:
        ok, why = eligible(db, c, policy, job)
        up = db.one("SELECT * FROM uploads WHERE clip_id=?", c["id"])
        if up and up["state"] in ("scheduled", "published"):
            report["scheduled"].append({"clip": c["id"], "url": up["url"], "publish_at": up["publish_at"]})
            continue
        if not ok:
            report["skipped"][c["id"]] = why
            if why.startswith("waiting") and not up:
                _upsert(db, c["id"], client["id"], "pending_approval")
            continue
        todo.append((c, up))
    if not todo:
        return report
    taken = [r["publish_at"] for r in db.all(
        "SELECT publish_at FROM uploads WHERE client_id=? AND publish_at IS NOT NULL AND state IN "
        "('scheduled','published','uploaded')", client["id"])]
    slots = next_slots(len(todo), policy, taken)
    for (c, up), slot in zip(todo, slots):
        try:
            res = publish_clip(db, c, client, policy, yt, (up or {}).get("publish_at") or slot)
            report["scheduled"].append(res)
        except YouTubeError as e:
            report["failed"][c["id"]] = str(e)
            if e.needs_user:
                report["needs_user"] = str(e)
                break  # revoked auth affects every clip; stop and notify once
    db.audit(actor, "publish.run", job_id, scheduled=len(report["scheduled"]), failed=len(report["failed"]))
    return report


def _upsert(db: DB, clip_id: str, client_id: str, state: str, **kw) -> None:
    now = time.time()
    if db.one("SELECT id FROM uploads WHERE clip_id=?", clip_id):
        sets = ", ".join(f"{k}=?" for k in kw)
        db.run(f"UPDATE uploads SET state=?, updated=?{', ' + sets if sets else ''} WHERE clip_id=?",
               state, now, *kw.values(), clip_id)
    else:
        db.run("INSERT INTO uploads(id,clip_id,client_id,state,created,updated) VALUES(?,?,?,?,?,?)",
               uuid.uuid4().hex[:12], clip_id, client_id, state, now, now)
        if kw:
            _upsert(db, clip_id, client_id, state, **kw)


def publish_clip(db: DB, clip: dict, client: dict, policy: dict, yt: YouTube, publish_at: str,
                 max_attempts: int = 3) -> dict:
    seo = json.loads(clip["seo_json"] or "{}")
    h = clip.get("content_hash") or file_hash(clip["path"])
    dup = db.one("SELECT u.video_id, u.url FROM uploads u JOIN clips c ON c.id=u.clip_id WHERE c.content_hash=? AND "
                 "u.clip_id<>? AND u.video_id IS NOT NULL", h, clip["id"])
    if dup:
        _upsert(db, clip["id"], client["id"], "failed", error=f"duplicate of already uploaded {dup['url']}")
        raise YouTubeError(f"identical file already uploaded as {dup['url']}")
    up = db.one("SELECT * FROM uploads WHERE clip_id=?", clip["id"]) or {}
    vid = up.get("video_id")
    attempt = int(up.get("attempts") or 0)
    if not vid and up.get("state") == "processing":
        msg = "an earlier upload of this clip was interrupted; check the channel's private videos before retrying"
        _upsert(db, clip["id"], client["id"], "needs_user", error=msg)
        raise YouTubeError(msg, needs_user=True)
    if not vid:
        _upsert(db, clip["id"], client["id"], "processing", publish_at=publish_at, attempts=attempt + 1)
        last = None
        for k in range(max_attempts):
            try:
                vid = yt.upload_private(clip["path"], seo["title"], seo["description"], seo.get("tags", []),
                                        str(policy.get("category_id", "22")))
                break
            except YouTubeError as e:
                last = e
                if not e.retryable or k == max_attempts - 1:
                    _upsert(db, clip["id"], client["id"], "needs_user" if e.needs_user else "failed", error=str(e))
                    raise
                time.sleep(min(60, 2 ** k * 3))
        if not vid:
            raise last or YouTubeError("upload failed")
        _upsert(db, clip["id"], client["id"], "uploaded", video_id=vid, url=f"https://youtube.com/shorts/{vid}",
                error=None)
    yt.set_schedule(vid, publish_at, bool(policy.get("made_for_kids", False)))
    st = yt.video_status(vid).get("status", {})
    got = (st.get("publishAt") or "").replace(".000Z", "Z")
    if st.get("privacyStatus") != "private" or got[:16] != publish_at[:16]:
        _upsert(db, clip["id"], client["id"], "failed", error=f"schedule not confirmed by YouTube: {st}")
        raise YouTubeError(f"YouTube did not confirm the schedule for {vid}: {st}", retryable=True)
    _upsert(db, clip["id"], client["id"], "scheduled", publish_at=publish_at, error=None)
    db.audit("worker", "upload.scheduled", clip["id"], video_id=vid, publish_at=publish_at)
    return {"clip": clip["id"], "video_id": vid, "url": f"https://youtube.com/shorts/{vid}", "publish_at": publish_at}
