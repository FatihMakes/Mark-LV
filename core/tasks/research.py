"""core/tasks/research.py — deep research, as a method rather than a hope.

An agent told "research this carefully" in one open loop does what the first
live test showed: three searches, a skim, a confident report, half a minute of
a ten-minute budget, and no record of where any sentence came from. So research
runs through stages the engine enforces, and the model does the thinking inside
each one:

    PLAN         the question becomes sub-questions and searches, sized to the budget
    INVESTIGATE  one sub-question at a time: search, read pages in full, and record
                 every finding with its source and an exact quote
    REVIEW       what is thin, single-sourced or contradictory becomes new
                 sub-questions (at most two review cycles)
    WRITE        the report is written from the recorded findings — nothing else
    VERIFY       statements that go beyond the findings are corrected

WHAT IS CHECKED IN CODE, NOT ASKED FOR
    Quotes. Every page read in full is kept. A finding's quote is looked up in
    the text of the page it names; one that is not there is marked unverified,
    and the report is told so. A model cannot invent a supporting sentence and
    have it pass. A finding recorded from a page that was never opened — a
    search snippet, a half-remembered address — has its page fetched right
    then, so the check is made rather than skipped.

    Citations. Sources are numbered by the runner. A citation to a number that
    does not exist is replaced, and the source list is appended by the runner —
    the model never writes it, so it cannot list a source it did not use.

    Depth. The time budget is shared across sub-questions and actually spent: a
    sub-question declared done after fewer than MIN_SOURCES pages read in full,
    with most of its time left, is sent back once to read more.

    Language. The report is written in the language of the task; searches may
    go wherever a topic is best documented.

Every stage saves its state on the task, so a restart resumes at the stage and
sub-question it stopped in. Finish early and it writes up what it has; steer it
and the note reaches the stage in progress, the review and the report.

MEASURED ON THE FIRST LIVE RUN (4-minute budget)
    3m 06s, 42 rounds, 4 sub-questions including one gap from the review, 14
    sources of which 9 read in full, 18 findings. It also showed what this
    version fixes: 8 of those findings came from pages never opened, and the
    verification pass then marked well-supported statements unverified — in
    English, inside a Turkish report.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from urllib.parse import urldefrag

from core import outcome
from core.tasks.brain import NoModel
from core.tasks.engine import QUOTA_WAIT, trim_turns

MAX_SUBQ = 8
MAX_GAPS = 3
MAX_CYCLES = 2
MIN_SOURCES = 2
WRITE_SHARE = 0.20            # of the time budget kept for review, writing and verifying
MIN_WRITE_SECONDS = 45

FINDING_DECL = {
    "name": "record_finding",
    "description": ("Record one finding for the report. claim = the fact in your own words; "
                    "source_url = the page it came from; quote = a short passage copied EXACTLY from "
                    "that page which supports it (it is checked against the page's text); stance = "
                    "supports | contradicts | context; date = when the source says it happened or was "
                    "published, if given. Record contradicting evidence too. Only recorded findings "
                    "reach the report."),
    "parameters": {"type": "OBJECT", "properties": {
        "claim": {"type": "STRING"},
        "source_url": {"type": "STRING"},
        "source_title": {"type": "STRING"},
        "quote": {"type": "STRING"},
        "stance": {"type": "STRING", "enum": ["supports", "contradicts", "context"]},
        "date": {"type": "STRING"}},
        "required": ["claim", "source_url", "quote"]},
}

DONE_DECL = {
    "name": "subquestion_done",
    "description": ("Close the current question: summary = at most two plain sentences on what the "
                    "evidence says, in the language of the task; confidence = low | medium | high."),
    "parameters": {"type": "OBJECT", "properties": {
        "summary": {"type": "STRING"},
        "confidence": {"type": "STRING", "enum": ["low", "medium", "high"]}},
        "required": ["summary"]},
}

_CHECK_LABEL = {"exact": "quote verified", "partial": "quote partly verified",
                "missing": "QUOTE NOT FOUND ON THE PAGE — unverified",
                "unread": "page could not be read — unverified", "no-quote": "no quote — unverified"}


class _Stop(Exception):
    """Unwind out of the runner: the task has been parked, cancelled or failed."""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    # Turkish dotted capital İ casefolds to i + a combining dot; drop the dot so
    # "İstanbul" and "istanbul" compare equal, as a reader would expect.
    text = text.replace(chr(0x307), "")          # U+0307 COMBINING DOT ABOVE
    return " ".join(re.findall(r"\w+", text))


def quote_support(quote: str, page: str) -> str:
    """exact | partial | missing — is `quote` really on `page`?

    Words only, case and punctuation ignored, because extraction reflows lines
    and a model reasonably drops a comma. Long quotes that were trimmed or
    joined count as partial when most of their six-word runs are present."""
    q, p = _norm(quote), _norm(page)
    if not q or not p:
        return "missing"
    if q in p:
        return "exact"
    words = q.split()
    if len(words) < 8:
        return "missing"
    windows = [" ".join(words[i:i + 6]) for i in range(0, len(words) - 5, 3)]
    hits = sum(1 for w in windows if w in p) / max(1, len(windows))
    return "partial" if hits >= 0.6 else "missing"


def parse_json(text: str):
    """The JSON object in a model's reply, or None.

    Delegates to core.gemini.parse_json: slicing from the first "{" to the last
    "}" broke on a reply whose prose after the object contained a brace, and on
    reasoning blocks. Every caller here wants an object, so anything else is
    still None."""
    from core.gemini import parse_json as _parse
    value = _parse(str(text or ""))
    return value if isinstance(value, dict) else None


def fix_citations(body: str, valid: set) -> str:
    """Every [n] and [n, m] in the report either names a real source or becomes [?]."""
    def one(m):
        nums = [x.strip() for x in m.group(1).split(",")]
        return "[" + ", ".join(x if int(x) in valid else "?" for x in nums) + "]"
    return re.sub(r"\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]", one, body)


def _plain(text: str, limit: int) -> str:
    """One line of plain text: no Markdown headings or emphasis, no line breaks."""
    text = re.sub(r"[#*_`>|]+", " ", str(text or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _url_key(url: str) -> str:
    url = urldefrag(str(url or "").strip())[0]
    return url[:-1] if url.endswith("/") else url


class ResearchRunner:
    def __init__(self, engine, tid: str, flags: dict, ask, tools, profile):
        self.e, self.tid, self.flags, self.ask = engine, tid, flags, ask
        self.tools, self.profile = tools, profile
        self.store = engine.store
        self.wd = self.store.workdir(tid)
        (self.wd / "pages").mkdir(parents=True, exist_ok=True)
        tools.on_page = self._on_page
        task = self.store.get(tid)
        self.used_s = float(task["used"]["seconds"])
        self.rounds = int(task["used"]["rounds"])
        self._auto_tried: set[str] = set()

    # ── task state ───────────────────────────────────────────────────────────
    def _task(self) -> dict:
        return self.store.get(self.tid)

    def _state(self) -> dict:
        return dict(self._task().get("research") or {})

    def _set_state(self, **fields) -> None:
        self.store.mutate(self.tid, lambda t: t.setdefault("research", {}).update(fields))

    def _budget_s(self) -> float:
        return self._task()["budget"]["minutes"] * 60.0

    def _left(self) -> float:
        return self._budget_s() - self.used_s

    def _reserve(self) -> float:
        return max(MIN_WRITE_SECONDS, self._budget_s() * WRITE_SHARE)

    def _spend(self, seconds: float, rounds: int = 0) -> None:
        self.used_s += max(0.0, seconds)
        self.rounds += rounds
        self.e._account(self.tid, self.used_s, self.rounds)

    def _check(self) -> None:
        if self.flags["halt"]:
            raise _Stop()
        if self.flags["cancel"]:
            self.e._finish(self.tid, "cancelled")
            raise _Stop()

    def _wrap(self) -> bool:
        return bool(self.flags["finish"] or self._task().get("wrap_up"))

    def _progress(self, text: str) -> None:
        self.e._event(self.tid, "progress", text)
        self.e._notify("progress", self.tid, text)

    def _pull_steering(self, turns: list | None = None) -> None:
        notes = self._task().get("notes_in") or []
        if not notes:
            return
        self.store.mutate(self.tid, lambda t: (
            t.setdefault("research", {}).setdefault("user_updates", []).extend(notes),
            t.update(notes_in=[])))
        if turns is not None:
            turns.append({"role": "user", "text": "[UPDATE FROM THE USER, while you work]\n"
                                                  + "\n".join(f"- {n}" for n in notes)})

    def _updates_block(self) -> str:
        ups = self._state().get("user_updates") or []
        return ("\n[UPDATES FROM THE USER]\n" + "\n".join(f"- {u}" for u in ups)) if ups else ""

    # ── model calls ──────────────────────────────────────────────────────────
    def _system(self, phase: str, body: str) -> str:
        base = self.e._system_prompt(self._task(), self.profile, True)
        return f"{base}\n\n[PHASE: {phase}]\n{body.strip()}"

    def _call(self, system: str, turns: list, decls) -> dict:
        began = self.e._clock()
        try:
            return self.ask(system, trim_turns(turns), decls)
        except NoModel as err:
            self._spend(self.e._clock() - began)
            if err.transient:
                self.store.update(self.tid, status="waiting_quota",
                                  quota_until=self.e._clock() + QUOTA_WAIT)
                self.e._event(self.tid, "state", "waiting for a model: " + str(err)[:160])
                self.e._notify("waiting", self.tid, str(err))
            else:
                self.e._finish(self.tid, "failed", error=str(err))
            raise _Stop() from None

    def _soft_call(self, system: str, turns: list) -> dict | None:
        """A model call whose failure costs only itself — used once the report
        exists, so running out of quota during a check never loses the report."""
        began = self.e._clock()
        try:
            return self.ask(system, trim_turns(turns), None)
        except NoModel as err:
            self.e._event(self.tid, "state", "verification skipped: " + str(err)[:120])
            return None
        finally:
            self._spend(self.e._clock() - began)

    # ── sources, pages, findings ─────────────────────────────────────────────
    def _sources(self) -> dict:
        from core.tasks.store import read_json
        data = read_json(self.wd / "sources.json", {})
        return data if isinstance(data, dict) else {}

    def _save_sources(self, data: dict) -> None:
        from core.tasks.store import write_json
        write_json(self.wd / "sources.json", data)

    def _next_n(self, data: dict) -> int:
        return 1 + max([s["n"] for s in data.values() if s.get("n")] or [0])

    def _source(self, url: str, title: str = "") -> dict:
        data = self._sources()
        key = _url_key(url)
        entry = data.get(key)
        if entry is None:
            entry = {"n": self._next_n(data), "url": url, "title": "", "read": False, "page": ""}
        if title and not entry.get("title"):
            entry["title"] = str(title)[:200]
        data[key] = entry
        self._save_sources(data)
        return entry

    def _on_page(self, requested: str, doc: dict) -> None:
        """A page was read in full: keep its text, and give the address it was
        asked for and the address it ended at (after redirects) one number."""
        name = hashlib.sha1(_url_key(doc["url"]).encode("utf-8")).hexdigest()[:16] + ".txt"
        (self.wd / "pages" / name).write_text(doc["text"], encoding="utf-8")
        data = self._sources()
        keys = {_url_key(requested), _url_key(doc["url"])}
        known = [data[k] for k in keys if k in data]
        entry = dict(known[0]) if known else {"n": self._next_n(data), "url": doc["url"],
                                              "title": "", "read": False, "page": ""}
        entry.update(read=True, page=name, title=entry.get("title") or doc.get("title") or "")
        for k in keys:
            data[k] = entry
        self._save_sources(data)

    def _page_text(self, url: str) -> str:
        entry = self._sources().get(_url_key(url))
        if not entry or not entry.get("page"):
            return ""
        try:
            return (self.wd / "pages" / entry["page"]).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _findings(self) -> list[dict]:
        path = self.wd / "findings.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def _record(self, sid: int, args: dict) -> str:
        claim = " ".join(str(args.get("claim") or "").split())[:700]
        url = str(args.get("source_url") or "").strip()
        if not claim or not url.startswith(("http://", "https://")):
            return outcome.failed("record_finding", "needs the claim and the http(s) source_url it came from")
        page = self._page_text(url)
        if not page and "fetch_page" in self.tools.names and _url_key(url) not in self._auto_tried:
            # Recorded from a page never opened: open it now, so the quote is
            # checked against the page rather than taken on trust.
            self._auto_tried.add(_url_key(url))
            self.e._event(self.tid, "tool", f"fetch_page {url[:120]} (to check a quote)")
            self.tools.execute("fetch_page", {"url": url})
            page = self._page_text(url)
        src = self._source(url, title=args.get("source_title") or "")
        quote = " ".join(str(args.get("quote") or "").split())[:500]
        if not page:
            support = "unread"
        elif not quote:
            support = "no-quote"
        else:
            support = quote_support(quote, page)
        rec = {"n": src["n"], "subq": sid, "claim": claim, "quote": quote, "support": support,
               "stance": str(args.get("stance") or "supports")[:12], "date": str(args.get("date") or "")[:40]}
        with (self.wd / "findings.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.e._event(self.tid, "finding", f"[{src['n']}] {claim}")
        reply = outcome.done("record_finding", source=src["n"], quote_check=support)
        if support == "unread":
            reply += " -- The page could not be read, so this finding counts as unverified."
        elif support == "missing":
            reply += (" -- That quote is not on the page. Copy the exact words from the page, or this "
                      "finding counts as unverified.")
        elif support == "no-quote":
            reply += " -- Without a quote this finding counts as unverified; add the exact words."
        return reply

    # ── the stages ───────────────────────────────────────────────────────────
    def run(self) -> None:
        now = self.e._clock()
        task = self._task()
        self.store.mutate(self.tid, lambda t: t.update(status="running", started=t.get("started") or now))
        if not task.get("started"):
            self.e._notify("started", self.tid, "")
        self.e._event(self.tid, "state", "running")
        try:
            while True:
                self._check()
                phase = self._state().get("phase") or "plan"
                if self._wrap() and phase in ("plan", "investigate", "review"):
                    self._set_state(phase="write")
                    continue
                if phase == "plan":
                    self._plan()
                elif phase == "investigate":
                    self._investigate()
                elif phase == "review":
                    self._review()
                else:
                    self._write()
                    return
        except _Stop:
            return

    def _plan(self) -> None:
        task = self._task()
        minutes = task["budget"]["minutes"]
        n = 2 if minutes <= 3 else 4 if minutes <= 12 else 6 if minutes <= 40 else MAX_SUBQ
        system = self._system("PLAN", f"""
