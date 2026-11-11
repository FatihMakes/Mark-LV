"""core/tasks/agents.py — agent profiles, written as Markdown rather than code.

An agent is a role, a set of tools and a budget. None of that is logic, so none
of it belongs in a .py file: a profile is a Markdown file in agents/ with a
short header and the instructions underneath, and anyone who can write a
paragraph can add one.

    ---
    name: researcher
    title: Researcher
    description: One line saying what this agent is for.
    mode: research
    tools: search_web, fetch_page
    minutes: 10
    rounds: 60
    ---
    The instructions, in plain words.

`mode` is optional. Left out, the agent works in one open loop until it is done.
`research` runs it through the research method instead (core/tasks/research.py):
a plan, an investigation per sub-question with quotes checked against the pages
read, a review for gaps, and a report written only from what was recorded.

The header is read by hand — `key: value`, one per line — so profiles need no
YAML package. A file that is not valid is skipped with a reason in the log and
never stops the others loading.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
MODES = ("", "agent", "research")


@dataclass
class AgentProfile:
    name: str
    title: str = ""
    description: str = ""
    tools: list[str] = field(default_factory=list)
    minutes: int = 10
    rounds: int = 40
    instructions: str = ""
    file: str = ""
    mode: str = ""


# Used only when agents/ is missing or holds nothing valid, so a broken install
# still has one agent that can do something.
FALLBACK = AgentProfile(
    name="general", title="General",
    description="Any long piece of work.",
    tools=["search_web", "fetch_page", "notes", "write_file"],
    minutes=10, rounds=40,
    instructions="Carry the task through to the end with your tools, then write a clear result.")


def parse(text: str, file: str = "") -> AgentProfile:
    """Raises ValueError with a readable reason."""
    text = text.lstrip("﻿")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("no header — a profile starts with a --- block")
    head, body = m.group(1), m.group(2).strip()
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"header line is not 'key: value': {line.strip()!r}")
        meta[key.strip().lower()] = value.strip()

    name = meta.get("name", "").lower()
    if not _NAME_RE.match(name):
        raise ValueError("name must be 2-32 lowercase letters, digits, - or _")
    tools = [t.strip() for t in re.split(r"[,\s]+", meta.get("tools", "")) if t.strip()]
    if not tools:
        raise ValueError("tools is empty — an agent with no tools can only guess")
    if not body:
        raise ValueError("no instructions under the header")
    mode = meta.get("mode", "").lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(m for m in MODES if m)}")

    def _int(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(meta.get(key, default))))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number") from None

    return AgentProfile(name=name, title=meta.get("title") or name.title(),
                        description=meta.get("description", ""), tools=tools,
                        minutes=_int("minutes", 10, 1, 720), rounds=_int("rounds", 40, 1, 1000),
                        instructions=body, file=file, mode="" if mode == "agent" else mode)


def load(dirs, log=print) -> dict[str, AgentProfile]:
    """Every valid profile in `dirs`; a later directory's profile replaces an
    earlier one of the same name, so a user folder can override a shipped one."""
    out: dict[str, AgentProfile] = {}
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            if path.name.startswith(("_", ".")) or path.name.lower() == "readme.md":
                continue
            try:
                prof = parse(path.read_text(encoding="utf-8"), path.name)
            except Exception as e:
                log(f"[Tasks] agent profile {path.name} skipped — {e}")
                continue
            out[prof.name] = prof
    if not out:
        out[FALLBACK.name] = FALLBACK
    return out
