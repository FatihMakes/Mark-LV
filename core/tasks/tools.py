"""core/tasks/tools.py — what a background agent may use.

An agent profile names the tools it wants; this decides what it gets. The
decision is made here, in code, rather than in the profile or a prompt,
because a background agent spends its time reading pages written by strangers
and nothing it reads may widen what it can do.

THE RULES
    Nothing that acts on the computer or on the world. No opening applications,
    typing, sending messages, moving files outside the task's own folder. A
    profile that names such a tool does not get it — the tool simply is not in
    this file. (Mark LVIII's approval policy is where acting tools will come
    back, behind a button a person presses.)

    The web and private memory are never in the same agent. An agent that can
    fetch a URL could be told by a page to put what it knows about the user
    into that URL. So an agent with web access is not given recall_memory, and
    its system prompt carries no stored facts.

    Files stay in the task's folder. Anything the user attached is copied into
    it at the start; reads and writes resolve inside it and refuse to leave.
"""
from __future__ import annotations

import time
from pathlib import Path

from core import outcome

_WEB = {"web_search", "search_web", "fetch_page"}
KNOWN = ("search_web", "web_search", "fetch_page", "notes", "write_file", "file_processor",
         "recall_memory")
_MAX_WRITE = 2_000_000

_DECLS = {
    "search_web": {
        "name": "search_web",
        "description": ("Search the web and get a list of results — title, address and snippet — "
                        "to choose which pages to read with fetch_page. Try several phrasings, and "
                        "other languages where the topic is better covered. news=true for recent "
                        "news articles."),
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING"},
            "max_results": {"type": "INTEGER", "description": "3-15, default 8"},
            "news": {"type": "BOOLEAN"}},
            "required": ["query"]},
    },
    "fetch_page": {
        "name": "fetch_page",
        "description": ("Read a web page or an online PDF in full — the page's own text, not a search "
                        "snippet. Use it on the most promising results before relying on them. "
                        "Public http(s) addresses only."),
        "parameters": {"type": "OBJECT", "properties": {
            "url": {"type": "STRING", "description": "the full address, starting with http"},
            "max_chars": {"type": "INTEGER", "description": "how much text to return, 2000-40000 "
                                                            "(default 15000)"}},
            "required": ["url"]},
    },
    "notes": {
        "name": "notes",
        "description": ("Your working notes for this task, kept on disk. add = record one finding, "
                        "with the URL it came from; read = get them all back. Long tasks trim old tool "
                        "results out of your memory to save space — your notes are never trimmed, so "
                        "note everything you will need for the final result."),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "enum": ["add", "read"]},
            "text": {"type": "STRING", "description": "for add: the finding and its source"}},
            "required": ["action"]},
    },
    "write_file": {
        "name": "write_file",
        "description": ("Save a file for the user in this task's folder — a table as CSV, a draft, "
                        "collected data. Returns where it was saved."),
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING", "description": "a file name, e.g. comparison.csv"},
            "content": {"type": "STRING"}},
            "required": ["path", "content"]},
    },
    "recall_memory": {
        "name": "recall_memory",
        "description": "Look up a stored fact about the user. Empty query lists all.",
        "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}},
    },
}


def _inside(root: Path, rel: str) -> Path:
    root = root.resolve()
    given = Path(str(rel or ""))
    target = (given if given.is_absolute() else root / given).resolve()
    if target != root and root not in target.parents:
        raise PermissionError("that path is outside this task's folder")
    return target


