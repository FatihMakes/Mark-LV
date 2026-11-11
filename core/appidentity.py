"""
core/appidentity.py — what applications this machine is running, and which of
them the user just named.

WHY THIS IS ONE PLACE AND NOT TWO
    Opening an application and closing one are the same question asked twice.
    "Spotify" has to become a launcher name to start it, and has to become a
    RUNNING PROCESS to stop it — and the messy part, turning what a person said
    into something the operating system recognises, is identical either way.
    Written twice, the two drift, and the day they disagree is the day "close
    Spotify" closes something else.

HOW A NAME IS MATCHED, AND WHY THERE IS NO LIST OF APPLICATIONS
    Nothing here decides in advance which applications exist. The candidates
    are whatever is running on this machine right now, read from the process
    table, and the user's word is compared against each one's process name,
    executable name and window titles. A machine in Jakarta running software
    nobody here has heard of works exactly as well as this one.

    `ALIASES` is not a list of permitted applications and never restricts
    anything: it maps a few names that differ BETWEEN OPERATING SYSTEMS — the
    browser called `chrome` on Windows is `Google Chrome` on macOS and
    `google-chrome` on Linux. Anything not in it passes through untouched, and
    matching then falls back on what is actually running. Delete the whole map
    and the only thing that breaks is convenience on three-way-named apps.

MATCHING IS RANKED, NOT BOOLEAN
    A substring test alone answers "close discord" with Discord AND
    DiscordCanary AND a browser tab process, and picks whichever came back
    first. So every candidate is scored — an exact name beats a prefix, a
    prefix beats a substring, a process with a visible window beats a
    background helper — and the caller is handed the ranking. When the top two
    are equally good, that is a question for the user, not a coin toss.

THE ASSISTANT IS NEVER A TARGET
    The bug this module was written for: "close the app" ran
    `pyautogui.hotkey("alt", "f4")` against whatever had focus, and while
    somebody is talking to the assistant the window with focus is very often
    the assistant. It closed itself, and reported nothing at all.

    `own_pids()` is the answer, and it is deliberately generous: this process,
    every process it started, and every ancestor up to the session leader —
    because closing the terminal or launcher that owns us kills us just as
    dead as closing us directly. Callers are told a match is `is_self` and
    refuse rather than guess.
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from core.lazyimport import LazyModule, installed

_PSUTIL = installed("psutil")
psutil = LazyModule("psutil")

_OS = platform.system()          # "Windows" | "Darwin" | "Linux"

# Everything that is not a letter or a digit, in any script. Spaces, dots,
# hyphens and underscores all vanish, so "Visual Studio Code", "visual-studio
# code" and "VisualStudioCode" become one string.
_PUNCT = re.compile(r"[^\w]|_", re.UNICODE)


def fold(text: str) -> str:
    """A name reduced to what two spellings of it have in common.

    Decompose, drop the combining marks, drop everything that is not a letter
    or a digit, then casefold. "Café", "CAFE" and "cafe" all land on `cafe`;
    "Ünlü" matches "unlu"; Cyrillic and Greek fold by the same rule rather than
    by a table of exceptions.

    This is the same rule `plugins/_whatsapp_core.fold()` applies to contact
    names, arrived at there first. The two are deliberately identical and
    should one day be one function; they are apart today only because `core`
    must not import from `plugins`, and moving that one would touch a file that
    is working.

    Turkish dotless ı is the known limit: `casefold()` maps I→i but leaves ı
    alone, so "ısı" and "isi" stay different. That is correct for words and
    almost never reached for application names, which are Latin and
    lowercase-safe. Fixing it properly needs locale-aware casing, which is a
    different job than this one.
    """
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _PUNCT.sub("", text).casefold()


# ─────────────────────────────────────────────────────────────────────────────
#  Cross-platform launcher names
# ─────────────────────────────────────────────────────────────────────────────
# Read the module docstring before adding to this: it exists ONLY for names
# that genuinely differ between operating systems. A name that is the same
# everywhere does not belong here — it already works without an entry.
ALIASES: dict[str, dict[str, str]] = {
    "chrome":             {"Windows": "chrome",            "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",            "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",           "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",            "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",             "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",            "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",             "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",          "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",          "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",           "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",             "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",              "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",           "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",             "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",            "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",           "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",               "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",           "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",              "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",              "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",              "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",           "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",    "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",           "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",          "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",             "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",           "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",           "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",             "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",          "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",           "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",       "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",       "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",      "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",      "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",      "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",       "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",      "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",          "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",       "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",         "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",            "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",            "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",          "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",            "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",             "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher", "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher", "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _contains_word(phrase: str, word: str) -> bool:
    """Is `word` in `phrase` as a whole word, not as a fragment of a longer one?

    THIS EXISTS BECAUSE THE LOOSE VERSION MAPPED THE WRONG APPLICATION.

    The rule used to be `if alias in key or key in alias`, and measured against
    a real process table it turned "msedgewebview2" into "msedge": the alias
    "edge" appears inside it, so a browser-embedding runtime resolved to the
    browser. Every application that embeds a component named after another one
    has the same problem, and the day it matters is the day "close X" closes
    something the user was working in.

    `plugins/_whatsapp_core.name_in_phrase()` found this first, against contact
    names, and the fix is the same: the match has to begin and end where a word
    does. A letter or a digit against either edge means it was cut out of the
    middle of something longer.
    """
    start = phrase.find(word)
    while start != -1:
        end = start + len(word)
        before_ok = start == 0 or not phrase[start - 1].isalnum()
        after_ok = end == len(phrase) or not phrase[end].isalnum()
        if before_ok and after_ok:
            return True
        start = phrase.find(word, start + 1)
    return False


def launcher_name(spoken: str) -> str:
    """The name this OS starts the application by, or the word itself.

    Falling through unchanged is the important half: it is what makes an
    application nobody anticipated work, and it is why this map restricts
    nothing.

    Only two ways to match, both deliberate: the whole phrase IS an alias, or
    an alias appears in it as a whole word — so "google chrome" and "open
    chrome please" resolve, and "msedgewebview2" does not. The old third way,
    the user's word being a fragment of an alias, is gone: it meant a two-letter
    utterance resolved to whichever alias happened to contain those letters.
    """
    key = (spoken or "").lower().strip()
    if not key:
        return spoken
    if key in ALIASES:
        return ALIASES[key].get(_OS, spoken)
    # Longest alias first, so "google chrome" is not decided by "chrome".
    for alias in sorted(ALIASES, key=len, reverse=True):
        if _contains_word(key, alias):
            return ALIASES[alias].get(_OS, spoken)
    return spoken


def spelling_variants(spoken: str) -> list[str]:
    """Every folded spelling this request could reasonably be running under.

    Both the word the user said and the OS launcher name for it, because the
    two differ exactly where it matters: somebody says "vscode" and the process
    is called `Code`. Deduplicated, empties dropped.
    """
    out: list[str] = []
    for candidate in (spoken, launcher_name(spoken)):
        folded = fold(candidate)
        if folded and folded not in out:
            out.append(folded)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  This assistant's own processes — never a target
# ─────────────────────────────────────────────────────────────────────────────
_OWN_EXES: Optional[set[str]] = None


def own_exes(chain: Optional[set[int]] = None) -> set[str]:
    """The executable files this assistant is running from, lowercased.

    Cached for the life of the process, because it cannot change: our own
    binaries do not move while we are running out of them. That matters —
    reading `.exe` for every process on the machine is most of the cost of
    working out what is ours, and it should be paid once rather than on every
    request to close something.
    """
    global _OWN_EXES
    if _OWN_EXES is not None:
        return _OWN_EXES
    if not _PSUTIL:
        _OWN_EXES = set()
        return _OWN_EXES

    # sys.executable goes in unconditionally, before anything is scanned.
    #
    # Everything else here depends on reading the process table, and a read can
    # fail: AccessDenied on a process, a parent that exits mid-walk, a chain
    # that comes back short. Every one of those failure modes silently SHRINKS
    # the protected set, and the thing it can shrink away is the interpreter
    # running this code.
    #
    # This one line cannot fail and cannot be raced. It was added after a
    # regression run in which `close_app("python")` answered `status=done` —
    # the guard held on every attempt to reproduce it, which is exactly why it
    # needed a floor that does not depend on reproducing anything.
    found: set[str] = set()
    if sys.executable:
        found.add(os.path.normcase(sys.executable))
    for pid in (chain or _chain_pids()):
        try:
            exe = psutil.Process(pid).exe()
            if exe:
                found.add(os.path.normcase(exe))
        except Exception:
            # A permission error, or a process that exited between the two
            # calls. Either way it is one fewer path to compare against.
            continue
    _OWN_EXES = found
    return _OWN_EXES


_SELF_EXES: Optional[set[str]] = None


def _ancestor_pids() -> set[int]:
    """This process and the ones it came from. NOT the ones it started.

    THE DISTINCTION THIS EXISTS TO DRAW, AND WHAT IGNORING IT COST
        `_chain_pids()` below answers "what must never be closed", and for that
        question a descendant belongs in the set: a helper this process spawned
        is the assistant's own machinery.

        It is the wrong answer to a completely different question — "is this
        process the assistant" — and the two were sharing one implementation.
        Every application the assistant LAUNCHES becomes a descendant at the
        instant it exists, so `open_app` and `browser_control` were making their
        own results unreadable: the thing they had just started was classified
        as us and hidden from every search meant to find it.

        Measured, with nine live chrome.exe processes on screen: after the
        assistant launched Chrome, `running("chrome")` returned nothing, the
        browser tool reported `browser_running=false` about a browser the user
        was looking at, and `screenview` refused to read the Chrome window on
        the grounds that it was the assistant's own.

        Worse than either, because `own_exes()` collects the EXECUTABLES of the
        chain: one launch put chrome.exe into the set of "our own binaries", so
        every other copy of Chrome on the machine became us too.

    Ancestors stay in, and they earn it: the terminal that owns this process
    kills it when it goes, so it is genuinely part of what the assistant is.
    """
    pids = {os.getpid()}
    if not _PSUTIL:
        return pids
    try:
        parent, seen = psutil.Process(os.getpid()).parent(), set()
        while parent is not None and parent.pid not in seen and parent.pid > 1:
            seen.add(parent.pid)
            pids.add(parent.pid)
            try:
                parent = parent.parent()
            except Exception:
                break
    except Exception as exc:
        print(f"[appidentity] could not walk the parent chain: {exc}")
    return pids


def self_exes() -> set[str]:
    """The executables the assistant IS, lowercased — never the ones it starts.

    The narrow twin of `own_exes()`. That one describes everything that must
    survive a close request, which includes whatever this process spawned. This
    one answers identity, so it is built from `_ancestor_pids()` and from
    `sys.executable`, both of which are fixed for the life of the process and
    neither of which can be contaminated by launching an application.
    """
    global _SELF_EXES
    if _SELF_EXES is not None:
        return _SELF_EXES
    found: set[str] = set()
    if sys.executable:
        found.add(os.path.normcase(sys.executable))
    if _PSUTIL:
        for pid in _ancestor_pids():
            try:
                exe = psutil.Process(pid).exe()
                if exe:
                    found.add(os.path.normcase(exe))
            except Exception:
                continue
    _SELF_EXES = found
    return _SELF_EXES


def _chain_pids() -> set[int]:
    """This process, its descendants and its ancestors — no full scan.

    Split out from `own_pids()` so the cheap half can be used on its own by
    `running()`, which is already walking the whole process table and can
    decide sibling membership in the same pass instead of a second one.
    """
    pids = {os.getpid()}
    if not _PSUTIL:
        return pids
    try:
        me = psutil.Process(os.getpid())
        for child in me.children(recursive=True):
            pids.add(child.pid)
        parent, seen = me.parent(), set()
        while parent is not None and parent.pid not in seen and parent.pid > 1:
            seen.add(parent.pid)
            pids.add(parent.pid)
            try:
                parent = parent.parent()
            except Exception:
                break
    except Exception as exc:
        print(f"[appidentity] could not map own process tree: {exc}")
    return pids


def _own_stem() -> str:
    """The folded name of the executable this code is running in — 'python',
    'pythonw', or the packaged build's own name. Used when a process's full
    path cannot be read; see `running()`."""
    return fold(os.path.splitext(os.path.basename(sys.executable or ""))[0])


def own_pids() -> set[int]:
    """Every process whose death would take the assistant with it, or damage it.

    FOUR GROUPS, AND THE FOURTH WAS LEARNED THE HARD WAY

      * this process
      * everything it started — a browser driven by playwright, a helper the
        assistant spawned
      * every ancestor. The terminal or launcher that owns this process kills
        it when it goes, so "close the terminal" while running from one is the
        self-close bug wearing a different hat
      * EVERY PROCESS SHARING AN EXECUTABLE WITH ANY OF THE ABOVE — the
        siblings

    The first three are a CHAIN. A modern desktop application is a TREE: one
    executable, a main process, and a dozen siblings for windows, rendering and
    networking, none of which is an ancestor or a descendant of any particular
    one of the others.

    That gap was not theoretical. Testing this very module against a running
    Electron application, the chain protected the two processes above this one
    and closed thirteen siblings — the main window survived and the application
    was gutted around it. Chain membership was the wrong question; "is this the
    same program I am part of" is the right one, and an executable path answers
    it on every operating system without knowing a single application's name.

    THE BROAD SIDE IS THE SAFE SIDE
        This also protects an unrelated script that happens to run under the
        same interpreter — say "close python" while the assistant is itself
        python, and it refuses and says why. That is the correct direction to
        err: a refusal costs the user one sentence, and closing the assistant
        costs them the conversation.

    Never raises. A process table that cannot be read still yields our own pid,
    so the guard degrades to "never close literally myself" rather than to
    "close anything".
    """
    pids = _chain_pids()
    if not _PSUTIL:
        return pids

    # The siblings, found by executable — see own_exes().
    our_exes = own_exes(pids)
    if our_exes:
        try:
            for proc in psutil.process_iter(["pid", "exe"]):
                try:
                    exe = proc.info.get("exe")
                    if exe and os.path.normcase(exe) in our_exes:
                        pids.add(proc.info["pid"])
                except Exception:
                    continue
        except Exception as exc:
            print(f"[appidentity] sibling scan failed: {exc}")

    return pids


# ─────────────────────────────────────────────────────────────────────────────
#  Window titles (used for matching and for saying what was closed)
# ─────────────────────────────────────────────────────────────────────────────
def _windows_by_pid() -> dict[int, list[str]]:
    """pid -> visible top-level window titles, best effort, never raising.

    Only Windows is implemented natively. macOS and Linux fall back to process
    names, which is enough to match and to close; titles there would each need
    another external tool, and a missing tool must not be the difference
    between closing the right application and the wrong one.
    """
    if _OS != "Windows":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        titles: dict[int, list[str]] = {}

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            titles.setdefault(int(pid.value), []).append(buf.value)
            return True

        user32.EnumWindows(WNDENUMPROC(_collect), 0)
        return titles
    except Exception as exc:
        print(f"[appidentity] window enumeration unavailable: {exc}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
#  Finding what is running
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Match:
    """One running application that could be what the user meant."""

    pid: int
    name: str                              # process name, as the OS reports it
    score: int                             # bigger is a better match
    is_self: bool                          # part of the assistant's own tree
    titles: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What to call this application out loud: its process name, without
        the extension. "Spotify.exe" becomes "Spotify".

        DELIBERATELY NOT THE WINDOW TITLE, THOUGH THAT READS BETTER
            Window titles are the document you have open, the video you are
            watching, the person you are messaging. This value is returned to
            the model, which on the default engine means it leaves the machine.
            Asking to open a browser should not send the title of the video
            playing in it to a cloud API.

            Titles are still collected, because matching needs them and because
            two copies of the same application can only be told apart by them.
            They are used where that is the actual question — never as a
            decoration on a sentence that had a perfectly good name already.
        """
        stem = os.path.splitext(self.name or "")[0]
        return stem or self.name or f"pid {self.pid}"


