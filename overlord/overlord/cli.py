"""Terminal front door.

  python -m overlord.cli client "My Channel"            create a client, prints its id
  python -m overlord.cli run SOURCE --client ID         asks how many clips (or --clips N) and the rights basis,
                                                        then runs the whole pipeline in this process
  python -m overlord.cli enqueue SOURCE --client ID --clips N --rights own_content   queue for the workers
  python -m overlord.cli show JOB_ID                    clip table with QC, metadata and upload state
  python -m overlord.cli approve CLIP_ID | reject CLIP_ID
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import settings
from .db import DB
from .ingest import RIGHTS_BASES, probe
from .profiles import create_client


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="overlord")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("client"); c.add_argument("name")
    for name in ("run", "enqueue"):
        r = sub.add_parser(name)
        r.add_argument("source"); r.add_argument("--client", required=True); r.add_argument("--clips", type=int)
        r.add_argument("--rights", choices=RIGHTS_BASES); r.add_argument("--min", type=float, default=15)
        r.add_argument("--max", type=float, default=59); r.add_argument("--publish", action="store_true",
                                                                         help="publish per the client's policy")
    s = sub.add_parser("show"); s.add_argument("job")
    for v in ("approve", "reject"):
        x = sub.add_parser(v); x.add_argument("clip")
    a = ap.parse_args(argv)
    st = settings()
    db = DB(st.db_path)
    if a.cmd == "client":
        print(create_client(db, a.name))
        return 0
    if a.cmd in ("run", "enqueue"):
        pr = probe(a.source)
        print(f"Analysed source: {pr.title!r} · {pr.live_status} · {(pr.duration or 0) / 60:.1f} min")
        n = a.clips or int(input("How many clips would you like? ").strip())
        rights = a.rights or input(f"Rights basis {RIGHTS_BASES}: ").strip()
        if rights not in RIGHTS_BASES:
            print("A rights basis is required."); return 2
        jid, created = db.create_job(a.client, a.source, pr.key, rights, n, a.min, a.max)
        print(f"Job {jid} {'queued' if created else 'already exists'}. I'll find the strongest {n} independent "
              f"moments, edit them, quality-check them and prepare metadata.")
        if a.cmd == "run":
            from .worker import process_one, youtube_client
            yt = youtube_client(st) if a.publish else None
            while process_one(db, st, "cli", yt):
                pass
            return _show(db, jid)
        return 0
    if a.cmd == "show":
        return _show(db, a.job)
    db.run("UPDATE clips SET approval=? WHERE id=?", "approved" if a.cmd == "approve" else "rejected", a.clip)
    db.audit("cli", f"clip.{a.cmd}", a.clip)
    return 0


def _show(db: DB, jid: str) -> int:
    j = db.one("SELECT * FROM jobs WHERE id=?", jid)
    print(f"\nJob {jid}: {j['status']} ({j['stage']})" + (f" — {j['error']}" if j["error"] else ""))
    rep = (db.checkpoint(jid, "select") or {}).get("report", {})
    if rep:
        print(f"Candidates {rep.get('pool')} → selected {rep.get('selected')}/{rep.get('requested')} "
              f"[{rep.get('scoring_source')}]" + (f"; {rep.get('shortfall_reason')}" if rep.get("shortfall") else ""))
    for c in db.all("SELECT * FROM clips WHERE job_id=? ORDER BY rank", jid):
        qc = json.loads(c["qc_json"] or "{}")
        seo = json.loads(c["seo_json"] or "{}")
        up = db.one("SELECT state,url,publish_at FROM uploads WHERE clip_id=?", c["id"]) or {}
        print(f"#{c['rank']} {c['id']} {c['start']:.1f}-{c['end']:.1f}s score {c['score']:.2f} "
              f"QC {'PASS' if qc.get('pass') else 'FAIL ' + ','.join(qc.get('critical_failed', []))} "
              f"| {seo.get('title', '')!r} | {up.get('state', c['approval'])} {up.get('url') or ''}")
        if c["path"]:
            print(f"    {c['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
