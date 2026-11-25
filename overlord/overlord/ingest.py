"""Source ingestion: local/uploaded files, archived YouTube videos, and live streams.

Live streams are not treated like finished videos. probe() reports the live state from yt-dlp metadata:
  is_live      -> record_live() captures the stream to disk as it plays (yt-dlp, MPEG-TS, resumable chunks), and the
                  job continues once the broadcast ends or the configured capture limit is reached
  is_upcoming  -> the job waits (requeued) until the stream starts
  post_live    -> YouTube is still processing the archive; the job waits and retries
  was_live / not_live -> ordinary archived download

Every job must carry a rights basis (own channel, licence, clipping-programme permission ...). Overlord does not
bypass DRM, paywalls, logins or platform restrictions.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

RIGHTS_BASES = ("own_content", "licensed", "clipping_program", "creator_permission", "public_domain_or_cc")


class IngestError(RuntimeError):
    pass


class NotReady(IngestError):
    """Source exists but is not processable yet (upcoming / archive still processing). Job is requeued."""


@dataclass
class Probe:
    kind: str  # file | youtube | url
    key: str  # stable dedupe key (video id or sha256)
    live_status: str  # not_live | is_live | is_upcoming | post_live | was_live | file
    title: str
    duration: float | None
    channel: str | None


def _sha256(path: Path, limit: int = 64 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(limit))
    h.update(str(path.stat().st_size).encode())
    return h.hexdigest()[:32]


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise IngestError(f"ffprobe failed on {path.name}: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout)


def probe(source: str) -> Probe:
    p = Path(source)
    if p.exists():
        meta = ffprobe(p)
        if not any(s.get("codec_type") == "audio" for s in meta.get("streams", [])):
            raise IngestError("file has no audio track; Overlord selects clips from speech")
        return Probe("file", "file:" + _sha256(p), "file", p.stem, float(meta["format"].get("duration", 0) or 0), None)
    if not source.startswith(("http://", "https://")):
        raise IngestError(f"not a file or URL: {source!r}")
    import yt_dlp  # local import keeps tests that use files independent of yt-dlp

    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        try:
            info = ydl.extract_info(source, download=False)
        except Exception as e:  # yt-dlp raises many types; report the message plainly
            raise IngestError(f"could not read source: {e}") from e
    kind = "youtube" if "youtube" in (info.get("extractor") or "").lower() else "url"
    return Probe(kind, f"{info.get('extractor')}:{info.get('id')}", info.get("live_status") or "not_live",
                 info.get("title") or "", info.get("duration"), info.get("channel") or info.get("uploader"))


def fetch(source: str, pr: Probe, workdir: Path, max_live_s: int = 4 * 3600, heartbeat=None) -> Path:
    """Return a local media file for the source, downloading or recording as needed. Idempotent: an existing
    complete file in workdir is reused."""
    workdir.mkdir(parents=True, exist_ok=True)
    done = workdir / "source.done"
    if done.exists():
        return Path(done.read_text().strip())
    if pr.kind == "file":
        dst = workdir / ("source" + Path(source).suffix.lower())
        if not dst.exists():
            shutil.copy2(source, dst)
        done.write_text(str(dst))
        return dst
    if pr.live_status in ("is_upcoming", "post_live"):
        raise NotReady(f"stream state is {pr.live_status}; will retry later")
    if pr.live_status == "is_live":
        dst = record_live(source, workdir, max_live_s, heartbeat)
    else:
        dst = _download(source, workdir)
    done.write_text(str(dst))
    return dst


def _download(url: str, workdir: Path) -> Path:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "outtmpl": str(workdir / "source.%(ext)s"),
            "format": "bv*[height<=1080]+ba/b[height<=1080]/b", "merge_output_format": "mp4", "continuedl": True,
            "retries": 5, "fragment_retries": 10}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            raise IngestError(f"download failed: {e}") from e
    files = sorted(workdir.glob("source.*"), key=lambda f: f.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix not in (".part", ".ytdl", ".done")]
    if not files:
        raise IngestError("download produced no file")
    return files[0]


def record_live(url: str, workdir: Path, max_s: int, heartbeat=None) -> Path:
    """Record a live stream from its current point until it ends or max_s passes. Runs yt-dlp in a subprocess so
    the worker can keep its job lease alive (heartbeat) while recording."""
    dst = workdir / "live.ts"
    cmd = ["yt-dlp", "--no-part", "--hls-use-mpegts", "-f", "b[height<=1080]/bv*[height<=1080]+ba/b", "-o", str(dst),
           url]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    while proc.poll() is None:
        if heartbeat:
            heartbeat()
        if time.time() - t0 > max_s:
            proc.terminate()
            proc.wait(30)
            break
        time.sleep(15)
    if not dst.exists() or dst.stat().st_size < 1_000_000:
        err = (proc.stderr.read() if proc.stderr else "")[-400:]
        raise IngestError(f"live recording failed or too short: {err}")
    mp4 = workdir / "source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(dst), "-c", "copy", "-movflags", "+faststart", str(mp4)],
                   check=True)
    return mp4
