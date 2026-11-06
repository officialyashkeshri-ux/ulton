"""Generate the HyperFrames reel (index.html + compositions) from word timings and the cut EDL."""
import json
import sys
from pathlib import Path

P = Path(sys.argv[1])
words = json.loads((P / "data" / "words_cut.json").read_text())
edl = json.loads((P / "data" / "edl.json").read_text())
DUR = 16.6
LIME, INK, PAPER = "#D4FF3F", "#0B0B0D", "#F2EEE3"
EMPH = {"keyframes", "plugins", "claude", "zoom", "vibe", "captions", "app"}


def clean(w):
    return w.strip().strip(".,!?").upper()


# ---- caption groups: 2-3 words, break at sentence end or a pause >= 0.15 s
groups, cur = [], []
for i, w in enumerate(words):
    cur.append(w)
    nxt = words[i + 1] if i + 1 < len(words) else None
    end_sentence = w["w"].strip()[-1:] in ".!?,"
    pause = nxt is not None and nxt["s"] - w["e"] >= 0.15
    chars = sum(len(clean(x["w"])) for x in cur) + len(cur) - 1
    nxt_chars = chars + 1 + len(clean(nxt["w"])) if nxt else 0
    if len(cur) == 3 or nxt_chars > 13 or end_sentence or pause or nxt is None:
        groups.append(cur)
        cur = []

cap_html, cap_js = [], []
for gi, g in enumerate(groups):
    start = max(0.12, g[0]["s"] - 0.04)
    nxt_start = groups[gi + 1][0]["s"] - 0.04 if gi + 1 < len(groups) else DUR - 0.05
    end = min(max(g[-1]["e"] + 0.15, start + 0.4), nxt_start, DUR - 0.05)
    gid = f"cap-g{gi}"
    spans = []
    for wi, w in enumerate(g):
        wid = f"{gid}-w{wi}"
        spans.append(f'<span id="{wid}" class="w">{clean(w["w"])}</span>')
        ws = max(w["s"], start)
        hi = LIME if clean(w["w"]).lower() in EMPH else "#FFFFFF"
        cap_js.append(f'tl.set("#{wid}", {{ color: "{hi}", scale: 1.06 }}, {ws:.3f});')
        cap_js.append(f'tl.set("#{wid}", {{ color: "#FFFFFF", scale: 1 }}, {min(w["e"], end - 0.01):.3f});')
    cap_html.append(
        f'    <div id="{gid}" class="clip grp" data-start="{start:.3f}" data-duration="{end - start:.3f}" '
        f'data-track-index="{gi % 2}">\n      <div class="line">{" ".join(spans)}</div>\n    </div>')
    cap_js.append(f'tl.fromTo("#{gid} .line", {{ scale: 0.6, opacity: 0, y: 30 }}, '
                  f'{{ scale: 1, opacity: 1, y: 0, duration: 0.18, ease: "back.out(2.2)" }}, {start:.3f});')
    cap_js.append(f'tl.to("#{gid} .line", {{ opacity: 0, duration: 0.08, ease: "power2.in" }}, {end - 0.08:.3f});')

captions = f"""<template>
  <style>
    #root {{ position: absolute; inset: 0; }}
    .grp {{ position: absolute; left: 0; right: 0; top: 1150px; text-align: center; overflow: visible; }}
    .line {{ display: inline-block; font-family: "Archivo Black"; font-size: 96px; line-height: 1.05;
      letter-spacing: -0.01em; color: #FFFFFF; text-shadow: -6px 0px 0 {INK}, 6px 0px 0 {INK}, 0px -6px 0 {INK}, 0px 6px 0 {INK}, -4px -4px 0 {INK}, 4px -4px 0 {INK}, -4px 4px 0 {INK}, 4px 4px 0 {INK}, 0px 12px 0 {INK}, -4px 12px 0 {INK}, 4px 12px 0 {INK}; }}
    .w {{ display: inline-block; margin: 0 16px; }}
  </style>
  <div id="root" data-composition-id="captions" data-width="1080" data-height="1920">
{chr(10).join(cap_html)}
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    {chr(10) + '    '.join(cap_js)}
    window.__timelines["captions"] = tl;
  </script>
</template>
"""

# ---- stage: the take inside a camera wrapper; hard punch-ins on alternate jump cuts
cam_js = []
for seg in edl:
    z = 1.15 if seg["zoom"] > 1.0 else 1.0
    t = seg["out_s"]
    if t == 0:
        cam_js.append(f'tl.set("#stage-cam", {{ scale: {z} }}, 0);')
    else:
        # snap on the cut, then a slow 2% push while the segment holds
        cam_js.append(f'tl.set("#stage-cam", {{ scale: {z} }}, {t:.3f});')
    cam_js.append(f'tl.to("#stage-cam", {{ scale: {z * 1.02:.4f}, duration: {seg["out_e"] - t - 0.02:.3f}, ease: "none" }}, {t:.3f});')

stage = f"""<template>
  <style>
    #root {{ position: absolute; inset: 0; overflow: hidden; background: {PAPER}; }}
    #stage-cam {{ position: absolute; inset: 0; transform-origin: 50% 38%; }}
    #stage-take {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
  </style>
  <div id="root" data-composition-id="stage" data-width="1080" data-height="1920">
    <div id="stage-cam" data-layout-allow-overflow>
      <video id="stage-take" class="clip" src="assets/take.mp4" data-start="0" data-duration="{DUR}"
        data-track-index="0" muted playsinline></video>
    </div>
  </div>
  <script>
    const tl = gsap.timeline({{ paused: true }});
    {chr(10) + '    '.join(cam_js)}
    window.__timelines["stage"] = tl;
  </script>
</template>
"""

index = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1080, height=1920" />
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ width: 1080px; height: 1920px; overflow: hidden; background: {PAPER}; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{DUR}" data-width="1080" data-height="1920">
      <div id="stage" data-composition-id="stage" data-composition-src="compositions/stage.html"
        data-start="0" data-duration="{DUR}" data-track-index="0" data-track-kind="graphics"
        data-width="1080" data-height="1920" style="z-index: 1"></div>
      <div id="captions" data-composition-id="captions" data-composition-src="compositions/captions.html"
        data-start="0" data-duration="{DUR}" data-track-index="1" data-track-kind="captions"
        data-width="1080" data-height="1920" style="z-index: 2"></div>
      <audio id="mix" class="clip" src="assets/mix.wav" data-start="0" data-duration="{DUR}"
        data-track-index="2" data-volume="1"></audio>
    </div>
    <script>
      window.__timelines["main"] = gsap.timeline({{ paused: true }});
    </script>
  </body>
</html>
"""

(P / "compositions" / "captions.html").write_text(captions)
(P / "compositions" / "stage.html").write_text(stage)
(P / "index.html").write_text(index)
print(f"{len(groups)} caption groups, {len(edl)} camera segments")
