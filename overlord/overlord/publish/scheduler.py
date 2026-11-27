"""Publishing calendar: next free slots within the client's windows, never more than max_per_day per local day.

Slots already taken (scheduled or published uploads) are respected, so restarts and re-runs never double-book.
Preferred windows can be re-ranked from analytics (learning.best_windows) but the daily cap is never exceeded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def next_slots(n: int, policy: dict, taken_iso: list[str], now: datetime | None = None,
               min_lead: timedelta = timedelta(hours=1)) -> list[str]:
    tz = ZoneInfo(policy.get("timezone", "UTC"))
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    windows = sorted(policy.get("windows") or ["18:00"])
    cap = max(1, int(policy.get("max_per_day", 1)))
    per_day: dict[str, int] = {}
    taken = set()
    for iso in taken_iso:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
        per_day[d.date().isoformat()] = per_day.get(d.date().isoformat(), 0) + 1
        taken.add(d.replace(second=0, microsecond=0))
    out: list[str] = []
    day = now.date()
    for _ in range(400):  # look ahead at most ~13 months
        key = day.isoformat()
        for w in windows:
            if len(out) >= n:
                return out
            if per_day.get(key, 0) >= cap:
                break
            hh, mm = (int(x) for x in w.split(":"))
            slot = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz)
            if slot < now + min_lead or slot in taken:
                continue
            out.append(slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            per_day[key] = per_day.get(key, 0) + 1
            taken.add(slot)
        day += timedelta(days=1)
    return out