Plan the research. Do not answer the question yet.
Split it into at most {n} sub-questions that together make a complete answer, most
important first. For each, give 2-4 search queries — in the language the topic is
best documented in, plus the task's language where that helps.
Return ONLY a JSON object:
{{"goal": "the question in one sentence",
  "report_language": "the language the task is written in",
  "complete_answer": "what a complete answer must contain",
  "subquestions": [{{"q": "...", "queries": ["...", "..."]}}]}}""")
        began = self.e._clock()
        self._pull_steering()
        reply = self._call(system, [{"role": "user", "text": f"[TASK]\n{task['task']}"
                                                              f"{self._updates_block()}"}], None)
        self._spend(self.e._clock() - began, 1)
        data = parse_json(reply.get("text")) or {}
        subqs = [s for s in (data.get("subquestions") or [])
                 if isinstance(s, dict) and str(s.get("q") or "").strip()]
        if not subqs:
            subqs = [{"q": task["task"], "queries": []}]
        plan = {"goal": str(data.get("goal") or task["task"])[:600],
                "language": str(data.get("report_language") or "")[:40],
                "complete": str(data.get("complete_answer") or "")[:800],
                "subqs": [{"id": i + 1, "q": _plain(s["q"], 300),
                           "queries": [str(x)[:200] for x in (s.get("queries") or [])][:4],
                           "status": "pending", "rounds": 0, "summary": "", "confidence": "",
                           "nudged": False, "gap": False}
                          for i, s in enumerate(subqs[:n])]}
        from core.tasks.store import write_json
        write_json(self.wd / "plan.json", plan)
        self._set_state(phase="investigate", plan=plan, cycle=0)
        self._progress(f"Plan: {len(plan['subqs'])} questions — " + " | ".join(s["q"] for s in plan["subqs"]))

    def _mark(self, sid: int, **fields) -> None:
        if "summary" in fields:
            fields["summary"] = _plain(fields["summary"], 400)

        def fn(t):
            for s in t["research"]["plan"]["subqs"]:
                if s["id"] == sid:
                    s.update(fields)
        self.store.mutate(self.tid, fn)

    def _subq(self, sid: int) -> dict:
        return next(s for s in self._state()["plan"]["subqs"] if s["id"] == sid)

    def _investigate(self) -> None:
        for sq in self._state()["plan"]["subqs"]:
            if sq["status"] == "done":
                continue
            self._check()
            if self._wrap() or self._left() <= self._reserve():
                break
            self._subquestion(sq["id"])
        pending = [s for s in self._state()["plan"]["subqs"] if s["status"] != "done"]
        for s in pending:
            self._mark(s["id"], status="done", confidence=s.get("confidence") or "low",
                       summary=s.get("summary") or "not investigated: out of time")
        self._set_state(phase="write" if (self._wrap() or self._left() <= self._reserve()) else "review")

    def _subquestion(self, sid: int) -> None:
        plan = self._state()["plan"]
        sq = next(s for s in plan["subqs"] if s["id"] == sid)
        pending = max(1, sum(1 for s in plan["subqs"] if s["status"] != "done"))
        allot_s = max(30.0, (self._left() - self._reserve()) / pending)
        rounds_left = self._task()["budget"]["rounds"] - self.rounds
        allot_r = max(3, min(30, rounds_left // pending))
        began_s, began_r = self.used_s, int(sq.get("rounds") or 0)

        key = f"sq{sid}"
        turns = self.store.load_turns(self.tid, key)
        if not turns:
            others = "\n".join(f"  {s['id']}. {s['q']}" + ("  (done)" if s["status"] == "done" else "")
                               for s in plan["subqs"])
            turns = [{"role": "user", "text": (
                f"[RESEARCH GOAL] {plan['goal']}\n[THE PLAN]\n{others}\n\n"
                f"[NOW: QUESTION {sid}] {sq['q']}\n"
                f"Suggested searches: {'; '.join(sq['queries']) or 'your own'}\n"
                f"Time for this question: about {allot_s / 60:.1f} minutes, {allot_r} rounds."
                f"{self._updates_block()}")}]
            self.store.save_turns(self.tid, turns, key)
        self.e._event(self.tid, "progress", f"Question {sid}: {sq['q']}")

        system = self._system("INVESTIGATE", f"""
