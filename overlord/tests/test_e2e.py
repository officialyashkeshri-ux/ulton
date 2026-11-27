"""End-to-end: generated talking video -> ingest -> transcribe -> select -> render -> QC -> SEO -> publish (fake
YouTube) -> analytics -> learning report; plus crash/restart resume, retries and duplicate protection."""
import json
from pathlib import Path

import pytest

from overlord import learning, pipeline
from overlord.profiles import create_client, update_publish
from overlord.publish.publisher import publish_job
from overlord.publish.youtube import FakeYouTube, YouTubeError
from overlord.worker import process_one


def _job(db, cid, src, k=3):
    from overlord.ingest import probe

    return db.create_job(cid, str(src), probe(str(src)).key, "own_content", k, 6, 20)[0]


def test_full_pipeline_approval_then_publish(env, speech_video):
    st, db = env
    cid = create_client(db, "Test channel", {"min_score": 0.3})
    jid = _job(db, cid, speech_video, 3)
    yt = FakeYouTube()
    assert process_one(db, st, "w1", yt)
    job = db.one("SELECT * FROM jobs WHERE id=?", jid)
    assert job["status"] in ("done", "partial"), job["error"]
    clips = db.all("SELECT * FROM clips WHERE job_id=? ORDER BY rank", jid)
    assert 1 <= len(clips) <= 3
    passed = [c for c in clips if c["qc_pass"]]
    assert passed, [json.loads(c["qc_json"])["critical_failed"] for c in clips]
    for c in passed:
        qc = json.loads(c["qc_json"])["checks"]
        assert qc["resolution"]["value"] == "1080x1920" and qc["codec"]["ok"] and qc["loudness"]["ok"]
        assert Path(c["path"]).exists() and json.loads(c["seo_json"])["title"]
    # approval mode (default): nothing uploaded, clips wait for a human
    assert yt.calls == []
    assert db.one("SELECT COUNT(*) n FROM uploads WHERE state='pending_approval'")["n"] == len(passed)
    db.run("UPDATE clips SET approval='approved' WHERE id=?", passed[0]["id"])
    rep = publish_job(db, jid, yt)
    assert len(rep["scheduled"]) == 1 and yt.calls == ["upload", "schedule"]
    up = db.one("SELECT * FROM uploads WHERE clip_id=?", passed[0]["id"])
    assert up["state"] == "scheduled" and up["publish_at"] and up["url"].startswith("https://youtube.com/shorts/")
    assert yt.videos[up["video_id"]]["status"]["publishAt"] == up["publish_at"]
    # re-running publishing never uploads the same clip again
    publish_job(db, jid, yt)
    assert yt.calls.count("upload") == 1


def test_autonomous_mode_retries_and_no_duplicates(env, speech_video):
    st, db = env
    cid = create_client(db, "Auto", {"min_score": 0.3})
    update_publish(db, cid, {"autonomous": True, "max_per_day": 1})
    jid = _job(db, cid, speech_video, 2)
    yt = FakeYouTube(fail_upload_times=1)  # first upload hits a simulated 503, then succeeds
    process_one(db, st, "w1", yt)
    ups = db.all("SELECT * FROM uploads WHERE state='scheduled'")
    n_ok = db.one("SELECT COUNT(*) n FROM clips WHERE job_id=? AND qc_pass=1", jid)["n"]
    assert len(ups) == n_ok and yt.calls.count("upload") == n_ok + 1
    days = [u["publish_at"][:10] for u in ups]
    assert len(days) == len(set(days))  # max_per_day=1 respected
    before = len(yt.videos)
    publish_job(db, jid, yt)
    assert len(yt.videos) == before


