"""core/tasks — long work that runs in the background while the conversation goes on.

One engine for both bodies of the assistant: the desktop runs it next to the
voice session, and the cloud server (core/cloud.py) runs the same code around
the clock. A task is handed to an agent profile (agents/*.md), works through a
tool-calling model one round at a time, is written to disk after every round,
and ends in a report.

    store.py    tasks on disk, one file each, written atomically
    brain.py    one round of a tool-calling model, walking the Gemini ladder
    agents.py   agent profiles — plain Markdown files, not code
    tools.py    what a background agent may use, enforced here rather than asked for
    web.py      reading a web page or PDF in full, public addresses only
    engine.py   the scheduler, the worker loop, budgets, cancel, steer, resume
    desktop.py  the desktop's glue: where files live, how results are announced
"""
from core.tasks.engine import ACTIVE, TaskEngine  # noqa: F401