Investigate ONE question from the plan.
- Find sources with search_web (several phrasings, other languages where useful), then OPEN
  the best results with fetch_page before recording anything from them. Prefer primary
  sources — the study, the official page, the original announcement — and check how recent
  they are.
- Record every useful fact with record_finding: the claim, the source_url, and a short quote
  copied EXACTLY from the page. Record evidence that contradicts, too.
- The question is done when at least {MIN_SOURCES}-3 independent sources have been read and
  recorded, or when searching only repeats what you have. Then call subquestion_done.
- Only recorded findings reach the report. Do not write the report here.""")
        decls = self.tools.declarations() + [FINDING_DECL, DONE_DECL]
        seen: dict[str, str] = {}
        wrap_rounds = 0
        while True:
            self._check()
            self._pull_steering(turns)
            rounds_here = int(self._subq(sid).get("rounds") or 0) - began_r
            spent_here = self.used_s - began_s
            wrap = (self._wrap() or self._left() <= self._reserve()
                    or spent_here >= allot_s or rounds_here >= allot_r)
            now_decls = decls
            if wrap:
                wrap_rounds += 1
                if wrap_rounds > 2:
                    self._mark(sid, status="done", confidence="low",
                               summary="closed for time before a summary was written")
                    return
                turns.append({"role": "user", "text": "[WRAP UP THIS QUESTION] Time for it is spent. "
                                                      "Record any findings not yet recorded, then call "
                                                      "subquestion_done."})
                now_decls = [FINDING_DECL, DONE_DECL]

            round_began = self.e._clock()
            reply = self._call(system, turns, now_decls)
            turns.append({"role": "model", "text": reply.get("text") or "", "calls": reply.get("calls") or []})
            calls = reply.get("calls") or []
            if not calls:
                self._mark(sid, status="done", summary=reply.get("text") or "", confidence="")
                self.store.save_turns(self.tid, turns, key)
                self._spend(self.e._clock() - round_began, 1)
                return
            if reply.get("text"):
                self._progress(reply["text"])

            results, closed = [], False
            for call in calls:
                if self.flags["cancel"] or self.flags["halt"]:
                    break
                name, args = call["name"], call.get("args") or {}
                if name == "record_finding":
                    res = self._record(sid, args)
                elif name == "subquestion_done":
                    res, closed = self._close(sid, args, wrap, spent_here / max(1.0, allot_s))
                else:
                    sig = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                    if sig not in seen:
                        self.e._event(self.tid, "tool", f"{name} {self.e._arg_hint(args)}")
                        seen[sig] = self.tools.execute(name, args)
                    res = seen[sig]
                results.append({"id": call["id"], "name": name, "result": res})
            if len(results) < len(calls):
                turns.pop()
                self.store.save_turns(self.tid, turns, key)
                continue
            turns.append({"role": "tool", "results": results})
            self.store.save_turns(self.tid, turns, key)
            self._mark(sid, rounds=int(self._subq(sid).get("rounds") or 0) + 1)
            self._spend(self.e._clock() - round_began, 1)
            if closed:
                return

    def _close(self, sid: int, args: dict, wrapping: bool, used_fraction: float) -> tuple[str, bool]:
        read = {e["n"] for e in self._sources().values() if e.get("read")}
        mine = {f["n"] for f in self._findings() if f["subq"] == sid and f["n"] in read}
        sq = self._subq(sid)
        if not wrapping and not sq.get("nudged") and len(mine) < MIN_SOURCES and used_fraction < 0.6:
            self._mark(sid, nudged=True)
            return (outcome.failed("subquestion_done",
                                   f"not yet: findings for this question come from {len(mine)} page(s) read "
                                   f"in full. Read at least {MIN_SOURCES - len(mine)} more independent "
                                   f"source(s) with fetch_page and record what they say — unless you have "
                                   f"searched and there are none; then call this again and say so."), False)
        self._mark(sid, status="done", summary=str(args.get("summary") or ""),
                   confidence=str(args.get("confidence") or "")[:8])
        return outcome.done("subquestion_done", question=sid), True

    def _digest(self, findings: list[dict], renumber: dict | None = None, limit: int = 60_000) -> str:
        plan = self._state()["plan"]
        lines = []
        for sq in plan["subqs"]:
            lines.append(f"\n## Question {sq['id']}: {sq['q']}"
                         + (f"\n(summary: {sq['summary']}; confidence: {sq['confidence'] or '?'})"
                            if sq.get("summary") else ""))
            for f in (x for x in findings if x["subq"] == sq["id"]):
                n = renumber.get(f["n"], f["n"]) if renumber else f["n"]
                quote = f' — "{f["quote"]}"' if f["quote"] else ""
                date = f" ({f['date']})" if f["date"] else ""
                stance = "" if f["stance"] == "supports" else f" [{f['stance']}]"
                lines.append(f"[{n}]{stance} {f['claim']}{quote}{date} · "
                             f"{_CHECK_LABEL.get(f['support'], f['support'])}")
        text = "\n".join(lines).strip()
        return text if len(text) <= limit else text[:limit] + "\n[findings list truncated]"

    def _review(self) -> None:
        state = self._state()
        cycle = int(state.get("cycle") or 0)
        if cycle >= MAX_CYCLES or self._left() <= self._reserve() * 1.5:
            self._set_state(phase="write")
            return
        self._pull_steering()
        plan = state["plan"]
        system = self._system("REVIEW", f"""