# Scores. Named rather than inline so the ordering is a statement about intent
# instead of three magic numbers.
_EXACT = 100        # the process IS the word, ignoring case and punctuation
_PREFIX = 60        # the process name starts with it — "code" vs "code.exe"
_CONTAINS = 30      # it appears somewhere inside
_TITLE_ONLY = 20    # nothing in the process name; a window is titled that way
_HAS_WINDOW = 5     # tie-breaker: something the user can actually see


# Below this many characters, only an EXACT match counts.
#
# THIS NUMBER EXISTS BECAUSE "wp" OPENED WPS OFFICE.
#     The user said "open wp", the model passed `wp` through verbatim, and a
#     prefix test matched the running `wps.exe` — WPS Office — which the
#     assistant then reported as WhatsApp already being open. Two letters are
#     a prefix of far too much: "wp" also starts "wpad", "wps", "wpscloudsvr".
#
#     A short string carries almost no evidence, so it is not allowed to make a
#     partial match at all. Four characters is where a prefix starts meaning
#     something: "chro" belongs to Chrome and to essentially nothing else on a
#     normal machine, while "wp" belongs to anything.
_MIN_PARTIAL = 4


def _score(query: str, process_name: str, titles: list[str]) -> int:
    """How well one running process answers to one spoken word.

    Ranked rather than boolean because "discord" matches Discord and
    DiscordCanary and anything with discord in its path, and picking the first
    is how the wrong window gets closed.
    """
    stem = fold(os.path.splitext(process_name or "")[0])
    if not stem:
        return 0
    if stem == query:
        return _EXACT
    # Everything below here is a PARTIAL match, and a partial match on a
    # two-letter word is a coin toss wearing a score. See _MIN_PARTIAL.
    if len(query) < _MIN_PARTIAL:
        return 0
    if stem.startswith(query) or query.startswith(stem):
        return _PREFIX
    if query in stem:
        return _CONTAINS
    if any(query in fold(t) for t in titles):
        return _TITLE_ONLY
    return 0


