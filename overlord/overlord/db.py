"""SQLite persistence (WAL mode, safe for one API process plus several worker processes on one host).

Tables
  clients      one row per client; editing profile JSON is versioned in profile_versions
  jobs         one row per source video; status + current stage + lease for crash recovery
  checkpoints  per-job stage outputs (JSON) so a restarted worker resumes instead of redoing work
  clips        selected moments, render paths, QC report, SEO metadata, approval state
  uploads      one row per YouTube upload attempt; unique (clip_id) guards against duplicate uploads
  metrics      analytics snapshots per uploaded video per day
  feedback     explicit human feedback on clips (approve / reject / notes)
  audit        append-only log of state-changing actions (who, what, when)
  usage        compute seconds / API calls per job for cost-per-clip reporting
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL,
  profile_json TEXT NOT NULL, profile_version INTEGER NOT NULL DEFAULT 1,
  publish_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS profile_versions(
  client_id TEXT NOT NULL, version INTEGER NOT NULL, profile_json TEXT NOT NULL, note TEXT, created REAL NOT NULL,
  PRIMARY KEY(client_id, version));
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, source TEXT NOT NULL, source_key TEXT NOT NULL,
  rights_basis TEXT NOT NULL, clip_count INTEGER NOT NULL, min_len REAL NOT NULL, max_len REAL NOT NULL,
  status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued', progress REAL NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 5, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
  lease_owner TEXT, lease_until REAL, created REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(client_id, source_key, clip_count, min_len, max_len));
CREATE TABLE IF NOT EXISTS checkpoints(
  job_id TEXT NOT NULL, stage TEXT NOT NULL, data_json TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(job_id, stage));
CREATE TABLE IF NOT EXISTS clips(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, rank INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
  text TEXT NOT NULL, score REAL NOT NULL, scores_json TEXT NOT NULL, path TEXT, thumb TEXT,
  qc_json TEXT, qc_pass INTEGER, seo_json TEXT, approval TEXT NOT NULL DEFAULT 'pending',
  render_attempts INTEGER NOT NULL DEFAULT 0, content_hash TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS uploads(
  id TEXT PRIMARY KEY, clip_id TEXT NOT NULL UNIQUE, client_id TEXT NOT NULL, state TEXT NOT NULL,
  video_id TEXT, url TEXT, publish_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metrics(
  video_id TEXT NOT NULL, day TEXT NOT NULL, data_json TEXT NOT NULL, fetched REAL NOT NULL,
  PRIMARY KEY(video_id, day));
CREATE TABLE IF NOT EXISTS feedback(
  id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, client_id TEXT NOT NULL, verdict TEXT NOT NULL, note TEXT,
  created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  target TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS usage(
  job_id TEXT NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, priority, created);
CREATE INDEX IF NOT EXISTS clips_job ON clips(job_id, rank);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    # ---- generic helpers -------------------------------------------------------------------------------------
    def one(self, sql: str, *args: Any) -> dict | None:
        with self.conn() as c:
            r = c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def all(self, sql: str, *args: Any) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def run(self, sql: str, *args: Any) -> int:
        with self.conn() as c:
            return c.execute(sql, args).rowcount

    def audit(self, actor: str, action: str, target: str | None = None, **detail: Any) -> None:
        self.run("INSERT INTO audit(at,actor,action,target,detail_json) VALUES(?,?,?,?,?)",
                 time.time(), actor, action, target, json.dumps(detail, default=str))

    def usage(self, job_id: str, kind: str, amount: float) -> None:
        self.run("INSERT INTO usage(job_id,kind,amount,at) VALUES(?,?,?,?)", job_id, kind, float(amount), time.time())

    # ---- checkpoints -----------------------------------------------------------------------------------------
    def save_checkpoint(self, job_id: str, stage: str, data: Any) -> None:
        self.run("INSERT OR REPLACE INTO checkpoints(job_id,stage,data_json,created) VALUES(?,?,?,?)",
                 job_id, stage, json.dumps(data, default=str), time.time())

    def checkpoint(self, job_id: str, stage: str) -> Any | None:
        r = self.one("SELECT data_json FROM checkpoints WHERE job_id=? AND stage=?", job_id, stage)
        return json.loads(r["data_json"]) if r else None

    # ---- jobs -----------------------------------------------------------------------------------------------
    def create_job(self, client_id: str, source: str, source_key: str, rights_basis: str, clip_count: int,
                   min_len: float, max_len: float, priority: int = 5) -> tuple[str, bool]:
        """Returns (job_id, created). An identical request (same client, source, count, lengths) returns the
        existing job instead of creating a duplicate."""
        existing = self.one("SELECT id FROM jobs WHERE client_id=? AND source_key=? AND clip_count=? AND min_len=? "
                            "AND max_len=?", client_id, source_key, clip_count, min_len, max_len)
        if existing:
            return existing["id"], False
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        self.run("INSERT INTO jobs(id,client_id,source,source_key,rights_basis,clip_count,min_len,max_len,status,"
                 "priority,created,updated) VALUES(?,?,?,?,?,?,?,?, 'queued', ?,?,?)",
                 jid, client_id, source, source_key, rights_basis, clip_count, min_len, max_len, priority, now, now)
        self.audit("api", "job.create", jid, source=source, clip_count=clip_count)
        return jid, True

    def claim_job(self, worker: str, lease_s: float = 600) -> dict | None:
        """Atomically lease the next runnable job. Expired leases (crashed worker) are re-claimable."""
        now = time.time()
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM jobs WHERE (status='queued' AND (lease_until IS NULL OR lease_until < ?)) "
                          "OR (status='running' AND lease_until < ?) ORDER BY priority ASC, created ASC LIMIT 1",
                          (now, now)).fetchone()
            if not r:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE jobs SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, updated=? "
                      "WHERE id=?", (worker, now + lease_s, now, r["id"]))
            c.execute("COMMIT")
            out = dict(r)
            out["attempts"] += 1
            return out

    def heartbeat(self, job_id: str, worker: str, lease_s: float = 600) -> bool:
        return self.run("UPDATE jobs SET lease_until=?, updated=? WHERE id=? AND lease_owner=?",
                        time.time() + lease_s, time.time(), job_id, worker) == 1

    def set_stage(self, job_id: str, stage: str, progress: float) -> None:
        self.run("UPDATE jobs SET stage=?, progress=?, updated=? WHERE id=?", stage, progress, time.time(), job_id)

    def requeue(self, job_id: str, delay_s: float, error: str | None = None) -> None:
        """Put a job back in the queue, not claimable before delay_s (retry backoff / waiting for a live stream)."""
        self.run("UPDATE jobs SET status='queued', error=?, lease_owner=NULL, lease_until=?, updated=? WHERE id=?",
                 error, time.time() + delay_s, time.time(), job_id)

    def finish_job(self, job_id: str, status: str, error: str | None = None) -> None:
        self.run("UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_until=NULL, updated=? WHERE id=?",
                 status, error, time.time(), job_id)
        self.audit("worker", f"job.{status}", job_id, error=error)
