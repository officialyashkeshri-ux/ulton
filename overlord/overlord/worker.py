"""Worker process: leases jobs from the database, runs the pipeline, heartbeats, retries, recovers.

Run one per CPU-heavy slot:  python -m overlord.worker
Several workers (or hosts sharing the volume) can run at once: leases make each job single-owner, and a job whose
worker died becomes claimable again when its lease expires.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback

from .config import settings
from .db import DB
from .ingest import IngestError, NotReady
from .pipeline import PipelineError, run_job
from .publish.youtube import YouTubeError

log = logging.getLogger("overlord.worker")
STOP = threading.Event()


def youtube_client(st):
    if not st.has_composio():
        return None
    from .publish.youtube import ComposioYouTube

    return ComposioYouTube(st.composio_user_id, st.composio_account_id)


def process_one(db: DB, st, worker_id: str, yt=None) -> bool:
    job = db.claim_job(worker_id, lease_s=600)
    if not job:
        return False
    jid = job["id"]
    log.info("job %s claimed (attempt %s)", jid, job["attempts"])
    beat_stop = threading.Event()

    def beat():
        while not beat_stop.wait(60):
            if not db.heartbeat(jid, worker_id):
                log.warning("lost lease on %s", jid)

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    started = time.time()
    try:
        status = run_job(db, st, job, yt, heartbeat=lambda: db.heartbeat(jid, worker_id))
        if time.time() - started > st.job_timeout_s:
            log.warning("job %s exceeded its time budget", jid)
        db.finish_job(jid, status)
    except NotReady as e:
        db.requeue(jid, 600, str(e))
    except YouTubeError as e:
        if e.needs_user:
            db.finish_job(jid, "needs_user", str(e))
        else:
            _retry_or_fail(db, st, job, str(e))
    except (IngestError, PipelineError) as e:
        db.finish_job(jid, "failed", str(e))  # not retryable: bad source or nothing worth publishing
    except Exception as e:  # unexpected: keep the trace, retry with backoff
        log.error("job %s crashed: %s", jid, traceback.format_exc())
        _retry_or_fail(db, st, job, f"{type(e).__name__}: {e}")
    finally:
        beat_stop.set()
    return True


def tick(db: DB, yt, last: dict) -> None:
    """Housekeeping every few minutes: publish clips approved after their job finished, and once a day pull
    analytics for every client (YouTube's analytics lag by ~2 days, so more often is pointless)."""
    from . import learning
    from .publish.publisher import publish_job

    if yt is None:
        return
    rows = db.all("""SELECT DISTINCT c.job_id FROM clips c JOIN jobs j ON j.id=c.job_id
                     LEFT JOIN uploads u ON u.clip_id=c.id
                     WHERE j.status IN ('done','partial') AND c.qc_pass=1 AND c.approval='approved'
                     AND (u.state IS NULL OR u.state IN ('pending_approval','failed'))""")
    for r in rows:
        try:
            publish_job(db, r["job_id"], yt, actor="tick")
        except YouTubeError as e:
            log.warning("tick publish %s: %s", r["job_id"], e)
    if time.time() - last.get("analytics", 0) > 24 * 3600:
        last["analytics"] = time.time()
        for c in db.all("SELECT id FROM clients"):
            try:
                n = learning.collect(db, c["id"], yt)
                log.info("analytics %s: %s videos", c["id"], n)
            except YouTubeError as e:
                log.warning("analytics %s: %s", c["id"], e)


def _retry_or_fail(db, st, job, err):
    if job["attempts"] < st.max_attempts:
        db.requeue(job["id"], 30 * 2 ** job["attempts"], err)
    else:
        db.finish_job(job["id"], "failed", err)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format='{"t":"%(asctime)s","lvl":"%(levelname)s","src":"%(name)s","msg":"%(message)s"}')
    st = settings()
    db = DB(st.db_path)
    wid = f"{socket.gethostname()}:{os.getpid()}"
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    yt = None
    try:
        yt = youtube_client(st)
    except YouTubeError as e:
        log.warning("publishing disabled: %s", e)
    log.info("worker %s up; publishing %s", wid, "on" if yt else "off")
    last: dict = {}
    while not STOP.is_set():
        try:
            if time.time() - last.get("tick", 0) > 300:
                last["tick"] = time.time()
                tick(db, yt, last)
            if not process_one(db, st, wid, yt):
                STOP.wait(5)
        except Exception:
            log.error("worker loop error: %s", traceback.format_exc())
            STOP.wait(10)


if __name__ == "__main__":
    main()