def running(spoken: str, include_self: bool = False) -> list[Match]:
    """Everything running that answers to `spoken`, best match first.

    Self-matches are EXCLUDED by default but are not silently dropped when
    asked for: `include_self=True` returns them flagged, so a caller can tell
    "nothing matches" apart from "the only thing that matches is me" — two
    situations that need very different sentences.
    """
    if not _PSUTIL:
        return []
    queries = spelling_variants(spoken)
    if not queries:
        return []

    # One pass over the process table, deciding "does this match" and "is this
    # ours" together. Asking `own_pids()` first would walk every process a
    # second time — measured at 418 ms, paid on every request.
    # IDENTITY, not protection. `_chain_pids()` and `own_exes()` describe what
    # must never be closed and deliberately include everything this process
    # started; using them here hid every application the assistant had just
    # launched from the search meant to find it. See `_ancestor_pids()`.
    chain = _ancestor_pids()
    ours = self_exes()
    titles_by_pid = _windows_by_pid()
    out: list[Match] = []

    try:
        procs = list(psutil.process_iter(["pid", "name", "exe"]))
    except Exception as exc:
        print(f"[appidentity] process list unavailable: {exc}")
        return []

    for proc in procs:
        try:
            pid = proc.info.get("pid")
            name = proc.info.get("name") or ""
            if not pid or not name:
                continue
            titles = titles_by_pid.get(pid, [])
            best = max((_score(q, name, titles) for q in queries), default=0)
            if best <= 0:
                continue
            if titles:
                best += _HAS_WINDOW
            # SELF-DETECTION MUST NOT DEPEND ON A READ THAT CAN FAIL.
            #
            # `exe` comes back None for a process that is exiting, and for one
            # the OS will not let us inspect. Treating that as "not ours" is
            # how a python.exe belonging to this very application became a
            # candidate: unreadable path, so not matched against our own, so
            # not self.
            #
            # When the path cannot be read, the NAME is compared instead. A
            # process called python.exe while this code is running inside a
            # python.exe is ours as far as this decision goes — the cost of
            # being wrong that way is one refusal, and the cost of being wrong
            # the other way is ending a process nobody meant to end.
            exe = proc.info.get("exe")
            if exe:
                is_own_binary = os.path.normcase(exe) in ours
            else:
                is_own_binary = fold(os.path.splitext(name)[0]) == _own_stem()
            is_self = pid in chain or is_own_binary
            if is_self and not include_self:
                continue
            out.append(Match(pid=pid, name=name, score=best,
                             is_self=is_self, titles=titles))
        except Exception:
            # A process that exited between listing and reading is normal and
            # is not worth a line in the log.
            continue

    out.sort(key=lambda m: (-m.score, m.pid))
    return out


