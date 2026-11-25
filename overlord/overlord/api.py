"""HTTP API + dashboard.  Run: uvicorn overlord.api:app --host 0.0.0.0 --port 8080

Auth: every /api route except /api/health needs header `Authorization: Bearer $OVERLORD_ADMIN_TOKEN`
(the server refuses to start without the token unless OVERLORD_INSECURE_LOCAL=1, for local development only).

Conversation flow (POST /api/chat): send a URL or file path -> the API probes it and asks how many clips -> send a
number -> job queued. Missing essentials (rights basis) are asked for in the same chat, one question at a time.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import ingest, learning
from .config import settings
from .db import DB
from .profiles import create_client, get_client, rollback_profile, update_profile, update_publish

ST = settings()
DBI = DB(ST.db_path)
app = FastAPI(title="Overlord", version="0.1.0")
WEB = Path(__file__).parent / "web"
if not ST.admin_token and os.environ.get("OVERLORD_INSECURE_LOCAL") != "1":
    raise RuntimeError("Set OVERLORD_ADMIN_TOKEN (or OVERLORD_INSECURE_LOCAL=1 for local development only)")


def auth(authorization: str = Header(default="")) -> str:
    if not ST.admin_token:
        return "local"
    tok = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(tok, ST.admin_token):
        raise HTTPException(401, "bad token")
    return "admin"


@app.get("/api/health")
def health():
    DBI.one("SELECT 1 AS ok")
    q = DBI.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")
    return {"ok": True, "queue": q["n"], "llm": ST.has_llm(), "publishing": ST.has_composio()}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return (WEB / "index.html").read_text(encoding="utf-8")


# ---- conversational intake ----------------------------------------------------------------------------------
class ChatIn(BaseModel):
    client_id: str
    message: str
    session: str | None = None


SESS: dict[str, dict] = {}


@app.post("/api/chat")
def chat(m: ChatIn, actor: str = Depends(auth)):
    get_client(DBI, m.client_id)
    sid = m.session or uuid.uuid4().hex[:10]
    s = SESS.setdefault(sid, {"client_id": m.client_id})
    text = m.message.strip()
    url = re.search(r"https?://\S+", text)
    if url or (Path(text).exists() and Path(text).is_file()):
        src = url.group(0) if url else text
        try:
            pr = ingest.probe(src)
        except ingest.IngestError as e:
            return {"session": sid, "reply": f"I couldn't open that source: {e}"}
        s.update(source=src, probe=pr.__dict__)
        state = {"is_live": "It's live right now. I'll record it as it plays and clip it when it ends.",
                 "is_upcoming": "It hasn't started yet. I'll wait and start recording when it goes live.",
                 "post_live": "The stream just ended and YouTube is still processing it; I'll start when it's ready.",
                 }.get(pr.live_status, f"I've checked \"{pr.title}\"" + (f" ({pr.duration / 60:.0f} min)." if pr.duration else "."))
        return {"session": sid, "reply": state + " How many clips would you like?", "ask": "clip_count"}
    if "source" in s and "clip_count" not in s and re.fullmatch(r"\d{1,3}", text):
        s["clip_count"] = max(1, min(100, int(text)))
        return {"session": sid, "ask": "rights_basis",
                "reply": "What gives you the right to clip this? (" + ", ".join(ingest.RIGHTS_BASES) + ")"}
    if "clip_count" in s and "rights" not in s:
        if text not in ingest.RIGHTS_BASES:
            return {"session": sid, "ask": "rights_basis", "reply": "Please answer with one of: " + ", ".join(ingest.RIGHTS_BASES)}
        s["rights"] = text
        jid, created = DBI.create_job(s["client_id"], s["source"], s["probe"]["key"], text, s["clip_count"], 15, 59)
        pol = get_client(DBI, s["client_id"])["publish"]
        mode = ("publish them on your schedule automatically" if pol["autonomous"]
                else "hold them for your approval before uploading")
        SESS.pop(sid, None)
        return {"session": sid, "job_id": jid, "created": created,
                "reply": f"Understood. I'll find the strongest {s['clip_count']} independent moments, edit them, run "
                         f"quality checks, write the metadata, and {mode}." + ("" if created else " (This exact job already exists.)")}
    return {"session": sid, "reply": "Send me a YouTube link, a livestream link, or an uploaded file to start."}


@app.post("/api/upload")
def upload(f: UploadFile = File(...), actor: str = Depends(auth)):
    ext = Path(f.filename or "x.mp4").suffix.lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".m4v"):
        raise HTTPException(400, "unsupported file type")
    dst = ST.data_dir / "uploads" / f"{uuid.uuid4().hex[:12]}{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as out:
        shutil.copyfileobj(f.file, out)
    return {"path": str(dst)}


# ---- clients / profiles -------------------------------------------------------------------------------------
class ClientIn(BaseModel):
    name: str
    profile: dict | None = None
    publish: dict | None = None


@app.post("/api/clients")
def new_client(c: ClientIn, actor: str = Depends(auth)):
    return {"id": create_client(DBI, c.name, c.profile, c.publish)}


@app.get("/api/clients")
def clients(actor: str = Depends(auth)):
    return [get_client(DBI, r["id"]) for r in DBI.all("SELECT id FROM clients ORDER BY created")]


@app.patch("/api/clients/{cid}/profile")
def patch_profile(cid: str, patch: dict, actor: str = Depends(auth)):
    return {"version": update_profile(DBI, cid, patch, note=patch.pop("_note", ""), actor=actor)}


@app.post("/api/clients/{cid}/profile/rollback/{version}")
def rollback(cid: str, version: int, actor: str = Depends(auth)):
    return {"version": rollback_profile(DBI, cid, version, actor)}


@app.patch("/api/clients/{cid}/publish")
def patch_publish(cid: str, patch: dict, actor: str = Depends(auth)):
    return update_publish(DBI, cid, patch, actor)


# ---- jobs / clips ---------------------------------------------------------------------------------------------
@app.get("/api/overview")
def overview(actor: str = Depends(auth)):
    one = lambda q: DBI.one(q)["n"]  # noqa: E731
    usage = DBI.all("SELECT kind, SUM(amount) amt FROM usage GROUP BY kind")
    return {"videos": one("SELECT COUNT(*) n FROM jobs"), "clips": one("SELECT COUNT(*) n FROM clips"),
            "qc_passed": one("SELECT COUNT(*) n FROM clips WHERE qc_pass=1"),
            "scheduled": one("SELECT COUNT(*) n FROM uploads WHERE state='scheduled'"),
            "published": one("SELECT COUNT(*) n FROM uploads WHERE state='published'"),
            "pending": one("SELECT COUNT(*) n FROM uploads WHERE state='pending_approval'"),
            "failed_jobs": one("SELECT COUNT(*) n FROM jobs WHERE status IN ('failed','needs_user')"),
            "usage": {u["kind"]: round(u["amt"], 1) for u in usage}}


@app.get("/api/jobs")
def jobs(actor: str = Depends(auth)):
    return DBI.all("SELECT id,client_id,source,status,stage,progress,attempts,error,clip_count,created,updated "
                   "FROM jobs ORDER BY created DESC LIMIT 100")


@app.get("/api/jobs/{jid}")
def job(jid: str, actor: str = Depends(auth)):
    j = DBI.one("SELECT * FROM jobs WHERE id=?", jid)
    if not j:
        raise HTTPException(404)
    j["select"] = DBI.checkpoint(jid, "select")
    j["publish"] = DBI.checkpoint(jid, "publish")
    j["clips"] = [_clip(c) for c in DBI.all("SELECT * FROM clips WHERE job_id=? ORDER BY rank", jid)]
    return j


def _clip(c: dict) -> dict:
    up = DBI.one("SELECT state,url,publish_at,error,video_id FROM uploads WHERE clip_id=?", c["id"])
    return {**{k: v for k, v in c.items() if not k.endswith("_json") and k not in ("path", "thumb")},
            "scores": json.loads(c["scores_json"]), "qc": json.loads(c["qc_json"] or "null"),
            "seo": json.loads(c["seo_json"] or "null"), "upload": up,
            "video_url": f"/api/clips/{c['id']}/video" if c["path"] else None}


@app.post("/api/jobs/{jid}/retry")
def retry(jid: str, actor: str = Depends(auth)):
    DBI.run("UPDATE jobs SET attempts=0 WHERE id=?", jid)
    DBI.requeue(jid, 0, None)
    DBI.audit(actor, "job.retry", jid)
    return {"ok": True}


@app.get("/api/clips/{cid}/video")
def clip_video(cid: str, actor: str = Depends(auth)):
    c = DBI.one("SELECT path FROM clips WHERE id=?", cid)
    if not c or not c["path"] or not Path(c["path"]).exists():
        raise HTTPException(404)
    return FileResponse(c["path"], media_type="video/mp4")


class Verdict(BaseModel):
    verdict: str  # approved | rejected
    note: str | None = None


@app.post("/api/clips/{cid}/review")
def review(cid: str, v: Verdict, actor: str = Depends(auth)):
    if v.verdict not in ("approved", "rejected"):
        raise HTTPException(400, "verdict must be approved or rejected")
    c = DBI.one("SELECT c.id, j.client_id FROM clips c JOIN jobs j ON j.id=c.job_id WHERE c.id=?", cid)
    if not c:
        raise HTTPException(404)
    DBI.run("UPDATE clips SET approval=? WHERE id=?", v.verdict, cid)
    DBI.run("INSERT INTO feedback(id,clip_id,client_id,verdict,note,created) VALUES(?,?,?,?,?,strftime('%s','now'))",
            uuid.uuid4().hex[:12], cid, c["client_id"], v.verdict, v.note)
    DBI.audit(actor, f"clip.{v.verdict}", cid, note=v.note)
    return {"ok": True}


@app.get("/api/clients/{cid}/learning")
def learning_report(cid: str, actor: str = Depends(auth)):
    return learning.recommend(DBI, cid)


@app.get("/api/audit")
def audit(actor: str = Depends(auth)):
    return DBI.all("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
