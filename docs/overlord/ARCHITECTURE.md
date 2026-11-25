# Overlord architecture

## Components
| # | Component | Implementation | Inputs → outputs | Failure / recovery |
|---|---|---|---|---|
| 1 | Frontend | `overlord/web/index.html` (served by the API) | browser → API | stateless; token kept in the browser |
| 2 | Backend API | FastAPI `overlord/api.py` | chat, uploads, reviews, settings → DB rows | bearer-token auth; refuses to start without a token |
| 3 | AI orchestration | `pipeline.py` | job row → checkpoints, clips | each stage idempotent + checkpointed |
| 4 | Ingestion | `ingest.py` (files, yt-dlp archive download, live recording) | source → `src/source.*` | `NotReady` requeues (upcoming / processing archive); `IngestError` fails with reason |
| 5 | Transcription | `transcribe.py` (faster-whisper, word timestamps, number re-joining) | media → `transcript.json` | cached on disk; reused after a crash |
| 6 | Content analysis | `analyze.py` (units, energy, rate, cues, TextTiling topics), `scoring.py`, optional `llm.py` | words → ranked candidates + report | heuristic path always works; Claude failures fall back |
| 7 | Rendering | `render.py` (YuNet face reframe, pacing, punch-ins, ASS captions, loudnorm) | moment → 1080x1920 MP4 + thumb | per-clip; failure never fails other clips |
| 8 | Job queue | `jobs` table + leases (`db.claim_job`, heartbeats) | queued → running → done/partial/failed/needs_user | expired lease ⇒ another worker resumes; retries with backoff |
| 9 | Database | SQLite WAL (`db.py`) | — | single host; move to Postgres for multi-host (see ROADMAP) |
| 10 | Object storage | volume at `$OVERLORD_DATA/jobs/<id>/` | media, transcripts, clips | — |
| 11 | Composio integration | `publish/youtube.py` (`tools.execute` upload, `tools.proxy` Data API) | clip → private video + publishAt | errors classified: retryable / needs_user / fatal |
| 12 | Publishing workers | `publish/publisher.py`, `publish/scheduler.py`, worker `tick()` | approved/eligible clips → scheduled videos | UNIQUE(clip_id), content-hash dedupe, schedule verified via videos.list |
| 13 | Analytics ingestion | `learning.collect` (daily in `tick`) | YouTube Analytics → `metrics` | only videos older than 2 days (reporting delay) |
| 14 | Monitoring | `/api/health`, JSON logs, `audit` table, `usage` table, Docker HEALTHCHECK | — | needs_user jobs surface on the dashboard |

## Job lifecycle
```
queued ──claim──▶ running ──▶ done | partial | failed | needs_user
   ▲                 │
   └─ requeue(delay) ┘  (NotReady, retryable error, crashed worker = expired lease)
```
Stages and checkpoints: `ingest` → `transcribe` (transcript.json) → `select` (clips rows + report) → `seo`
(clips.seo_json) → `render+qc` (clips.path/qc_json) → `publish` (uploads rows).

## Publishing state machine
`pending_approval → processing → uploaded → scheduled → published`, side exits `failed`, `needs_user`.
- Upload is always **private**; scheduling is `PUT /youtube/v3/videos?part=status` with `status.publishAt` (via
  Composio proxy, authenticated as the owner's channel). YouTube publishes at that time with no Overlord server running.
- `scheduled` is written only after `GET videos?part=status` returns the same `publishAt`.
- A clip found `processing` without a video id (crash mid-upload) goes to `needs_user`, never blindly re-uploaded.
- Revoked/expired auth stops the batch at the first failure and flags `needs_user` once.

## Scoring (explainable)
12 dimensions (hook, curiosity, relevance, emotion, information, completeness, retention, shareability, replay,
standalone, originality, pacing), each 0–1 with a recorded reason. Per-client weights (`profile.score_weights`).
Selection: pool → threshold → time-overlap ≤25% → TF-IDF similarity ≤0.55 → top N; shortfall is reported, never
padded. Leading connectives (so/and/तो/और/जहाँ पर/जिसके लिए) are trimmed; contrast/cause words are not.

## Learning loop
`learning.recommend`: Spearman correlation of each dimension with average-view-percentage across ≥12 published clips;
weights move at most ±15% per update; written as a new profile version (rollback-able). Reported as correlation.
