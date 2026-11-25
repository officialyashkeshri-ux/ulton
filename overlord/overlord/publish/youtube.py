"""YouTube operations through Composio (the owner's channel is authorised once in Composio; Overlord never sees
the OAuth tokens). Two transports implement one interface:

  ComposioYouTube   real calls: Composio tool YOUTUBE_UPLOAD_VIDEO for the upload (the SDK uploads the local file),
                    and Composio's authenticated proxy to the YouTube Data API v3 for scheduling (videos.update with
                    status.publishAt) and verification (videos.list part=status)
  FakeYouTube       in-memory stand-in for tests; same behaviour contract

Scheduling uses YouTube's own scheduler: the video is uploaded private with status.publishAt set, and YouTube makes
it public at that time even if every Overlord server is off.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Protocol

API = "https://www.googleapis.com/youtube/v3"


class YouTubeError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False, needs_user: bool = False):
        super().__init__(msg)
        self.retryable, self.needs_user = retryable, needs_user


class YouTube(Protocol):
    def channel(self) -> dict: ...
    def upload_private(self, path: str, title: str, description: str, tags: list[str], category_id: str) -> str: ...
    def set_schedule(self, video_id: str, publish_at_iso: str, made_for_kids: bool) -> None: ...
    def video_status(self, video_id: str) -> dict: ...
    def analytics(self, video_ids: list[str], start: str, end: str) -> list[dict]: ...


def _classify(status: int, body) -> YouTubeError:
    text = str(body)[:400]
    if status in (401,) or "invalid_grant" in text or "revoked" in text.lower():
        return YouTubeError(f"YouTube authorisation is no longer valid: {text}", needs_user=True)
    if status == 403 and ("quotaExceeded" in text or "uploadLimitExceeded" in text or "rateLimitExceeded" in text):
        return YouTubeError(f"YouTube quota/limit reached: {text}", retryable=True)
    if status == 403:
        return YouTubeError(f"YouTube refused the request: {text}", needs_user=True)
    if status >= 500 or status == 429:
        return YouTubeError(f"YouTube temporary error {status}: {text}", retryable=True)
    return YouTubeError(f"YouTube error {status}: {text}")


class ComposioYouTube:
    def __init__(self, user_id: str | None = None, account_id: str | None = None):
        from composio import Composio

        if not os.environ.get("COMPOSIO_API_KEY"):
            raise YouTubeError("COMPOSIO_API_KEY is not set on this server", needs_user=True)
        self.c = Composio()
        self.user_id = user_id
        self.account_id = account_id

    def _proxy(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None) -> dict:
        prm = [{"name": k, "value": str(v), "type": "query"} for k, v in (params or {}).items()]
        try:
            r = self.c.tools.proxy(endpoint=f"{API}{endpoint}", method=method, body=body, parameters=prm or None,
                                   connected_account_id=self.account_id, user_id=self.user_id)
        except Exception as e:  # network / auth errors from the SDK
            raise YouTubeError(f"Composio proxy failed: {e}", retryable=True) from e
        status = int(getattr(r, "status", 0) or 0)
        data = getattr(r, "data", None)
        if status >= 300 or status == 0:
            raise _classify(status, data)
        return data or {}

    def channel(self) -> dict:
        d = self._proxy("GET", "/channels", {"part": "snippet,status,statistics", "mine": "true"})
        items = d.get("items") or []
        if not items:
            raise YouTubeError("the connected Google account has no YouTube channel", needs_user=True)
        return items[0]

    def upload_private(self, path, title, description, tags, category_id) -> str:
        try:
            r = self.c.tools.execute("YOUTUBE_UPLOAD_VIDEO", {
                "title": title, "description": description, "tags": tags, "categoryId": category_id,
                "privacyStatus": "private", "videoFilePath": path},
                user_id=self.user_id, connected_account_id=self.account_id, dangerously_skip_version_check=True)
        except Exception as e:
            raise YouTubeError(f"upload call failed: {e}", retryable=True) from e
        res = r if isinstance(r, dict) else getattr(r, "model_dump", lambda: r)()
        if not res.get("successful"):
            raise _classify(0 if "quota" not in str(res.get("error")) else 403, res.get("error"))
        data = res.get("data") or {}
        vid = data.get("id") or (data.get("response_data") or {}).get("id") or (data.get("data") or {}).get("id")
        if not vid:
            raise YouTubeError(f"upload returned no video id: {str(data)[:300]}")
        return vid

    def set_schedule(self, video_id, publish_at_iso, made_for_kids) -> None:
        self._proxy("PUT", "/videos", {"part": "status"}, {"id": video_id, "status": {
            "privacyStatus": "private", "publishAt": publish_at_iso, "selfDeclaredMadeForKids": made_for_kids}})

    def video_status(self, video_id) -> dict:
        d = self._proxy("GET", "/videos", {"part": "status,processingDetails", "id": video_id})
        items = d.get("items") or []
        if not items:
            raise YouTubeError(f"video {video_id} not found on the channel")
        return items[0]

    def analytics(self, video_ids, start, end) -> list[dict]:
        metrics = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained"
        try:
            r = self.c.tools.execute("YOUTUBE_QUERY_ANALYTICS", {
                "ids": "channel==MINE", "startDate": start, "endDate": end, "dimensions": "video",
                "metrics": metrics, "filters": "video==" + ",".join(video_ids[:200]), "sort": "-views",
                "maxResults": 200}, user_id=self.user_id, connected_account_id=self.account_id,
                dangerously_skip_version_check=True)
        except Exception as e:
            raise YouTubeError(f"analytics call failed: {e}", retryable=True) from e
        res = r if isinstance(r, dict) else getattr(r, "model_dump", lambda: r)()
        if not res.get("successful"):
            raise YouTubeError(f"analytics error: {res.get('error')}", retryable=True)
        data = res.get("data") or {}
        heads = [h["name"] for h in data.get("columnHeaders", [])]
        return [dict(zip(heads, row)) for row in data.get("rows", [])]


class FakeYouTube:
    """Deterministic in-memory YouTube used by tests (no network)."""

    def __init__(self, fail_upload_times: int = 0, quota: int = 50):
        self.videos: dict[str, dict] = {}
        self.fail_upload_times = fail_upload_times
        self.quota = quota
        self.calls: list[str] = []

    def channel(self):
        return {"id": "UCfake", "snippet": {"title": "Fake channel"}}

    def upload_private(self, path, title, description, tags, category_id):
        self.calls.append("upload")
        if self.fail_upload_times > 0:
            self.fail_upload_times -= 1
            raise YouTubeError("simulated 503", retryable=True)
        if self.quota <= 0:
            raise YouTubeError("quotaExceeded", retryable=True)
        self.quota -= 1
        vid = uuid.uuid4().hex[:11]
        self.videos[vid] = {"title": title, "status": {"privacyStatus": "private", "uploadStatus": "processed"},
                            "path": path, "at": time.time()}
        return vid

    def set_schedule(self, video_id, publish_at_iso, made_for_kids):
        self.calls.append("schedule")
        self.videos[video_id]["status"].update({"publishAt": publish_at_iso, "selfDeclaredMadeForKids": made_for_kids})

    def video_status(self, video_id):
        return {"id": video_id, "status": dict(self.videos[video_id]["status"])}

    def analytics(self, video_ids, start, end):
        return [{"video": v, "views": 100 + i * 10, "averageViewPercentage": 60 - i, "estimatedMinutesWatched": 30,
                 "averageViewDuration": 20, "likes": 5, "comments": 1, "shares": 0, "subscribersGained": 0}
                for i, v in enumerate(video_ids)]
