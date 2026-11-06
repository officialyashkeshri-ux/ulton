# Vibe Editing System (Ideabro Studio) — installed skills

| Skill | Purpose |
|---|---|
| `vibe-editing-system` | Start here: router, setup, picks the video type |
| `vibe-talking-head` | Reels/podcast/YouTube edits from a raw take |
| `vibe-launch-ads` | Promos, launch videos, ad variants |
| `vibe-motion-graphics` | Explainers, kinetic type, logo reveals, packs |
| `vibe-stylised-animation` | Clay/crayon/paper-cut/cartoon animation |
| `vibe-vfx` | Background removal, layers, tracking, looks |
| `vibe-ai-footage` | AI-generated clips into finished films |
| `vibe-audio-videos` | Music, SFX, TTS voiceover, audiograms |
| `vibe-bulk-videos` | CSV-driven / multi-format / multi-language batches |

Guide: `docs/Vibe-Editing-System-Ideabro-Studio.md`.
Setup check: `python3 .claude/skills/vibe-editing-system/scripts/doctor.py --kit all`
(add `--install` to pip-install missing packages; Linux skia also needs `libegl1 libgl1`).

## HyperFrames (HeyGen) plugin

`.claude/settings.json` registers the `heygen-com/hyperframes` marketplace and enables
`hyperframes@hyperframes`, so Claude Code offers to install it when you open this repo
(skills: hyperframes, hyperframes-cli, hyperframes-registry, media-use, figma, slideshow,
style-sampling, reverse-any-video, remotion-to-hyperframes).
Check: `npx hyperframes doctor`. Its optional whisper-cpp is built on first transcription;
optional local music needs `pip install transformers torch soundfile numpy`.
