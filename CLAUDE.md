# ulton repository — project memory

Read this first in every session. Keep it short; details live in the linked docs.

## What lives here
| Path | What | Notes |
|---|---|---|
| `index.html`, `sw.js`, `manifest.webmanifest`, `icon-*` | **ULTON** personal-assistant PWA (GitHub Pages: officialyashkeshri-ux.github.io/ulton) | Static site; do not break the Pages build |
| `.claude/skills/vibe-*` | Ideabro Vibe Editing System skills (9) | Installed from the owner's Drive; scripts are self-contained Python |
| `.claude/settings.json` | Enables the HyperFrames plugin marketplace | Do not overwrite |
| `reels/` | Finished HyperFrames reel projects | Owner's work; never delete |
| `overlord/` | **Overlord**: clip intelligence + editing + QC + YouTube publishing engine | See `docs/overlord/` |
| `docs/overlord/` | Architecture, implementation checklist, decisions, deployment | Update CHECKLIST.md after every milestone |

## Overlord in one paragraph
Source (YouTube URL / livestream / uploaded file + rights basis) → faster-whisper word timestamps → speech units
(punctuation, pauses, audio dips, clause words) → sentence-aligned candidates → 12 explainable scores (+ optional
Claude judgement) → ranked, de-duplicated selection with honest shortfall → face-tracked 9:16 render with word
captions → automatic QC (critical checks block publishing, recoverable ones re-render) → metadata → YouTube upload
via Composio, scheduled with YouTube's own `status.publishAt` and verified → analytics → bounded weight updates.

## Rules for working on Overlord
- Run `cd overlord && python3 -m pytest -q tests/` before committing (e2e tests take ~8 min; they generate their own media).
- Approval mode is the default (`publish.autonomous=False`); only the owner turns autonomous publishing on.
- Never log or store secrets; they come from env vars only (`.env.example` lists them).
- Client profiles are versioned in the DB (`profile_versions`); change them with `update_profile`, never raw SQL.
- NeonClipping (github.com/iamajmeena/neonclipping) is **AGPL-3.0** since 2026-11-24. Do not copy its code into
  Overlord (Overlord runs as a network service and would have to be AGPL). It is installed separately at
  `/opt/apps/neonclipping` in cloud sessions, unmodified, if needed at all.
- Owner's YouTube channel: "Staeamers clip" (@risetalesop, UC-I-QR2qaEG6bwPpz93FHhg), connected in Composio.