Review the evidence against the goal. Look for: questions with weak or no evidence, claims
that rest on one source or on unverified findings, contradictions not yet resolved, and
anything a complete answer needs that is missing ({plan.get('complete') or 'use judgement'}).
Return ONLY a JSON object: {{"gaps": [{{"q": "a new, specific question", "queries": ["..."],
"why": "..."}}]}} — at most {MAX_GAPS} gaps, and an empty list when the evidence is enough.""")
        began = self.e._clock()
        reply = self._call(system, [{"role": "user", "text": f"[GOAL] {plan['goal']}{self._updates_block()}\n\n"
                                                              f"[FINDINGS]\n{self._digest(self._findings())}"}],
                           None)
        self._spend(self.e._clock() - began, 1)
        gaps = [g for g in ((parse_json(reply.get("text")) or {}).get("gaps") or [])
                if isinstance(g, dict) and str(g.get("q") or "").strip()][:MAX_GAPS]
        if not gaps and self._left() > self._budget_s() * 0.5 and not state.get("deepened"):
            # "Enough" with more than half the budget unspent is usually modesty,
            # not coverage: the user asked for this much time. Ask once what would
            # most improve the report before accepting it.
            self._set_state(deepened=True)
            began = self.e._clock()
            reply = self._call(system, [{"role": "user", "text": (
                f"[GOAL] {plan['goal']}{self._updates_block()}\n\n"
                f"[FINDINGS]\n{self._digest(self._findings())}\n\n"
                f"[TIME] About {self._left() / 60:.0f} minutes of the budget left. Before the "
                f"report is written, name up to {MAX_GAPS} follow-up questions that would most "
                f"improve it: independent confirmation of claims that rest on one source, evidence "
                f"that could contradict the findings, recent changes, and what a careful reader "
                f"would ask next. Return the same JSON; an empty list only if nothing would add "
                f"value.")}], None)
            self._spend(self.e._clock() - began, 1)
            gaps = [g for g in ((parse_json(reply.get("text")) or {}).get("gaps") or [])
                    if isinstance(g, dict) and str(g.get("q") or "").strip()][:MAX_GAPS]
        if not gaps:
            self._set_state(phase="write", cycle=cycle + 1)
            self._progress("Review: the evidence covers the question")
            return
        next_id = 1 + max(s["id"] for s in plan["subqs"])
        new = [{"id": next_id + i, "q": _plain(g["q"], 300),
                "queries": [str(x)[:200] for x in (g.get("queries") or [])][:4],
                "status": "pending", "rounds": 0, "summary": "", "confidence": "",
                "nudged": False, "gap": True} for i, g in enumerate(gaps)]
        self.store.mutate(self.tid, lambda t: (t["research"]["plan"]["subqs"].extend(new),
                                                t["research"].update(phase="investigate", cycle=cycle + 1)))
        self._progress(f"Review: {len(new)} gap(s) to close — " + " | ".join(s["q"] for s in new))

    def _write(self) -> None:
        task, state = self._task(), self._state()
        plan = state.get("plan") or {"goal": task["task"], "language": "", "subqs": []}
        findings = self._findings()
        by_n = {}
        for e in self._sources().values():
            if e.get("n"):
                by_n[e["n"]] = e
        used = []
        for f in findings:
            if f["n"] not in used and f["n"] in by_n:
                used.append(f["n"])
        renumber = {old: i + 1 for i, old in enumerate(used)}
        valid = set(renumber.values())
        language = plan.get("language") or "the language the task is written in"

        system = self._system("WRITE", f"""
