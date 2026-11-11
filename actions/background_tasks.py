"""background_tasks — hand long work to an agent while the conversation goes on.

The tool is thin on purpose: everything that makes a task survive a restart,
respect a budget, wait out a quota or stay away from the user's computer lives
in core/tasks. This file turns what the model asked for into a call on the
engine and turns the answer back into facts (core/outcome.py) for the model to
say in the user's language.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta


def _when(at: str) -> float:
    """'YYYY-MM-DD HH:MM', or 'HH:MM' meaning the next time the clock reads it."""
    text = str(at or "").strip()
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            pass
    try:
        hm = datetime.strptime(text, "%H:%M")
    except ValueError:
        raise ValueError("'at' must be 'YYYY-MM-DD HH:MM' or 'HH:MM'") from None
    when = now.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.timestamp()


def _minutes(task: dict) -> str:
    return f"{task['used']['seconds'] / 60:.1f}/{task['budget']['minutes']} min"


def _row(t: dict) -> str:
    return f"{t['id']} [{t['status']}] {t['agent']} · {t['title']} · {_minutes(t)}"


def background_tasks(parameters: dict, player=None) -> str:
    from core import outcome
    from core.tasks import ACTIVE
    from core.tasks import desktop

    eng = desktop.ensure(player)
    a = dict(parameters or {})
    action = str(a.get("action") or "list").strip().lower()

    if action == "start":
        task = str(a.get("task") or "").strip()
        if not task:
            return outcome.failed("background_tasks", "start needs the whole task, in the user's words")
        not_before = 0.0
        if a.get("at"):
            try:
                not_before = _when(a["at"])
            except ValueError as e:
                return outcome.failed("background_tasks", str(e))
        files = [a["file_path"]] if a.get("file_path") else None
        try:
            t = eng.start(task, agent=a.get("agent") or "general", minutes=a.get("minutes"),
                          deliver=a.get("deliver") or "voice", not_before=not_before, files=files)
        except ValueError as e:
            return outcome.failed("background_tasks", str(e))
        starts = time.strftime("%Y-%m-%d %H:%M", time.localtime(not_before)) if not_before else "now"
        return outcome.started("background_tasks", task_id=t["id"], agent=t["agent"],
                               minutes=t["budget"]["minutes"], starts=starts,
                               note="the result is announced when it is done; the conversation "
                                    "carries on meanwhile")

    if action == "agents":
        return "\n".join(f"{p.name}: {p.description} (default {p.minutes} min)"
                         for p in eng.agents(reload=True).values())

    if action == "list":
        rows = [_row(t) for t in eng.list(10)]
        return "\n".join(rows) if rows else "No background tasks yet."

    tid = str(a.get("task_id") or "").strip()
    tasks = eng.list(50)
    if tid:
        task = eng.get(tid)
    else:
        # No id: the task this most plausibly means — the latest still running,
        # or for a result, the latest at all.
        pool = tasks if action == "result" else [t for t in tasks if t["status"] in ACTIVE]
        task = pool[0] if pool else None
    if task is None:
        return outcome.failed("background_tasks", "no such task" if tid else "there is no task to act on")

    if action == "status":
        recent = [e["text"] for e in task.get("events") or [] if e["kind"] in ("progress", "tool")][-4:]
        return outcome.done("background_tasks", task_id=task["id"], status=task["status"],
                            agent=task["agent"], title=task["title"], time=_minutes(task),
                            rounds=f"{task['used']['rounds']}/{task['budget']['rounds']}",
                            recent=" | ".join(recent) or None, error=task.get("error") or None)
    if action == "result":
        if task["status"] != "done":
            return outcome.done("background_tasks", task_id=task["id"], status=task["status"],
                                note="not finished yet")
        return (outcome.done("background_tasks", task_id=task["id"], report=task.get("report") or None)
                + "\n" + outcome.untrusted("task-result", (task.get("result") or "")[:12_000]))
    if action == "finish":
        ok = eng.cancel(task["id"], finish=True)
        return (outcome.started("background_tasks", task_id=task["id"],
                                note="stopping and writing up what was found")
                if ok else outcome.failed("background_tasks", "that task is not running"))
    if action == "cancel":
        ok = eng.cancel(task["id"])
        return (outcome.done("background_tasks", task_id=task["id"], cancelled=True)
                if ok else outcome.failed("background_tasks", "that task is not running"))
    if action == "steer":
        ok = eng.steer(task["id"], a.get("note") or "")
        return (outcome.done("background_tasks", task_id=task["id"], note_added=True)
                if ok else outcome.failed("background_tasks", "needs a running task and a note"))
    return outcome.failed("background_tasks", f"unknown action '{action}'")


TOOL = {
    "name": "background_tasks",
    "description": (
        "Long work that runs in the background on this computer while the conversation carries on: "
        "real research, reading and comparing many sources, writing a report, working through a "
        "list. For a quick answer the user is waiting for, use web_search instead. "
        "start = hand the task to an agent — agent 'researcher' for research (it plans, reads "
        "sources in full, checks every quote against the page and writes a cited report), "
        "'general' for other work; task = the whole request, self-contained, in the user's words; "
        "minutes = the time budget when the user gives one ('research this for 10 minutes' -> 10); "
        "without one, 10 for research, more (20-40) when the user asks for something thorough or "
        "in depth; at = start later, "
        "'YYYY-MM-DD HH:MM' or 'HH:MM' local time ('tonight at 2' -> '02:00'); file_path = a file to "
        "work from. After start, say in one short sentence that it is under way — the result is "
        "announced by itself when done. "
        "list = the tasks and their state; status = how one is going; result = a finished task's "
        "result; finish = stop now and write up what was found; cancel = stop without a result; "
        "steer = add an instruction to a running task (note); agents = the available agents. "
        "task_id may be left empty to mean the latest task."),
    "brief": "Hands long work (research, reports) to a background agent: start, list, status, "
             "result, finish, cancel, steer.",
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "enum": ["start", "list", "status", "result", "finish", "cancel", "steer", "agents"]},
            "task": {"type": "STRING", "description": "for start: the whole task, self-contained"},
            "agent": {"type": "STRING", "description": "for start: researcher | general"},
            "minutes": {"type": "INTEGER", "description": "for start: time budget in minutes"},
            "at": {"type": "STRING", "description": "for start: when to begin, local time"},
            "deliver": {"type": "STRING", "enum": ["voice", "panel", "silent"],
                        "description": "for start: how to announce the result (default voice)"},
            "file_path": {"type": "STRING", "description": "for start: a file to work from"},
            "task_id": {"type": "STRING"},
            "note": {"type": "STRING", "description": "for steer: the new instruction"},
        },
        "required": ["action"],
    },
    "handler": background_tasks,
}
