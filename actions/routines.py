"""routines — things the assistant does on its own, managed by voice.

The engine is core/routines; this turns a tool call into a call on it, and its
answer into facts (core/outcome.py) for the model to say in the user's language.
"""
from __future__ import annotations

from datetime import datetime


def _trigger(a: dict) -> dict | None:
    from core.routines import rules as R

    on = str(a.get("on") or "").strip().lower()
    if on == "startup":
        return {"type": "startup"}
    if on == "idle":
        return {"type": "idle", "minutes": int(a.get("idle_minutes") or 15),
                "cooldown": int(a.get("cooldown_minutes") or 20)}
    if any(a.get(k) for k in ("at", "time", "monthly_day", "every_minutes")):
        return {"type": "time", "rule": R.rule_from_args(a, datetime.now().astimezone().tzinfo)}
    return None


def _row(eng, r: dict) -> str:
    nxt = ""
    if r["trigger"]["type"] == "time" and r.get("next_ts"):
        nxt = " · next " + datetime.fromtimestamp(r["next_ts"]).strftime("%Y-%m-%d %H:%M")
    what = r["builtin"] if r["kind"] == "builtin" else r["kind"]
    text = f" · \"{r['text'][:70]}\"" if r.get("text") else ""
    return (f"{r['id']} [{'on' if r['enabled'] else 'off'}] {r['name']} ({what}) · "
            f"{eng.describe(r)}{nxt}{text}")


def routines(parameters: dict, player=None) -> str:
    from core import outcome
    from core.routines import desktop

    eng = desktop.engine()
    if eng is None:
        return outcome.failed("routines", "routines are not running in this session")
    a = dict(parameters or {})
    action = str(a.get("action") or "list").strip().lower()
    try:
        if action == "list":
            rows = [_row(eng, r) for r in eng.list()]
            quiet = eng.quiet_hours()
            return "\n".join(rows + [f"quiet hours: {quiet or 'off'}"]) if rows else "No routines."

        if action == "add":
            kind = str(a.get("kind") or "").strip().lower()
            if kind not in ("say", "prompt", "task"):
                return outcome.ask("routines", "should it say a fixed reminder (say), carry out an "
                                               "instruction when the time comes (prompt), or run a "
                                               "background task (task)?")
            trigger = _trigger(a)
            if trigger is None:
                return outcome.failed("routines", "say when: at, time (+ days), monthly_day, "
                                                  "every_minutes, or on = startup | idle")
            r = eng.add(name=a.get("name") or a.get("text") or "", kind=kind, text=a.get("text") or "",
                        trigger=trigger, deliver=a.get("deliver") or "voice",
                        agent=a.get("agent") or "general", minutes=a.get("minutes") or 10,
                        quiet=a.get("quiet"))
            return outcome.done("routines", id=r["id"], name=r["name"], when=eng.describe(r),
                                next=datetime.fromtimestamp(r["next_ts"]).strftime("%Y-%m-%d %H:%M")
                                if r.get("next_ts") else None)

        if action == "quiet_hours":
            value = eng.set_quiet_hours(a.get("between") or "")
            return outcome.done("routines", quiet_hours=value or "off")

        rid = str(a.get("id") or "").strip()
        if not rid and a.get("builtin"):
            found = eng.find_builtin(str(a["builtin"]).strip().lower())
            rid = found["id"] if found else ""
        if not rid:
            return outcome.failed("routines", "which routine? give its id (list shows them) or builtin")

        if action == "update":
            fields = {k: a[k] for k in ("name", "text", "deliver", "agent", "minutes", "quiet", "enabled")
                      if a.get(k) is not None}
            trigger = _trigger(a)
            if trigger:
                fields["trigger"] = trigger
            options = {}
            for key in ("news_topic", "extra", "news"):
                if a.get(key) is not None:
                    options[key] = a[key]
            if a.get("focuses"):
                options["focuses"] = [str(f) for f in a["focuses"] if str(f).strip()]
            if options:
                fields["options"] = options
            if not fields:
                return outcome.failed("routines", "update needs something to change")
            r = eng.update(rid, **fields)
            if r.get("builtin") == "briefing" and "enabled" in fields:
                try:
                    desktop.mirror_brief_switch(r["enabled"])
                except Exception as e:
                    print(f"[Routines] MORNING BRIEF setting not updated: {e}")
            return outcome.done("routines", id=r["id"], name=r["name"], enabled=r["enabled"],
                                when=eng.describe(r), changed=",".join(fields))

        if action == "remove":
            return (outcome.done("routines", removed=rid) if eng.remove(rid)
                    else outcome.failed("routines", "no routine with that id"))

        if action == "run_now":
            return outcome.done("routines", id=rid, result=eng.run_now(rid))

        return outcome.failed("routines", f"unknown action '{action}'")
    except ValueError as e:
        return outcome.failed("routines", str(e))


TOOL = {
    "name": "routines",
    "description": (
        "Routines — things the assistant does on its own when a moment comes. "
        "add: kind 'say' = speak a fixed reminder (text = the exact words, in the user's language); "
        "kind 'prompt' = carry out an instruction at that moment with your tools (text = the "
        "instruction, e.g. 'tell me the weather and my agenda'); kind 'task' = start a background "
        "task (text = the task, agent researcher|general, minutes). WHEN, one of: at 'YYYY-MM-DD "
        "HH:MM' (once) | time 'HH:MM' + optional days (numbers 1=Mon…7=Sun; weekdays = [1,2,3,4,5]) | "
        "monthly_day 1-31 + time | every_minutes + optional between 'HH:MM-HH:MM' | on 'startup' | "
        "on 'idle' + idle_minutes. "
        "The built-in routines are builtin 'briefing' (the opening briefing: news_topic, extra, "
        "news true/false), 'checkin' (unprompted check-ins: focuses = what they may be about) and "
        "'news_watch' (the watched topics' headlines). list = all routines with ids; update = change "
        "one by id or builtin (enabled, when, text, and the options above); remove = delete by id; "
        "run_now = do it now; quiet_hours = 'HH:MM-HH:MM' when unprompted routines stay silent "
        "(empty turns it off). For a one-off reminder at a date and time, the reminder tool also "
        "shows a system notification."),
    "brief": "Scheduled and automatic routines: add, list, update, remove, run_now, quiet_hours.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "enum": ["add", "list", "update", "remove", "run_now", "quiet_hours"]},
            "id": {"type": "STRING"},
            "builtin": {"type": "STRING", "enum": ["briefing", "checkin", "news_watch"]},
            "name": {"type": "STRING"},
            "kind": {"type": "STRING", "enum": ["say", "prompt", "task"]},
            "text": {"type": "STRING"},
            "at": {"type": "STRING"},
            "time": {"type": "STRING"},
            "days": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "monthly_day": {"type": "INTEGER"},
            "every_minutes": {"type": "INTEGER"},
            "between": {"type": "STRING"},
            "on": {"type": "STRING", "enum": ["startup", "idle"]},
            "idle_minutes": {"type": "INTEGER"},
            "cooldown_minutes": {"type": "INTEGER"},
            "enabled": {"type": "BOOLEAN"},
            "quiet": {"type": "BOOLEAN"},
            "deliver": {"type": "STRING", "enum": ["voice", "panel", "silent"]},
            "agent": {"type": "STRING"},
            "minutes": {"type": "INTEGER"},
            "news_topic": {"type": "STRING"},
            "news": {"type": "BOOLEAN"},
            "extra": {"type": "STRING"},
            "focuses": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["action"],
    },
    "handler": routines,
}
