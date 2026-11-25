# Third-party files bundled with Overlord

| File | Source | Licence |
|---|---|---|
| `fonts/Anton-400.ttf` | Anton by Vernon Adams, Google Fonts | SIL Open Font License 1.1 (https://openfontlicense.org) |
| `fonts/InterTight-400.ttf`, `fonts/InterTight-700.ttf` | Inter Tight by Rasmus Andersson, Google Fonts | SIL Open Font License 1.1 |
| `fonts/NotoSansDevanagari-700.ttf` | Noto Sans Devanagari, Google Fonts | SIL Open Font License 1.1 |
| `models/face_detection_yunet_2023mar.onnx` | OpenCV Zoo YuNet (huggingface.co/opencv/face_detection_yunet) | MIT |

Runtime dependencies (installed by pip, not bundled): faster-whisper (MIT), OpenCV (Apache-2.0), FastAPI (MIT),
yt-dlp (Unlicense), composio (MIT/Apache per package), anthropic (MIT). FFmpeg is a system package (LGPL/GPL build).
Overlord contains no NeonClipping code.
