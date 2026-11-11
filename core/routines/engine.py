"""core/routines/engine.py — the routine store and the scheduler.

A tick every few seconds asks each enabled routine one question — is it due,
and can it be delivered now — and runs the ones that are.

TRIGGERS
    time     a rule from rules.py. Due when its next moment has passed.
    startup  once per run of the app, the first time the assistant can speak.
    idle     when the user has been quiet for N minutes, at most every M minutes.

DELIVERY, AND WHAT HAPPENS WHEN IT CANNOT HAPPEN NOW
    A routine that speaks needs a connected, awake assistant that is not
    already talking. When it is due and that is not true, it waits — but only
    for as long as it still makes sense: a reminder is still worth hearing two
    hours late, a spoken instruction forty-five minutes late, a background task
    six hours late. Past that it is skipped, said so in the log, and moved on
    to its next time. That is also how a routine that fell due while the
    computer was off is handled when it comes back.

    A background task needs nobody listening, so it never waits for that.

QUIET HOURS
    One window for the whole app, off by default. Routines marked `quiet` —
    the unprompted ones, like the idle check-in — are held back inside it. A
    routine the user set for an exact time is not: 06:30 inside a 23:00-07:00
    window was chosen on purpose.

The engine knows nothing about Qt, main.py or the Live API. It talks to a host:
    ready() -> bool          can something be said right now
    idle_seconds() -> float  how long the user has been quiet
    say(text)                speak a fixed reminder
    prompt(text)             give the assistant an instruction now
    builtin(name, options)   run a built-in routine
    start_task(text, agent, minutes, deliver)
    log(text)                a line for the activity log

A host that needs more than the text — which chat a reminder belongs to, say —
reads engine.firing(): the routine being carried out on this thread.

TWO OPTIONAL HOOKS, for a host whose clock is not this machine's
    tz_fn()       the timezone rules are read in. Default: this machine's. The
                  cloud body runs on a UTC server for a user who is not in UTC.
    grace_fn(r)   how late routine r may still run, in seconds; None = GRACE.
"""
from __future__ import annotations

import secrets
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

from core.routines import rules as R

KINDS = ("say", "prompt", "task", "builtin")
GRACE = {"say": 2 * 3600, "prompt": 45 * 60, "builtin": 45 * 60, "task": 6 * 3600}
STARTUP_WINDOW = 3 * 60     # a startup routine that could not speak by then is skipped
CALM_SECONDS = 10           # a timed routine does not cut into what the user is saying


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts).astimezone()


def _new_id() -> str:
    return "r" + secrets.token_hex(3)