Write the research report from the FINDINGS below — and only from them. Write all of it in
{language}, including any note that something could not be verified.
Structure, with Markdown headings:
- a short summary that answers the question first;
- the findings, organised by theme rather than by the order they were found;
- where sources disagree, and what remains uncertain.
Cite findings with their numbers in square brackets, like [3] or [3, 5]. Use only numbers from
the list. A finding marked unverified may be used, with a brief note in {language} that it
could not be confirmed. Do not add a title, and do not write a list of sources — both are
added for you.""")
        began = self.e._clock()
        reply = self._call(system, [{"role": "user", "text": (
            f"[TASK]\n{task['task']}{self._updates_block()}\n\n[GOAL] {plan['goal']}\n\n"
            f"[FINDINGS]\n{self._digest(findings, renumber) or '(nothing was recorded)'}")}], None)
        self._spend(self.e._clock() - began, 1)
        body = fix_citations((reply.get("text") or "").strip() or "No report could be written.", valid)

        # One verification pass while the budget and a model allow it: the model
        # names statements that go beyond the findings, then fixes only those.
        # Both calls are soft — the report already exists, and losing it because
        # the quota ran out during a check would be the worst trade available.
        if findings and self._left() > 20:
            check = self._soft_call(self._system("VERIFY", """
Compare the REPORT with the FINDINGS. List only statements that the findings contradict, or
that claim more than any finding says (a number, a date, a comparison no finding gives). Do not
list a statement just because its finding is marked unverified when the report already says so.
Return ONLY JSON: {"unsupported": [{"statement": "...", "why": "..."}]} — an empty list when
the report stays within its findings."""),
                [{"role": "user", "text": f"[REPORT]\n{body}\n\n[FINDINGS]\n{self._digest(findings, renumber)}"}])
            problems = [p for p in ((parse_json((check or {}).get("text")) or {}).get("unsupported") or [])
                        if isinstance(p, dict) and p.get("statement")][:8]
            if problems:
                fixed = self._soft_call(self._system("REVISE", f"""
