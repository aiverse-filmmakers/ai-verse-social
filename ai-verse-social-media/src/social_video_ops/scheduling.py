from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .util import UserError, timestamp


def next_times(config: dict, reservations: list[str], *, start: datetime | None = None,
               analytics: list[dict] | None = None) -> dict:
    start = start or datetime.now(timezone.utc)
    if start.tzinfo is None:
        raise UserError("Schedule planning requires an aware date/time.")
    zone = ZoneInfo(config["timezone"])
    policy = config["schedule"]
    booked = [timestamp(value) for value in reservations]
    basis = "customer schedule; insufficient account analytics"
    ranked = {}
    samples = 0
    for slot in analytics or []:
        try:
            day, hour = int(slot.get("dayOfWeek", slot.get("day_of_week"))), int(slot["hour"])
            count = int(slot.get("postCount", slot.get("post_count", 0)))
            if 0 <= day <= 6 and 0 <= hour <= 23 and count > 0:
                # Provider days are Sunday=0; engine compares in UTC.
                ranked[(day, hour)] = float(slot.get("averageEngagement", slot.get("avg_engagement", 0)))
                samples += count
        except (KeyError, ValueError, TypeError):
            continue
    if samples >= policy["minimum_best_time_samples"]:
        basis = f"historical UTC engagement slots ({samples} observations); not an optimality guarantee"
    else:
        ranked = {}
    candidates = []
    local_start = start.astimezone(zone)
    for offset in range(15):
        date = local_start.date() + timedelta(days=offset)
        for window in policy["windows"]:
            hour, minute = map(int, window.split(":"))
            wall = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone, fold=0)
            utc = wall.astimezone(timezone.utc)
            # Skip nonexistent DST wall times; fold=0 chooses first repeated hour once.
            if utc.astimezone(zone).replace(tzinfo=None) != wall.replace(tzinfo=None):
                continue
            if utc <= start:
                continue
            if sum(v.astimezone(zone).date() == date for v in booked) >= policy["max_posts_per_day"]:
                continue
            if any(abs((utc-v).total_seconds()) < policy["min_gap_minutes"]*60 for v in booked):
                continue
            day = (utc.weekday()+1)%7
            score = ranked.get((day, utc.hour), 0)
            candidates.append((utc, score))
        # Limit recommendations to the next seven calendar days with capacity.
        if candidates and offset >= 6:
            break
    if not candidates:
        raise UserError("No slot fits the next 15 days. Review windows, limits, or existing reservations.")
    # Rank analytics only among approved windows; avoid shifting outside customer scope.
    utc, score = sorted(candidates, key=lambda value: (-value[1], value[0]))[0]
    return {"scheduled_at": utc.isoformat(), "local_time": utc.astimezone(zone).isoformat(),
            "timezone": config["timezone"], "basis": basis, "score": score,
            "confidence": "historical estimate" if ranked else "schedule fallback"}
