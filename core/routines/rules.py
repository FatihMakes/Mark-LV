"""core/routines/rules.py — when a time rule next fires.

The same rules the cloud body has used since its reminders shipped
(core/cloud.py), moved here so the desktop and the server read one definition.

Every rule is data and every answer is "the first moment strictly after this
one", so a scheduler that was not running at 08:00 still knows at 08:20 that an
08:00 routine is due, and how late it is.

    {"type": "once",     "at_ts": 1800000000.0}
    {"type": "weekly",   "time": "08:30", "days": [1, 2, 3, 4, 5]}
    {"type": "monthly",  "day": 31, "time": "09:00"}      31 = the month's last day
    {"type": "interval", "minutes": 30, "start": "08:00", "end": "23:00", "days": [...]}

Days are ISO numbers, 1 = Monday — numbers, because a day name is a word in
exactly one language.
"""
from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]


def parse_hhmm(value) -> tuple[int, int]:
    text = str(value or "").strip().replace(".", ":")
    hour, _, minute = text.partition(":")
    try:
        hour, minute = int(hour), int(minute or 0)
    except ValueError:
        raise ValueError(f"'{value}' is not a time of day (HH:MM)") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"'{value}' is not a time of day (HH:MM)")
    return hour, minute


def hhmm(value) -> str:
    return "%02d:%02d" % parse_hhmm(value)


def parse_days(value) -> list[int]:
    if value in (None, "", []):
        return list(ALL_DAYS)
    items = value if isinstance(value, (list, tuple)) else re.split(r"[^\d]+", str(value))
    days = sorted({int(x) for x in items if str(x).strip().isdigit() and 1 <= int(x) <= 7})
    if not days:
        raise ValueError("days are numbers 1-7, where 1 is Monday")
    return days


def next_fire(rule: dict, after: datetime) -> datetime | None:
    """The first moment strictly after `after` that `rule` fires, or None."""
    kind = rule.get("type")
    if kind == "once":
        at = datetime.fromtimestamp(float(rule["at_ts"]), after.tzinfo)
        return at if at > after else None
    days = set(rule.get("days") or ALL_DAYS)
    if kind == "weekly":
        hour, minute = parse_hhmm(rule["time"])
        for i in range(8):
            cand = (after + timedelta(days=i)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand > after and cand.isoweekday() in days:
                return cand
        return None
    if kind == "monthly":
        hour, minute = parse_hhmm(rule["time"])
        want, year, month = int(rule["day"]), after.year, after.month
        for _ in range(14):
            day = min(want, calendar.monthrange(year, month)[1])
            cand = datetime(year, month, day, hour, minute, tzinfo=after.tzinfo)
            if cand > after:
                return cand
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return None
    if kind == "interval":
        step = max(5, int(rule["minutes"]))
        sh, sm = parse_hhmm(rule.get("start") or "00:00")
        eh, em = parse_hhmm(rule.get("end") or "23:59")
        for i in range(8):
            base = (after + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            if base.isoweekday() not in days:
                continue
            start, end = base.replace(hour=sh, minute=sm), base.replace(hour=eh, minute=em)
            cand = start
            if after >= start:
                done = int((after - start).total_seconds() // (step * 60)) + 1
                cand = start + timedelta(minutes=step * done)
            if start <= cand <= end and cand > after:
                return cand
        return None
    return None


def describe_rule(rule: dict, tz=None) -> str:
    """`tz` is the clock a one-off is shown in; None = this machine's."""
    kind = rule.get("type")
    days = list(rule.get("days") or ALL_DAYS)
    on = "" if days == ALL_DAYS else " on days " + ",".join(map(str, days)) + " (1=Mon)"
    if kind == "once":
        return "once at " + datetime.fromtimestamp(float(rule["at_ts"]), tz).strftime("%Y-%m-%d %H:%M")
    if kind == "weekly":
        return f"at {rule['time']}" + (on or " every day")
    if kind == "monthly":
        return f"on day {rule['day']} of every month at {rule['time']}"
    if kind == "interval":
        return f"every {rule['minutes']} min, {rule.get('start', '00:00')}-{rule.get('end', '23:59')}{on}"
    return str(kind)


def rule_from_args(a: dict, tz) -> dict:
    """A tool call's WHEN fields as a rule. Raises ValueError with a sentence the
    model can pass on."""
    days = parse_days(a.get("days"))
    time_of_day = a.get("time") or a.get("daily")
    if a.get("every_minutes"):
        minutes = int(a["every_minutes"])
        if minutes < 5:
            raise ValueError("every_minutes must be at least 5")
        start, end = "00:00", "23:59"
        if a.get("between"):
            first, _, last = str(a["between"]).replace(" ", "").partition("-")
            start, end = hhmm(first), hhmm(last)
        return {"type": "interval", "minutes": minutes, "start": start, "end": end, "days": days}
    if a.get("monthly_day"):
        day = int(a["monthly_day"])
        if not 1 <= day <= 31:
            raise ValueError("monthly_day is 1-31")
        if not time_of_day:
            raise ValueError("a monthly routine needs a time (HH:MM)")
        return {"type": "monthly", "day": day, "time": hhmm(time_of_day)}
    if time_of_day:
        return {"type": "weekly", "time": hhmm(time_of_day), "days": days}
    if a.get("at"):
        text = str(a["at"]).strip().replace("T", " ")
        try:
            when = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            raise ValueError("'at' must be 'YYYY-MM-DD HH:MM'") from None
        return {"type": "once", "at_ts": when.timestamp()}
    raise ValueError("say when: 'at' for once, 'time' (+ 'days') to repeat, 'monthly_day' + 'time', "
                     "'every_minutes' (+ 'between'), or on = startup | idle")
