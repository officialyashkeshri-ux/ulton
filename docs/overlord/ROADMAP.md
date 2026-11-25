# Overlord roadmap and decisions

## Decisions (and why)
- **Own pipeline instead of forking NeonClipping**: NeonClipping became AGPL-3.0 on 2026-11-24; a modified copy run
  as a service would force publishing Overlord's source under AGPL. Overlord reuses only permissively licensed parts
  (faster-whisper MIT, OpenCV YuNet MIT, ffmpeg, fonts under OFL).
- **SQLite + local volume first**: one VM covers the current workload at the lowest cost; leases and checkpoints are
  already database-level so moving to Postgres is a driver swap, not a redesign.
- **YouTube's own scheduler (`status.publishAt`)**, not a local calendar: scheduled videos publish even if every
  Overlord process is down.
- **Approval mode default**: autonomous publishing is a stored per-client policy the owner switches on once.
- **Heuristic scoring always available, Claude optional**: the system never stops working because an API key is
  missing; every score is explainable either way.

## Next (in priority order)
1. Live verification with the owner: one real private upload + schedule to "Staeamers clip", confirm publishAt.
2. Deploy to a VPS (DEPLOY.md) and point the dashboard at it.
3. Speaker diarisation (pyannote or NeMo) for podcasts: speaker-aware moments and captions.
4. Live streams: rolling-window processing while the stream is still running (today: record then clip).
5. B-roll/overlay engine driven by the transcript (only where it improves clarity).
6. Experiment framework: A/B assignment of caption style / hook type per clip, analysed per cohort.
7. Postgres + S3-compatible storage for multi-host scaling; per-client rate limits and quota accounting.
