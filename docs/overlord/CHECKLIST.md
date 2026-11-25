# Overlord implementation checklist

Status legend: ✅ built and tested · 🟡 built, not yet verified against the live service · ⛔ blocked (reason) · ⬜ not built

Last updated: 2026-11-25 (session 0dab6155).

## Phases
| Phase | Status | Evidence |
|---|---|---|
| 1 Inspect existing system | ✅ | NeonClipping audited (AGPL, no telemetry, binds 127.0.0.1); ulton repo mapped in `CLAUDE.md` |
| 2 Checkpoint | ✅ | local tag `checkpoint/pre-overlord-2026-11-25` on main@726aafd; work on branch `claude/editing-system-install-huh9hy` |
| 3 Gaps + roadmap | ✅ | this file + `ROADMAP.md` |
| 4 Core pipeline | ✅ | `tests/test_e2e.py::test_full_pipeline_approval_then_publish`; real Hindi footage run (3/3 clips QC pass) |
| 5 Intelligence + editing | ✅ (heuristic) / 🟡 (Claude layer) | heuristic tested; Claude layer written to current API docs but no Anthropic key in this environment |
| 6 Quality control | ✅ | 15 checks, critical gate, auto re-render; tests assert resolution/codec/loudness |
| 7 Composio + YouTube | 🟡 | Live read verified (channel lookup through Composio proxy). Upload/schedule tested with FakeYouTube only — **no real upload yet** (needs owner's go-ahead + COMPOSIO_API_KEY on the server) |
| 8 Analytics + feedback | ✅ (logic) / 🟡 (live) | collect/recommend tested with fake analytics |
| 9 Cloud deployment | ⛔ | Dockerfile + compose ready; **no hosting account** available to this session (see DEPLOY.md) |
| 10 E2E tests | ✅ | 8 unit + 6 e2e passing (crash-resume, retries, dedupe, revoked auth, chat API, permission reference) |
| 11 Deploy + verify | ⛔ | depends on 9 |
| 12 Commit + push | ✅ | branch pushed; PR opened |

## Known issues / limits (honest)
- No speaker diarisation: multi-speaker podcasts are clipped by content, not by speaker; captions have no speaker names.
- No B-roll or overlay insertion yet (deliberately: only add when it serves the clip; needs an asset source).
- Heuristic scores are proxies (energy, cue words, structure). They pick sensible moments but cannot judge humour or
  context like a person; enable the Claude layer (ANTHROPIC_API_KEY) for semantic judgement.
- YouTube downloads from cloud IPs are often blocked by YouTube ("sign in to confirm you're not a bot"). Owner's own
  videos can be fetched via authorised routes later; uploads of files always work.
- Raw Video #3 contains frame blending (ghosted frames at ~11 s) in the source itself.
- Custom thumbnails for Shorts are not settable through the API in most cases; a cover JPG is produced anyway.
- SQLite = one host. Multi-host scaling needs Postgres + S3 (ROADMAP).

## Session log
- 2026-11-25: third-party content now needs a rights reference (campaign link / written permission) before any
  upload (`jobs.rights_ref`, chat asks for it). Owner asked to mass-clip an IShowSpeed stream (Content ID, not theirs);
  declined until a campaign link exists. See CLIPPING-PROGRAMS.md.
- Composio API key: the value first saved on the environment was a YouTube URL, not a key (Composio returned 401).
  Needs replacing with the key from Composio dashboard → Settings → API Keys (environment → Network secrets,
  host backend.composio.dev, header x-api-key).