def is_running(pid: int) -> bool:
    """Is this pid still alive? False when the process table cannot be read,
    because every caller uses this to decide whether something CLOSED, and
    "I cannot see" must not read as "it is gone"."""
    if not _PSUTIL:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def snapshot() -> set[int]:
    """Every pid alive right now — the 'before' half of launch verification."""
    if not _PSUTIL:
        return set()
    try:
        return set(psutil.pids())
    except Exception:
        return set()


def wait_for_launch(spoken: str, before: set[int],
                    timeout: float = 12.0, poll: float = 0.25) -> Optional[Match]:
    """Wait for a NEW process answering to `spoken`, and return it.

    This is what turns "the launcher returned" into "the application exists" —
    the whole reason `open_app` used to lie. It polls rather than sleeping a
    fixed time because applications differ by an order of magnitude: a text
    editor is up in 200 ms and a game launcher takes ten seconds, and one
    constant cannot serve both without being wrong for one of them.

    `before` is a pid snapshot taken BEFORE launching, so an already-running
    copy is not mistaken for the one we just started. The caller checks for
    that case first and says something different about it.

    WHY THIS ASKS FOR SELF-MATCHES AND FILTERS THEM ITSELF
        `running()` hides the assistant's own process tree, and `_chain_pids()`
        counts DESCENDANTS as part of that tree. Both are right for the
        question they were written for, which is "what may I close" — a helper
        this process spawned must never be a target.

        They are wrong for this question. `open_app` starts the application
        from inside the assistant, so the thing we are waiting for IS one of
        our descendants from the instant it exists. It was therefore hidden
        from the very search meant to find it, `wait_for_launch` returned None
        every single time, and `open_app` reported "asked to start but no new
        application appeared" for applications that were open on screen while
        it said so.

        Measured directly: launch notepad.exe from a Python process, and
        `running("notepad")` returns nothing while `running("notepad",
        include_self=True)` returns it flagged as ours.

        So this asks for everything and applies the one exclusion that is still
        meaningful here: a process running OUR OWN interpreter. That check is
        against `sys.executable`'s stem rather than against the set of
        executables in the process tree, because the tree now contains whatever
        we just launched — comparing against it would rule out the answer.
    """
    own = _own_stem()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for match in running(spoken, include_self=True):
            if match.pid in before:
                continue
            if own and fold(os.path.splitext(match.name)[0]) == own:
                continue        # a helper we spawned, not the application
            return match
        time.sleep(poll)
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  Closing
# ─────────────────────────────────────────────────────────────────────────────
def _post_close_windows(pid: int) -> int:
    """Ask each of a process's visible windows to close. Returns how many were
    asked.

    WM_CLOSE is what clicking the X does: the application decides what happens
    next, which is what lets it put up "save your changes?" instead of losing
    them. `psutil.terminate()` on Windows is TerminateProcess — an immediate
    kill with no such chance — so it must never be the first thing tried on a
    desktop application somebody is using.
    """
    if _OS != "Windows":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_CLOSE = 0x0010
        asked = 0

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _maybe_close(hwnd, _lparam):
            nonlocal asked
            if not user32.IsWindowVisible(hwnd):
                return True
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if int(owner.value) == pid:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                asked += 1
            return True

        user32.EnumWindows(WNDENUMPROC(_maybe_close), 0)
        return asked
    except Exception as exc:
        print(f"[appidentity] WM_CLOSE failed for pid {pid}: {exc}")
        return 0


