# HyperFrames test reel

A 16.6 s vertical (1080×1920, 30 fps) test reel built with HyperFrames: silence-cut take, pop captions with lime keyword highlights, and punch-in zooms on alternate jump cuts. Audio is the voice + music + whoosh mix at −14 LUFS.

- `index.html`: root composition (stage, captions, audio mix)
- `compositions/stage.html`: the take inside a camera wrapper; punch-ins are tweens on `#stage-cam`
- `compositions/captions.html`: caption groups timed to each word
- `assets/`: `take.mp4` (silence-cut take) and `mix.wav` (final audio mix)
- `data/`: word timings (`words_cut.json`) and cut list (`edl.json`) the compositions are generated from
- `tools/build_hf.py`: regenerates `index.html` and `compositions/` from `data/`, so you can change caption or zoom rules and rebuild
- `out/hf-reel_v1.mp4`: the rendered reel

```bash
python3 tools/build_hf.py .
npx hyperframes check
npx hyperframes preview
npx hyperframes render -o out/hf-reel_v2.mp4
```