def test_crash_after_transcribe_resumes_without_redoing_work(env, speech_video, monkeypatch):
    st, db = env
    cid = create_client(db, "Crash", {"min_score": 0.3})
    jid = _job(db, cid, speech_video, 2)
    real = pipeline.stage_render
    monkeypatch.setattr(pipeline, "stage_render", lambda *a: (_ for _ in ()).throw(RuntimeError("worker killed")))
    process_one(db, st, "w1", None)
    j = db.one("SELECT * FROM jobs WHERE id=?", jid)
    assert j["status"] == "queued" and "worker killed" in j["error"]
    tr = st.job_dir(jid) / "transcript.json"
    mtime = tr.stat().st_mtime
    monkeypatch.setattr(pipeline, "stage_render", real)
    db.run("UPDATE jobs SET lease_until=0 WHERE id=?", jid)  # skip the retry backoff for the test
    process_one(db, st, "w2", None)
    assert db.one("SELECT status FROM jobs WHERE id=?", jid)["status"] in ("done", "partial")
    assert tr.stat().st_mtime == mtime  # transcript reused from the checkpoint


def test_revoked_auth_stops_and_flags_once(env, speech_video):
    st, db = env
    cid = create_client(db, "Revoked", {"min_score": 0.3})
    update_publish(db, cid, {"autonomous": True})
    jid = _job(db, cid, speech_video, 2)

    class Revoked(FakeYouTube):
        def upload_private(self, *a):
            self.calls.append("upload")
            raise YouTubeError("authorisation is no longer valid", needs_user=True)

    yt = Revoked()
    process_one(db, st, "w1", yt)
    assert yt.calls.count("upload") == 1  # stops at the first auth failure instead of hammering YouTube
    assert (db.checkpoint(jid, "publish") or {}).get("needs_user")
    assert db.one("SELECT COUNT(*) n FROM uploads WHERE state='needs_user'")["n"] == 1


def test_analytics_collect_and_learning_report(env, speech_video):
    st, db = env
    cid = create_client(db, "Learn", {"min_score": 0.3})
    update_publish(db, cid, {"autonomous": True})
    _job(db, cid, speech_video, 2)
    yt = FakeYouTube()
    process_one(db, st, "w1", yt)
    db.run("UPDATE uploads SET publish_at='2026-01-01T12:00:00Z'")  # pretend they published long ago
    assert learning.collect(db, cid, yt) >= 1
    assert db.one("SELECT COUNT(*) n FROM uploads WHERE state='published'")["n"] >= 1
    rep = learning.recommend(db, cid)
    assert rep["samples"] >= 1 and rep["proposal"] == {} and "more published clips" in rep["notes"][0]


def test_api_chat_flow(env, speech_video, monkeypatch):
    monkeypatch.setenv("OVERLORD_INSECURE_LOCAL", "1")
    import importlib

    import overlord.api as api
    importlib.reload(api)
    from fastapi.testclient import TestClient

    c = TestClient(api.app)
    cid = c.post("/api/clients", json={"name": "Chat"}).json()["id"]
    r = c.post("/api/chat", json={"client_id": cid, "message": str(speech_video)}).json()
    assert "How many clips" in r["reply"]
    r = c.post("/api/chat", json={"client_id": cid, "message": "10", "session": r["session"]}).json()
    assert r["ask"] == "rights_basis"
    r = c.post("/api/chat", json={"client_id": cid, "message": "own_content", "session": r["session"]}).json()
    assert r["job_id"] and "strongest 10" in r["reply"] and "approval" in r["reply"]
    # third-party content: the chat asks for the campaign link, and a URL answer is not mistaken for a new video
    r = c.post("/api/chat", json={"client_id": cid, "message": str(speech_video)}).json()
    r = c.post("/api/chat", json={"client_id": cid, "message": "5", "session": r["session"]}).json()
    r = c.post("/api/chat", json={"client_id": cid, "message": "clipping_program", "session": r["session"]}).json()
    assert r["ask"] == "rights_ref"
    r = c.post("/api/chat", json={"client_id": cid, "message": "https://whop.com/campaign/abc", "session": r["session"]}).json()
    assert r.get("job_id"), r
    assert c.get("/api/health").json()["ok"]
    assert c.get("/api/overview").json()["videos"] == 2