def request_close(match: Match, timeout: float = 6.0) -> tuple[bool, str]:
    """Ask an application to close, politely, and report what actually happened.

    -> (gone, detail). `detail` is a cause in plain words, for the model to
    pass on; it is never a success message.

    POLITE, AND ONLY POLITE
        Windows gets WM_CLOSE — the X button. Everything else gets SIGTERM,
        which desktop applications on macOS and Linux handle as "quit". Both
        give the application its chance to object, and an application that
        objects has a reason: an unsaved document, an upload in flight.

        Nothing here escalates to a kill. Not being able to close something is
        a sentence the user can act on; losing an hour of their work because
        the assistant insisted is not. If they want it killed they can say so,
        and that will be a different, louder tool than this one.

    NEVER THE ASSISTANT
        Guarded here as well as in the caller. This is the function that ends
        processes, so the check belongs where the damage would be done rather
        than only where it is decided.
    """
    # THREE CHECKS, DELIBERATELY OVERLAPPING.
    #
    # The first two ask the ranking and the process-tree map, and both of those
    # are built from readings that can fail. The third asks a question with no
    # moving parts: is this process running the same executable this code is
    # running in? That needs no cache, no scan and no tree walk, so it is the
    # one that still answers correctly when everything else has degraded.
    #
    # Overlapping guards on a function that ENDS PROCESSES is not redundancy to
    # be tidied away later. A false negative here closes the assistant, or the
    # user's editor, and there is no undo for that.
    if match.is_self or match.pid == os.getpid():
        return False, ("that is the assistant's own process, so nothing was "
                       "closed")
    if match.pid in own_pids():
        return False, ("that process belongs to the assistant, so nothing was "
                       "closed")
    try:
        target_exe = psutil.Process(match.pid).exe()
        if target_exe and os.path.normcase(target_exe) in own_exes():
            return False, ("that is running the same program as the assistant "
                           "itself, so nothing was closed")
    except psutil.NoSuchProcess:
        # Gone before we could look at it. That is NOT the same as this
        # function having closed it, and reporting it as closed is how a
        # transient process turned into `status=done closed=python` — a
        # sentence that reads as the self-protection having failed.
        return False, ("that process had already exited before it could be "
                       "closed, so nothing was done to it")
    except Exception:
        # Cannot read the executable. Refuse: an unknown target is not a
        # target, and the cost of being wrong is one sentence rather than a
        # process nobody meant to end.
        return False, ("this process cannot be inspected, so it was left "
                       "alone rather than closed on a guess")
    if not _PSUTIL:
        return False, ("psutil is not installed, so applications cannot be "
                       "found or closed - run: pip install psutil")

    asked = _post_close_windows(match.pid)
    if asked == 0:
        try:
            psutil.Process(match.pid).terminate()
        except psutil.NoSuchProcess:
            return True, "it had already exited"
        except psutil.AccessDenied:
            return False, ("the system refused - it is running with higher "
                           "privileges than the assistant")
        except Exception as exc:
            return False, f"the close request failed ({exc})"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(match.pid):
            return True, ""
        time.sleep(0.15)

    # Still there. This is the `unverified` case and it usually means a dialog
    # is waiting on screen, which is worth saying because the user can answer
    # it — telling them "could not close" and nothing else sends them looking
    # for a fault that is really a prompt.
    return False, ("it is still running after being asked to close - it may be "
                   "showing a dialog, such as asking whether to save changes")


