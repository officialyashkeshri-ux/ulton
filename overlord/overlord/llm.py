"""Optional Claude layer: semantic judgement of the top candidates, and per-clip YouTube metadata.

Used only when an Anthropic credential is configured (Settings.has_llm). Every call:
  * uses structured JSON output, so results are parsed, never scraped from prose
  * checks stop_reason (refusal / max_tokens) before reading content
  * enables server-side refusal fallbacks
Failures degrade to the heuristic path instead of failing the job.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("overlord.llm")

SEMANTIC_DIMS = ["hook", "curiosity", "standalone", "information", "emotion", "shareability"]

_SCORE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, **{d: {"type": "integer"} for d in SEMANTIC_DIMS},
                       "misleading_if_clipped": {"type": "boolean"}, "why": {"type": "string"}},
        "required": ["id", *SEMANTIC_DIMS, "misleading_if_clipped", "why"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False}

_SEO_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "description": {"type": "string"},
                   "tags": {"type": "array", "items": {"type": "string"}},
                   "hashtags": {"type": "array", "items": {"type": "string"}},
                   "hook_text": {"type": "string"}},
    "required": ["title", "description", "tags", "hashtags", "hook_text"], "additionalProperties": False}


def _client():
    import anthropic

    return anthropic.Anthropic()


def _call(model: str, system: str, user: str, schema: dict, max_tokens: int = 16000) -> dict | None:
    import anthropic

    try:
        resp = _client().beta.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    except anthropic.RateLimitError as e:
        log.warning("Claude rate limited: %s", e)
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude API error %s: %s", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("Claude unreachable: %s", e)
        return None
    if resp.stop_reason in ("refusal", "max_tokens"):
        log.warning("Claude stopped with %s", resp.stop_reason)
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    return json.loads(text) if text else None


def judge(model: str, candidates: list, language: str, niche: str = "") -> dict[int, dict]:
    """Return {candidate_index: {dim: 0..1, misleading: bool, why: str}} for semantic dimensions."""
    lines = [f"[{i}] ({c.dur:.0f}s) {c.text}" for i, c in enumerate(candidates)]
    system = ("You rate candidate short-form video clips cut from one long recording. Judge each clip only as a "
              "viewer who has NOT seen the rest of the recording. Score 0-10 per dimension; be strict and use the "
              "full range. Flag misleading_if_clipped when cutting the clip out would change the speaker's meaning.")
    user = (f"Language: {language}. Niche: {niche or 'unspecified'}.\nDimensions: hook (first 3 seconds make you "
            "stay), curiosity, standalone (understandable without context), information, emotion, shareability.\n\n"
            + "\n".join(lines))
    out = _call(model, system, user, _SCORE_SCHEMA)
    res = {}
    for it in (out or {}).get("items", []):
        i = it.get("id")
        if isinstance(i, int) and 0 <= i < len(candidates):
            res[i] = {d: max(0, min(10, it[d])) / 10 for d in SEMANTIC_DIMS}
            res[i]["misleading"] = bool(it["misleading_if_clipped"])
            res[i]["why"] = it["why"][:300]
    return res


def seo(model: str, clip_text: str, language: str, channel_context: str, glossary: list[str], avoid: list[str]) -> dict | None:
    system = ("You write YouTube Shorts metadata that is accurate to the clip. Never promise what the clip does not "
              "show, never use clickbait that misrepresents it, no keyword stuffing, no engagement bait. Title under "
              "70 characters in the clip's language/script style. Description: 2-3 sentences of context plus one "
              "natural call to action. 5-12 tags. 2-4 hashtags including #shorts.")
    user = (f"Clip transcript ({language}):\n{clip_text}\n\nChannel context: {channel_context}\n"
            f"Spell these names exactly: {', '.join(glossary) or 'none'}\nNever use: {', '.join(avoid) or 'none'}")
    return _call(model, system, user, _SEO_SCHEMA, max_tokens=4000)