class TaskTools:
    def __init__(self, workdir: Path, wanted, log=print):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._log = log
        names = [n for n in dict.fromkeys(wanted) if n in KNOWN]
        dropped = [n for n in wanted if n not in KNOWN]
        if dropped:
            log(f"[Tasks] not available to background agents: {', '.join(dropped)}")
        if _WEB & set(names) and "recall_memory" in names:
            names.remove("recall_memory")
        self.names = names
        self._borrowed: dict = {}
        # Called with (requested_url, document) for every page read in full —
        # the research runner keeps the text to check quotes against.
        self.on_page = None

    @property
    def has_web(self) -> bool:
        return bool(_WEB & set(self.names))

    def _action(self, name: str) -> dict:
        if name not in self._borrowed:
            import importlib
            tool = getattr(importlib.import_module(f"actions.{name}"), "TOOL", None)
            if not (isinstance(tool, dict) and callable(tool.get("handler"))):
                raise RuntimeError(f"actions/{name}.py has no usable TOOL")
            self._borrowed[name] = tool
        return self._borrowed[name]

    def declarations(self) -> list[dict]:
        out = []
        for name in self.names:
            if name in _DECLS:
                out.append(dict(_DECLS[name]))
                continue
            try:
                tool = self._action(name)
            except Exception as e:
                self._log(f"[Tasks] {name} unavailable: {e}")
                continue
            desc = tool["description"]
            if name == "file_processor":
                desc = ("Read, summarise or analyse a file in this task's folder. Files the user "
                        "attached are in inputs/. file_path is relative to the task's folder.")
            elif name == "web_search":
                desc = ("A quick synthesised answer from a search engine, with sources. Good for "
                        "orienting; for evidence, read the pages themselves with fetch_page. "
                        "mode: search | news | research | price | compare.")
            out.append({"name": name, "description": desc, "parameters": tool["parameters"]})
        return out

    def execute(self, name: str, args: dict) -> str:
        args = dict(args or {})
        if name not in self.names:
            return outcome.failed(name, "this tool is not available to this agent")
        try:
            if name == "fetch_page":
                return self._fetch(args)
            if name == "search_web":
                return self._search(args)
            if name == "notes":
                return self._notes(args)
            if name == "write_file":
                return self._write(args)
            if name == "recall_memory":
                from memory.memory_manager import search_memory
                return search_memory(str(args.get("query") or ""), limit=8)
            from core.action_loader import _call_handler
            if name == "file_processor":
                if str(args.get("action") or "").lower() in ("run", "test"):
                    return outcome.failed(name, "running code is not available to background tasks")
                if not args.get("file_path"):
                    return outcome.failed(name, "file_path is required")
                args["file_path"] = str(_inside(self.workdir, args["file_path"]))
            result = str(_call_handler(self._action(name)["handler"], args, {}) or "Done.")
            if "<<<UNTRUSTED" not in result and name in ("file_processor", "web_search"):
                result = outcome.untrusted("file" if name == "file_processor" else "web", result)
            return result
        except Exception as e:
            return outcome.failed(name, f"{type(e).__name__}: {e}")

    # ── native tools ─────────────────────────────────────────────────────────
    def _fetch(self, args: dict) -> str:
        from core.tasks import web

        url = str(args.get("url") or "").strip()
        try:
            doc = web.fetch_document(url)
        except (web.Refused, web.FetchError) as e:
            return outcome.failed("fetch_page", str(e), url=url)
        if self.on_page is not None:
            try:
                self.on_page(url, doc)
            except Exception as e:
                self._log(f"[Tasks] page hook failed: {e}")
        return web.format_page(doc, args.get("max_chars") or 15_000)

    def _search(self, args: dict) -> str:
        from actions.web_search import _ddg_news, _ddg_search

        query = " ".join(str(args.get("query") or "").split())
        if not query:
            return outcome.failed("search_web", "search needs a query")
        try:
            n = max(3, min(15, int(args.get("max_results") or 8)))
        except (TypeError, ValueError):
            n = 8
        rows = _ddg_news(query, max_results=n) if args.get("news") else _ddg_search(query, max_results=n)
        rows = [r for r in rows or [] if r.get("url")]
        if not rows:
            return outcome.failed("search_web", "no results — try other words or another language",
                                  query=query)
        lines = [f"Results for: {query}"]
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r.get('title', '')}\n   {r['url']}\n   {(r.get('snippet') or '')[:300]}")
        return outcome.untrusted("web", "\n".join(lines))

    def _notes(self, args: dict) -> str:
        path = self.workdir / "notes.md"
        if str(args.get("action") or "read").lower() == "add":
            text = " ".join(str(args.get("text") or "").split())
            if not text:
                return outcome.failed("notes", "add needs the text of the note")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"- [{time.strftime('%H:%M')}] {text}\n")
            count = sum(1 for _ in path.open(encoding="utf-8"))
            return outcome.done("notes", added=True, total_notes=count)
        if not path.exists():
            return "No notes yet."
        return path.read_text(encoding="utf-8")[-60_000:]

    def _write(self, args: dict) -> str:
        content = str(args.get("content") or "")
        if len(content.encode("utf-8")) > _MAX_WRITE:
            return outcome.failed("write_file", "that is larger than 2 MB")
        target = _inside(self.workdir / "files", str(args.get("path") or "").strip() or "output.txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return outcome.done("write_file", saved=str(target), bytes=target.stat().st_size)