def _hwnds_for(pid: int) -> list[int]:
    """Visible top-level window handles belonging to one process (Windows)."""
    if _OS != "Windows":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                         wintypes.LPARAM)

        def _collect(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if int(owner.value) == pid:
                    found.append(int(hwnd))
            return True

        user32.EnumWindows(WNDENUMPROC(_collect), 0)
        return found
    except Exception as exc:
        print(f"[appidentity] window handles unavailable: {exc}")
        return []


def focus(match: "Match") -> bool:
    """Bring an already-running application to the front. -> did it work.

    WHY OPENING SOMETHING THAT IS ALREADY OPEN IS NOT A NO-OP
        "Open WhatsApp" when WhatsApp is running behind three other windows is
        still a request to SEE WhatsApp. Reporting "it was already open" and
        doing nothing is technically true and completely useless: nothing
        changes on screen, and from the user's chair the assistant did not open
        anything. That is exactly how this read when it shipped.

        So the running case raises the window instead of reporting at it. This
        is also what the operating system itself does when you click a taskbar
        icon for a running app, which is the behaviour people already expect.

    A process with no visible window — a background service, a tray-only app —
    returns False rather than pretending, and the caller falls through to
    launching it, which is what brings a tray app back.
    """
    if _OS == "Windows":
        hwnds = _hwnds_for(match.pid)
        if not hwnds:
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            SW_RESTORE = 9

            # WINDOWS REFUSES TO LET A BACKGROUND PROCESS STEAL FOCUS.
            #
            # SetForegroundWindow only succeeds when the calling process
            # already owns the foreground — a rule that exists so a background
            # program cannot yank the screen away mid-sentence. It is also
            # exactly our situation: the user says "open WhatsApp" while
            # working in something else, so the assistant is NOT in front and
            # the call is refused. Seen directly in testing — the same code
            # succeeded twelve times in a row with the HUD in front, and failed
            # every time with another window focused.
            #
            # The documented way through is to borrow the foreground thread's
            # input queue for the length of the call: while two threads share
            # one queue, Windows treats them as the same context and allows the
            # change. Attach, raise, detach.
            def _raise(hwnd) -> bool:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                if user32.SetForegroundWindow(hwnd):
                    return True

                fore = user32.GetForegroundWindow()
                if not fore:
                    return False
                their = user32.GetWindowThreadProcessId(fore, None)
                ours = kernel32.GetCurrentThreadId()
                if their == ours:
                    return False
                if not user32.AttachThreadInput(ours, their, True):
                    return False
                try:
                    user32.BringWindowToTop(hwnd)
                    return bool(user32.SetForegroundWindow(hwnd))
                finally:
                    user32.AttachThreadInput(ours, their, False)

            for hwnd in hwnds:
                if _raise(hwnd):
                    return True
            return False
        except Exception as exc:
            print(f"[appidentity] focus failed: {exc}")
            return False

    if _OS == "Darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set frontmost of the '
                 f'first process whose unix id is {match.pid} to true'],
                capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    # Linux. There is no single API here — X11 window management is whatever
    # the user installed — so the two tools nearly every desktop ships are
    # tried in turn, and neither being present is a normal minimal-desktop
    # state rather than an error. When both are missing this returns False and
    # the caller launches the application instead, which on most desktops
    # raises the existing window anyway.
    #
    # `xdotool` first because it can select by PID, which is exactly the
    # question being asked. `wmctrl` has no PID selector, so it matches on the
    # window title — looser, and only used when xdotool is absent.
    try:
        r = subprocess.run(
            ["xdotool", "search", "--pid", str(match.pid), "windowactivate"],
            capture_output=True, timeout=5)
        if r.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    except Exception:
        pass

    for title in (match.titles or [match.label]):
        if not title:
            continue
        try:
            r = subprocess.run(["wmctrl", "-a", title],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            return False        # not installed; no point trying other titles
        except Exception:
            continue
    return False
