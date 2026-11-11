"""core/tasks/desktop.py — the task engine as the desktop app runs it.

Where tasks and reports live on this computer, and how a finished task reaches
the person: a line in the activity log, the result in the content panel, and a
sentence spoken through the same channel plugins use (player.request_say), so
the assistant says it in the user's own language rather than reading English
out of this file.

main.py calls attach(ui) once the interface exists, which is also what resumes
anything a restart interrupted. The background_tasks tool calls ensure(), so a
test harness or a plugin without main.py still gets an engine.
"""
from __future__ import annotations

import threading
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent.parent
_lock = threading.Lock()
_engine = None
_player = None


def tasks_dir() -> Path:
    return _BASE / "memory" / "tasks"


def reports_dir() -> Path:
    return _BASE / "reports"


def agents_dir() -> Path:
    return _BASE / "agents"


def _max_parallel() -> int:
    try:
        from memory.config_manager import load_api_keys
        return max(1, min(4, int(load_api_keys().get("tasks_max_parallel") or 2)))
    except Exception:
        return 2


def ensure(player=None):
    global _engine, _player
    with _lock:
        if player is not None:
            _player = player
        if _engine is None:
            from core.tasks.engine import TaskEngine
            _engine = TaskEngine(tasks_dir(), reports_dir(), [agents_dir()],
                                 notify=_notify, max_parallel=_max_parallel())
        return _engine


def attach(player):
    return ensure(player)


def engine():
    return _engine


def _call(method: str, *args) -> None:
    fn = getattr(_player, method, None)
    if callable(fn):
        try:
            fn(*args)
        except Exception as e:
            print(f"[Tasks] {method} failed: {e}")


def _notify(event: str, task: dict | None, text: str) -> None:
    if not task:
        return
    title = task.get("title") or ""
    deliver = task.get("deliver") or "voice"
    if event == "started":
        _call("write_log", f"SYS: Background task started — {title}")
    elif event == "done":
        _call("write_log", f"SYS: Background task finished — {title}")
        if deliver == "silent":
            return
        report = task.get("report") or ""
        # The panel title is the app's own English label; the task's title is the
        # user's words and goes in the body, where it is not upper-cased.
        body = f"{title}\n\n{(task.get('result') or '').strip()}"
        if report:
            body += f"\n\n— Full report: {report}"
        _call("show_content", "BACKGROUND TASK", body[:4000])
        if deliver == "voice":
            from core import outcome
            _call("request_say",
                  "[BACKGROUND TASK FINISHED] A task the user gave you earlier is done: "
                  f"'{(task.get('task') or '')[:200]}'. Tell the user in one or two sentences "
                  "what came of it — the headline, not the whole result — and that the full "
                  "result is on the screen and saved as a report. Speak in the user's language. "
                  "Do not call any tool for this.\n"
                  + outcome.untrusted("task-result", (task.get("result") or "")[:3000]))
    elif event == "failed":
        _call("write_log", f"ERR: Background task failed — {title}: {(task.get('error') or '')[:160]}")
        if deliver != "silent":
            _call("request_say",
                  f"[BACKGROUND TASK FAILED] The background task '{title[:120]}' could not finish: "
                  f"{(task.get('error') or 'unknown reason')[:200]}. Tell the user in one short "
                  f"sentence, in their language. Do not call any tool for this.")
    elif event == "waiting":
        _call("write_log", f"SYS: Background task waiting for a model (quota) — {title}")
    elif event == "cancelled":
        _call("write_log", f"SYS: Background task cancelled — {title}")
