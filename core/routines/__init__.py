"""core/routines — things the assistant does on its own, when a moment arrives.

Before Mark LVII the same idea was written four times, each by hand: a morning
briefing fired at connect, a proactive check-in on a fifteen-minute timer, a
news watch on a thirty-minute loop, and reminders that were only ever an
operating-system notification. A routine is the shape they share:

    WHEN   a time rule (once, weekly, monthly, every N minutes), the assistant
           starting, or the user having been quiet for a while
    WHAT   say a fixed reminder, give the assistant an instruction to carry out
           with its tools, start a background task, or run one of the built-in
           routines those four features became

    rules.py    when a time rule next fires — no clock of its own, easy to test
    engine.py   the store and the scheduler: triggers, grace, quiet hours
    desktop.py  where routines live on this computer, the defaults, migration
"""
from core.routines.engine import RoutineEngine  # noqa: F401