Revise the report: for each listed statement, bring it back to what the findings say, or remove
it. Change nothing else. Keep {language} throughout, the headings and the [n] citations.
Return the whole revised report and nothing else."""),
                    [{"role": "user", "text": f"[REPORT]\n{body}\n\n[UNSUPPORTED]\n"
                                              + "\n".join(f"- {p['statement']} ({p.get('why', '')})"
                                                          for p in problems)
                                              + f"\n\n[FINDINGS]\n{self._digest(findings, renumber)}"}])
                text = ((fixed or {}).get("text") or "").strip()
                if len(text) > len(body) * 0.5:
                    body = fix_citations(text, valid)
                    self._progress(f"Verified: {len(problems)} statement(s) corrected")
            elif check is not None:
                self._progress("Verified: the report stays within its findings")

        read = sum(1 for n in used if by_n[n].get("read"))
        verified = sum(1 for f in findings if f["support"] in ("exact", "partial"))
        src_lines = [f"{renumber[n]}. {by_n[n].get('title') or by_n[n]['url']} — {by_n[n]['url']}"
                     + ("" if by_n[n].get("read") else "  *(search result only)*") for n in used]
        # Questions and confidence only: the summaries were written mid-investigation
        # and not always in the report's language; the report itself carries them.
        method = [f"- {s['q']}" + (f" · {s['confidence']}" if s.get("confidence") else "")
                  for s in plan["subqs"]]
        result = "\n".join([
            body, "", "## Sources", "", "\n".join(src_lines) or "_No sources were recorded._", "",
            "## Method", "",
            f"{len(findings)} findings from {len(used)} sources ({read} read in full); "
            f"{verified} of {len(findings)} quotes verified against the page; "
            f"{int(state.get('cycle') or 0)} review cycle(s).",
            "", "\n".join(method)])
        self._set_state(phase="done")
        self.e._complete(self.tid, result)