class RoutineEngine:
    def __init__(self, path, host, clock=time.time, mono=time.monotonic, tick: float = 5.0,
                 log=print, autostart: bool = True, tz_fn=None, grace_fn=None):
        self.path = Path(path)
        self.host = host
        self._tz_fn, self._grace_fn = tz_fn, grace_fn
        self._firing = threading.local()
        self._clock, self._mono, self._tick, self._log = clock, mono, tick, log
        self._lock = threading.RLock()
        self._doc = self._load()
        self._session_at: float | None = None
        self._startup_awake = False
        self._startup_done: set[str] = set()
        self._idle_last: dict[str, float] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        if autostart:
            self.start()

    # ── the host's clock ─────────────────────────────────────────────────────
    def local_dt(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self._tz_fn()) if self._tz_fn else local_dt(ts)

    def tz(self):
        return self._tz_fn() if self._tz_fn else None

    def grace(self, r: dict) -> float:
        if self._grace_fn is not None:
            try:
                custom = self._grace_fn(r)
                if custom is not None:
                    return float(custom)
            except Exception as e:
                self._log(f"[Routines] grace_fn failed: {e}")
        return GRACE[r["kind"]]

    def firing(self) -> dict | None:
        """The routine being carried out on this thread, while the host runs it."""
        r = getattr(self._firing, "routine", None)
        return dict(r) if r else None

    # ── storage ──────────────────────────────────────────────────────────────
    def _load(self) -> dict:
        from core.tasks.store import read_json
        doc = read_json(self.path, None)
        if not isinstance(doc, dict):
            doc = {}
        doc.setdefault("version", 1)
        doc.setdefault("quiet_hours", "")
        doc.setdefault("routines", [])
        return doc

    def _save(self) -> None:
        from core.tasks.store import write_json
        write_json(self.path, self._doc)

    @property
    def seeded(self) -> bool:
        return bool(self._doc.get("seeded"))

    def mark_seeded(self) -> None:
        with self._lock:
            self._doc["seeded"] = True
            self._save()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="routines")
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """Look again now — the assistant just became able to speak, say."""
        self._wake.set()

    def session_started(self, awake: bool) -> None:
        """The assistant connected. Only the first connect of a run counts as
        starting: a reconnect is not a new morning. Coming up asleep (wake-word
        mode) skips the startup routines — a briefing then would be talking in
        its sleep."""
        if self._session_at is None:
            self._session_at = self._mono()
            self._startup_awake = bool(awake)
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._wake.wait(self._tick)
            self._wake.clear()

    # ── the public surface ───────────────────────────────────────────────────
    def list(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._doc["routines"]]

    def get(self, rid: str) -> dict | None:
        with self._lock:
            r = self._find(rid)
            return dict(r) if r else None

    def _find(self, rid: str) -> dict | None:
        return next((r for r in self._doc["routines"] if r["id"] == rid), None)

    def find_builtin(self, name: str) -> dict | None:
        with self._lock:
            r = next((r for r in self._doc["routines"] if r.get("builtin") == name), None)
            return dict(r) if r else None

    def add(self, name: str, kind: str, text: str = "", trigger: dict | None = None,
            options: dict | None = None, deliver: str = "voice", agent: str = "general",
            minutes: int = 10, quiet: bool | None = None, builtin: str = "", system: bool = False,
            enabled: bool = True, source: str = "", next_ts: float = 0.0) -> dict:
        """`next_ts` keeps a moment already worked out elsewhere — a routine carried
        over from an older store — instead of computing it from now."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if kind != "builtin" and not str(text or "").strip():
            raise ValueError("a routine needs the text it says or the instruction it carries out")
        trigger = self._check_trigger(trigger or {})
        r = {"id": _new_id(), "name": " ".join(str(name or text or builtin).split())[:80],
             "kind": kind, "builtin": builtin, "text": str(text or "").strip(),
             "trigger": trigger, "options": dict(options or {}),
             "deliver": deliver if deliver in ("voice", "panel", "silent") else "voice",
             "agent": agent or "general", "minutes": max(1, min(720, int(minutes or 10))),
             "quiet": (trigger["type"] == "idle") if quiet is None else bool(quiet),
             "system": bool(system), "enabled": bool(enabled), "source": source,
             "created": self._clock(), "next_ts": 0.0, "last_run": 0.0, "runs": 0, "last_result": ""}
        if trigger["type"] == "time" and next_ts:
            r["next_ts"] = float(next_ts)
        elif trigger["type"] == "time":
            nxt = R.next_fire(trigger["rule"], self.local_dt(self._clock()))
            if nxt is None:
                raise ValueError("that time has already passed")
            r["next_ts"] = nxt.timestamp()
        with self._lock:
            self._doc["routines"].append(r)
            self._save()
        self._wake.set()
        return dict(r)

    def update(self, rid: str, **fields) -> dict:
        with self._lock:
            r = self._find(rid)
            if r is None:
                raise ValueError("no routine with that id")
            if "trigger" in fields and fields["trigger"]:
                r["trigger"] = self._check_trigger(fields.pop("trigger"))
                r["next_ts"] = 0.0
            fields.pop("trigger", None)
            if "options" in fields:
                r["options"] = {**(r.get("options") or {}), **dict(fields.pop("options") or {})}
            for key in ("name", "text", "deliver", "agent", "minutes", "quiet", "enabled"):
                if key in fields and fields[key] is not None:
                    r[key] = fields[key]
            if r["trigger"]["type"] == "time" and (not r["next_ts"] or fields.get("enabled")):
                nxt = R.next_fire(r["trigger"]["rule"], self.local_dt(self._clock()))
                if nxt is None:
                    raise ValueError("that time has already passed")
                r["next_ts"] = nxt.timestamp()
            self._save()
            out = dict(r)
        self._wake.set()
        return out

    def remove(self, rid: str) -> bool:
        with self._lock:
            r = self._find(rid)
            if r is None:
                return False
            if r.get("system"):
                raise ValueError("a built-in routine can be turned off, not deleted")
            self._doc["routines"].remove(r)
            self._save()
            return True

    def run_now(self, rid: str) -> str:
        r = self.get(rid)
        if r is None:
            raise ValueError("no routine with that id")
        return self._fire(r, manual=True)

    def quiet_hours(self) -> str:
        return self._doc.get("quiet_hours") or ""

    def set_quiet_hours(self, between: str) -> str:
        text = str(between or "").replace(" ", "")
        if text:
            first, sep, last = text.partition("-")
            if not sep:
                raise ValueError("quiet hours are 'HH:MM-HH:MM'")
            text = f"{R.hhmm(first)}-{R.hhmm(last)}"
        with self._lock:
            self._doc["quiet_hours"] = text
            self._save()
        return text

    def in_quiet(self, dt: datetime) -> bool:
        q = self.quiet_hours()
        if not q:
            return False
        first, _, last = q.partition("-")
        start, end = R.parse_hhmm(first), R.parse_hhmm(last)
        now = (dt.hour, dt.minute)
        return start <= now < end if start < end else (now >= start or now < end)

    def describe(self, r: dict) -> str:
        t = r["trigger"]
        if t["type"] == "startup":
            return "when the assistant starts"
        if t["type"] == "idle":
            return f"after {t['minutes']} min of quiet, at most every {t['cooldown']} min"
        return R.describe_rule(t["rule"], self.tz())

    # ── the scheduler ────────────────────────────────────────────────────────
    @staticmethod
    def _check_trigger(trigger: dict) -> dict:
        kind = trigger.get("type")
        if kind == "startup":
            return {"type": "startup"}
        if kind == "idle":
            return {"type": "idle", "minutes": max(1, int(trigger.get("minutes") or 15)),
                    "cooldown": max(1, int(trigger.get("cooldown") or 20))}
        if kind == "time" and isinstance(trigger.get("rule"), dict):
            rule = dict(trigger["rule"])
            R.next_fire(rule, local_dt(time.time()))      # raises on a malformed rule
            return {"type": "time", "rule": rule}
        raise ValueError("trigger must be time (with a rule), startup or idle")

    def _host(self, method: str, *args, default=None):
        fn = getattr(self.host, method, None)
        if not callable(fn):
            return default
        try:
            return fn(*args)
        except Exception as e:
            self._log(f"[Routines] host.{method} failed: {e}")
            return default

    def tick(self) -> None:
        now = self._clock()
        dt = self.local_dt(now)
        ready = bool(self._host("ready", default=False))
        idle = float(self._host("idle_seconds", default=0.0) or 0.0)
        for r in self.list():
            if not r["enabled"]:
                continue
            t = r["trigger"]["type"]
            if t == "startup":
                if r["id"] in self._startup_done or self._session_at is None:
                    continue
                if not self._startup_awake:
                    self._startup_done.add(r["id"])
                elif ready:
                    self._startup_done.add(r["id"])
                    self._fire(r)
                elif self._mono() - self._session_at > STARTUP_WINDOW:
                    self._startup_done.add(r["id"])
                    self._log(f"[Routines] '{r['name']}' skipped: the assistant was not ready after start")
            elif t == "idle":
                if not ready or (r["quiet"] and self.in_quiet(dt)):
                    continue
                trig = r["trigger"]
                if idle < trig["minutes"] * 60:
                    continue
                last = self._idle_last.get(r["id"])
                if last is not None and self._mono() - last < trig["cooldown"] * 60:
                    continue
                self._idle_last[r["id"]] = self._mono()
                self._fire(r)
            elif t == "time":
                self._tick_time(r, now, dt, ready and idle >= CALM_SECONDS)

    def _tick_time(self, r: dict, now: float, dt: datetime, ready: bool) -> None:
        nxt = float(r.get("next_ts") or 0)
        if not nxt:
            self._advance(r["id"], dt)
            return
        if now < nxt:
            return
        late = now - nxt
        if r["quiet"] and self.in_quiet(dt):
            self._advance(r["id"], dt, note="held back by quiet hours")
            return
        if late > self.grace(r):
            self._advance(r["id"], dt, note=f"missed by {late / 60:.0f} min")
            return
        if r["kind"] != "task" and not ready:
            return                                   # wait for the assistant, within grace
        self._fire(r)
        self._advance(r["id"], dt)

    def _advance(self, rid: str, dt: datetime, note: str = "") -> None:
        with self._lock:
            r = self._find(rid)
            if r is None:
                return
            if note:
                self._log(f"[Routines] '{r['name']}' skipped — {note}")
                r["last_result"] = f"skipped: {note}"
            nxt = R.next_fire(r["trigger"]["rule"], dt)
            if nxt is None:
                if r.get("system"):
                    r["enabled"] = False
                else:
                    self._doc["routines"].remove(r)       # a one-off that has happened
            else:
                r["next_ts"] = nxt.timestamp()
            self._save()

    def _fire(self, r: dict, manual: bool = False) -> str:
        kind = r["kind"]
        self._firing.routine = r
        try:
            # Called directly rather than through _host: a failure here is the
            # routine's result, and run_now has to be able to say so.
            if kind == "say":
                self.host.say(r["text"])
            elif kind == "prompt":
                self.host.prompt(self.prompt_text(r))
            elif kind == "task":
                self.host.start_task(r["text"], r.get("agent") or "general",
                                     int(r.get("minutes") or 10), r.get("deliver") or "voice")
            else:
                self.host.builtin(r["builtin"], dict(r.get("options") or {}))
            result = "ran"
        except Exception as e:
            result = f"failed: {type(e).__name__}: {e}"
            self._log(f"[Routines] '{r['name']}' {result}")
        finally:
            self._firing.routine = None
        if not r.get("system") or manual:
            self._host("log", f"SYS: Routine — {r['name']}")
        with self._lock:
            live = self._find(r["id"])
            if live is not None:
                live.update(last_run=self._clock(), runs=int(live.get("runs") or 0) + 1, last_result=result)
                self._save()
        return result

    def prompt_text(self, r: dict) -> str:
        dt = self.local_dt(self._clock())
        return (f"[ROUTINE '{r['name']}' — set up by the user earlier; nobody asked just now]\n"
                f"It is {dt.strftime('%A %d %B %Y, %H:%M')} for the user.\n"
                f"{r['text']}\n"
                "Do it now. Speak to the user in their own language, and use your tools if the "
                "instruction needs them. Keep it brief unless it asks for detail.")
