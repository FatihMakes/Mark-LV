"""
What the coding agent knows about ANY project before it writes, and how it
checks what it wrote afterwards.

WHY THIS EXISTS
    A model writing code was given the files and nothing else. It did not know
    how the project runs — which thread code executes on, what the project
    already offers, how its parts are wired — so it wrote code that was valid
    and wrong, and a syntax check passed all of it. Fixing each failure as it
    was reported only moved the problem to the next one.

    People hand the agent any folder — a Flask app, a Node tool, a game, this
    assistant itself — so nothing here may assume which. It works in layers:

    STACK     What the folder is: languages, frameworks, entry points, its own
              virtual environment, its own test command. Read from the files.

    GUIDE     The project's own rules (AGENTS.md / CLAUDE.md / CONTRIBUTING.md)
              when it has them, plus the stack facts, plus whatever a PROFILE
              adds for a project it recognises. (A project with no rules file
              gets a brief written by the model instead — see dev_agent.)

    CHECKS    After writing, cheapest first:
                * syntax for every format that can be checked without running
                  anything — Python, JSON, TOML, YAML, JavaScript;
                * importing the changed Python modules, in a separate process,
                  ONLY when importing cannot do anything (no top-level code
                  beyond definitions);
                * the project's OWN test suite, before and after, so only
                  failures the change introduced count;
                * profile checks — for this app: its threading rules, and each
                  changed plugin called the way the app calls it.

    PROFILES  Knowledge about one kind of project that cannot be read from
              files alone. There is one, for JARVIS itself, because people
              improve the assistant with the assistant; adding another means
              adding a class to _PROFILES.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

_GUIDE_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md",
                ".cursorrules", ".github/copilot-instructions.md")
_GUIDE_CHARS = 40_000
_HARNESS = Path(__file__).with_name("_smoke_harness.py")
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
              "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
              ".idea", ".vscode", ".next", "target", ".jarvis_backup"}
_TEST_TIMEOUT = 180


# ─────────────────────────────────────────────────────────────────────────────
#  Protected files — never read into a prompt, never changed
# ─────────────────────────────────────────────────────────────────────────────
# Learned from a real job: asked to add a plugin, the model also "edited"
# config/api_keys.json to switch the plugin on — rewrote the whole file from
# memory, dropped the API keys and half the settings, and the app asked for its
# key again on the next start. Worse, that file had been read into the project
# context, so the keys in it went to the model with every request.
#
# So two kinds of file are out of reach, decided here and not left to a prompt:
#   * anything that looks like a secret, by name;
#   * anything the project's .gitignore excludes — local settings, personal
#     data, logs and build output are exactly what a repository keeps out.
# Names that mean "secrets" only on DATA files — source code called
# api_keys.py or token_utils.py is code a project may well need changed.
_SECRET_RE = re.compile(
    r"(^|/)(\.env(\..*)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|.*\.(pem|key|p12|pfx|keystore|jks)|"
    r".*(secret|credential|password|passwd|token|api[_-]?keys?).*"
    r"\.(json|ya?ml|toml|ini|cfg|conf|txt|env|properties|xml))$",
    re.IGNORECASE)


def _ignore_patterns(folder: Path) -> list[str]:
    try:
        lines = (folder / ".gitignore").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue                      # negations ignored: protects more, never less
        line = re.split(r"\s+#", line, maxsplit=1)[0].strip()   # "path   # note"
        if line:
            out.append(line)
    return out


def _ignored(rel: str, patterns: list[str]) -> bool:
    from fnmatch import fnmatch
    parts = rel.split("/")
    for pat in patterns:
        is_dir = pat.endswith("/")
        pat = pat.strip("/")
        anchored = "/" in pat
        pat = pat.replace("**/", "")
        if anchored:
            # Matches the path itself or any folder it sits in.
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                if fnmatch(prefix, pat) and (not is_dir or i < len(parts)):
                    return True
        else:
            for i, seg in enumerate(parts):
                if fnmatch(seg, pat) and (not is_dir or i < len(parts) - 1):
                    return True
    return False


class Protection:
    """Which files of one project are off limits, and why."""

    def __init__(self, folder: Path):
        self.patterns = _ignore_patterns(folder)

    def reason(self, rel: str) -> str:
        rel = rel.replace("\\", "/").lstrip("/")
        if _SECRET_RE.search(rel):
            return "it looks like a file of secrets (keys, tokens, passwords)"
        if rel != ".gitignore" and _ignored(rel, self.patterns):
            return "the project's .gitignore keeps it out of the code (local settings, data or output)"
        return ""


# ─────────────────────────────────────────────────────────────────────────────
#  Edits that silently lose data
# ─────────────────────────────────────────────────────────────────────────────
def dropped_data(rel: str, old: str, new: str) -> str:
    """What an edit of a data file throws away, or ''.

    A model that "adds one setting" to a JSON file writes the whole file again
    from what it remembers, and anything it did not remember is gone. Removing
    a key is rarely what was asked, and never something to do silently."""
    ext = Path(rel).suffix.lower()

    def keys(text):
        if ext == ".json":
            data = json.loads(text)
        elif ext == ".toml":
            import tomllib
            data = tomllib.loads(text)
        elif ext in (".yml", ".yaml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            return None
        if not isinstance(data, dict):
            return None
        out = set()
        for k, v in data.items():
            out.add(str(k))
            if isinstance(v, dict):
                out.update(f"{k}.{kk}" for kk in v)
        return out
    try:
        before, after = keys(old), keys(new)
    except Exception:
        return ""
    if before is None or after is None:
        return ""
    lost = sorted(before - after)
    return ", ".join(lost[:12]) if lost else ""


# ─────────────────────────────────────────────────────────────────────────────
#  Stack — what the folder is
# ─────────────────────────────────────────────────────────────────────────────
def _walk(folder: Path, limit: int = 20_000):
    n = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            yield Path(root) / f
            n += 1
            if n >= limit:
                return


def _project_python(folder: Path) -> str:
    """The project's own interpreter when it has a virtual environment —
    its dependencies live there, not in the one running this assistant."""
    for venv in (".venv", "venv", "env"):
        for exe in (Path(venv) / "Scripts" / "python.exe", Path(venv) / "bin" / "python"):
            if (folder / exe).is_file():
                return str(folder / exe)
    return sys.executable


def _has_module(python: str, module: str) -> bool:
    try:
        r = subprocess.run([python, "-c", f"import {module}"], capture_output=True,
                           timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def detect_stack(folder: Path) -> dict:
    """Languages, frameworks, entry points and how to test — read, not guessed."""
    exts: dict[str, int] = {}
    tests_py = False
    for p in _walk(folder):
        exts[p.suffix.lower()] = exts.get(p.suffix.lower(), 0) + 1
        name = p.name.lower()
        if p.suffix == ".py" and (name.startswith("test_") or name.endswith("_test.py")):
            tests_py = True
    langs = {".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript",
             ".tsx": "TypeScript", ".jsx": "JavaScript", ".java": "Java", ".kt": "Kotlin",
             ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby", ".php": "PHP",
             ".cpp": "C++", ".c": "C", ".swift": "Swift", ".dart": "Dart", ".lua": "Lua",
             ".html": "HTML", ".css": "CSS", ".gd": "GDScript"}
    counts: dict[str, int] = {}
    for e, n in exts.items():
        if e in langs:
            counts[langs[e]] = counts.get(langs[e], 0) + n
    languages = [l for l, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    stack: dict = {"languages": languages, "frameworks": [], "entry_points": [],
                   "python": None, "test_cmd": None, "test_name": "", "node": False,
                   "notes": [], "profiles": []}

    # Dependencies name the frameworks better than any guess could.
    deps: list[str] = []
    req = folder / "requirements.txt"
    if req.is_file():
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*([A-Za-z0-9_.\-]+)", line)
            if m and not line.strip().startswith("#"):
                deps.append(m.group(1))
    pyproject = folder / "pyproject.toml"
    if pyproject.is_file():
        deps += re.findall(r'^\s*"([A-Za-z0-9_.\-]+)', pyproject.read_text(
            encoding="utf-8", errors="replace"), re.M)
    pkg = folder / "package.json"
    pkg_data: dict = {}
    if pkg.is_file():
        try:
            pkg_data = json.loads(pkg.read_text(encoding="utf-8"))
        except ValueError:
            pkg_data = {}
        deps += list((pkg_data.get("dependencies") or {}).keys())
        deps += list((pkg_data.get("devDependencies") or {}).keys())
    stack["frameworks"] = sorted(set(deps))[:40]

    for name in ("main.py", "app.py", "manage.py", "__main__.py", "run.py", "server.py",
                 "index.js", "server.js", "app.js", "main.js", "src/main.ts", "src/index.ts",
                 "src/main.py", "main.go", "src/main.rs", "Program.cs"):
        if (folder / name).is_file():
            stack["entry_points"].append(name)
    if pkg_data.get("main"):
        stack["entry_points"].append(str(pkg_data["main"]))

    # Python: the project's interpreter and its tests.
    if "Python" in languages:
        py = _project_python(folder)
        stack["python"] = py
        if py != sys.executable:
            stack["notes"].append(f"Uses its own virtual environment ({Path(py).parent.parent.name}).")
        if tests_py or (folder / "tests").is_dir():
            if _has_module(py, "pytest"):
                stack["test_cmd"] = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
                stack["test_name"] = "pytest"
            else:
                stack["test_cmd"] = [py, "-m", "unittest", "discover", "-q"]
                stack["test_name"] = "unittest"

    # Node: only when it can actually run — a test script that is not npm's
    # placeholder, and dependencies installed.
    if pkg.is_file():
        stack["node"] = bool(shutil.which("node"))
        script = str((pkg_data.get("scripts") or {}).get("test", ""))
        npm = shutil.which("npm")
        if (npm and script and "no test specified" not in script
                and (folder / "node_modules").is_dir() and not stack["test_cmd"]):
            stack["test_cmd"] = [npm, "test", "--silent"]
            stack["test_name"] = "npm test"
        elif script and not (folder / "node_modules").is_dir():
            stack["notes"].append("Has an npm test script, but dependencies are not "
                                  "installed (no node_modules), so it cannot be run.")

    for prof in _PROFILES:
        if prof.detect(folder):
            stack["profiles"].append(prof.name)
    return stack


def describe_stack(stack: dict) -> str:
    """One line for the activity log."""
    bits = [", ".join(stack["languages"][:3]) or "unknown language"]
    if stack["frameworks"]:
        bits.append(", ".join(stack["frameworks"][:4]))
    bits.append(f"tests: {stack['test_name']}" if stack["test_cmd"] else "no runnable tests")
    bits += stack["profiles"]
    return " · ".join(bits)


def _facts(stack: dict) -> str:
    lines = ["## What this project is (detected from its files)",
             f"- Languages: {', '.join(stack['languages']) or 'unknown'}"]
    if stack["frameworks"]:
        lines.append(f"- Dependencies: {', '.join(stack['frameworks'])} — use these; "
                     f"do not add new libraries unless the request needs one.")
    if stack["entry_points"]:
        lines.append(f"- Entry points: {', '.join(stack['entry_points'])}")
    if stack["test_cmd"]:
        lines.append(f"- Tests run with {stack['test_name']}; the change must keep them "
                     f"passing, and new behaviour should get a test in the same style.")
    for n in stack["notes"]:
        lines.append(f"- {n}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Guide
# ─────────────────────────────────────────────────────────────────────────────
def rules_file(folder: Path) -> Path | None:
    for name in _GUIDE_FILES:
        if (folder / name).is_file():
            return folder / name
    return None


def project_guide(folder: Path, stack: dict | None = None) -> str:
    """The project's rules, the detected facts, and any profile's additions."""
    stack = stack or detect_stack(folder)
    parts = []
    rf = rules_file(folder)
    if rf is not None:
        try:
            parts.append(f"## The project's own rules ({rf.name})\n\n"
                         + rf.read_text(encoding="utf-8")[:_GUIDE_CHARS])
        except OSError:
            pass
    parts.append(_facts(stack))
    for prof in _PROFILES:
        if prof.name in stack["profiles"]:
            extra = prof.guide(folder)
            if extra:
                parts.append(extra)
    return "\n\n".join(parts)


def fingerprint(folder: Path) -> str:
    """Changes when files are added, removed or resized — the cue to re-learn
    a project, without reading every byte of it."""
    h = hashlib.sha1()
    for p in sorted(_walk(folder, 5000)):
        try:
            h.update(f"{p.relative_to(folder).as_posix()}:{p.stat().st_size}\n".encode())
        except OSError:
            pass
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
#  Universal checks
# ─────────────────────────────────────────────────────────────────────────────
def syntax_problems(folder: Path, written: dict[str, str]) -> dict[str, str]:
    """{path: error} for every format checkable without running project code."""
    out: dict[str, str] = {}
    node = shutil.which("node")
    for rel, text in written.items():
        ext = Path(rel).suffix.lower()
        try:
            if ext == ".py":
                compile(text, rel, "exec")
            elif ext == ".json":
                json.loads(text)
            elif ext == ".toml":
                import tomllib
                tomllib.loads(text)
            elif ext in (".yml", ".yaml"):
                try:
                    import yaml
                except ImportError:
                    continue
                list(yaml.safe_load_all(text))
            elif ext in (".js", ".mjs", ".cjs") and node:
                r = subprocess.run([node, "--check", str(folder / rel)],
                                   capture_output=True, text=True, timeout=30,
                                   encoding="utf-8", errors="replace")
                if r.returncode != 0:
                    out[rel] = "JavaScript syntax error: " + (r.stderr or r.stdout).strip()[-600:]
        except SyntaxError as e:
            out[rel] = f"{type(e).__name__}: {e.msg} (line {e.lineno})"
        except Exception as e:                      # JSON, TOML and YAML errors
            out[rel] = f"{type(e).__name__}: {str(e)[:300]}"
    return out


_SAFE_TOP = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
             ast.ClassDef, ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Pass)


def _import_is_harmless(text: str) -> bool:
    """True when importing the module only defines things.

    Importing runs every top-level statement. A script with no `if __name__`
    guard would start its bot, send its email or open its window just by being
    imported — so a module is imported only when its top level is definitions,
    assignments, guarded blocks and a docstring."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    def ok(nodes) -> bool:
        for n in nodes:
            if isinstance(n, _SAFE_TOP):
                continue
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
                continue                                     # docstring
            if isinstance(n, ast.If):
                if "__name__" in ast.unparse(n.test):
                    continue                                 # the main guard
                if ok(n.body) and ok(n.orelse):
                    continue
                return False
            if isinstance(n, ast.Try):
                if ok(n.body) and all(ok(h.body) for h in n.handlers) and ok(n.orelse) \
                        and ok(n.finalbody):
                    continue
                return False
            return False
        return True
    return ok(tree.body)


def _module_name(rel: str) -> str:
    return rel[:-3].replace("/", ".").removesuffix(".__init__")


def _missing_module(err: str) -> str:
    m = re.search(r"No module named '([^']+)'", err or "")
    return m.group(1).split(".")[0] if m else ""


def runtime_problems(folder: Path, written: dict[str, str], originals: dict[str, str],
                     stack: dict, log=None, timeout: float = 90.0) -> dict[str, str]:
    """{path: problem} from importing the changed Python modules that are safe
    to import, and from any profile's runtime checks — all in a separate
    process, under the project's own interpreter."""
    py = [r for r in written if r.endswith(".py")]
    if not py or not stack.get("python"):
        return {}
    imports, skipped = [], []
    plugin_runs: list[str] = []
    for prof in _PROFILES:
        if prof.name in stack["profiles"]:
            plugin_runs += prof.runnable(written, log)
    for rel in py:
        if rel in plugin_runs or Path(rel).name.startswith("test"):
            continue
        if _import_is_harmless(written[rel]):
            imports.append(_module_name(rel))
        else:
            skipped.append(rel)
    if skipped and log:
        log("Not importing " + ", ".join(skipped[:5]) + " — it runs code when "
            "loaded, so it was checked by reading only.")
    if not imports and not plugin_runs:
        return {}

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8",
               OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    limit = min(timeout, 30 + 10 * len(plugin_runs))
    try:
        r = subprocess.run(
            [stack["python"], str(_HARNESS), str(folder),
             json.dumps({"imports": imports, "plugins": plugin_runs,
                         "profile": "jarvis" if "jarvis" in stack["profiles"] else ""})],
            cwd=str(folder), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=limit)
    except subprocess.TimeoutExpired as e:
        return _froze(e, plugin_runs or py[:1], limit)
    raw = r.stdout.rpartition("@@REPORT@@")[2].strip()
    try:
        report = json.loads(raw)
    except ValueError:
        tail = (r.stderr or r.stdout or "").strip()[-800:]
        return {(plugin_runs or py)[0]: f"The test process crashed before reporting:\n{tail}"}

    out: dict[str, str] = {}
    by_module = {_module_name(x): x for x in py}
    for mod, err in (report.get("imports") or {}).items():
        if not err:
            continue
        rel = by_module.get(mod, mod)
        missing = _missing_module(err)
        local = bool(missing) and any(p.stem == missing for p in folder.iterdir())
        if missing and not local:
            # A package this machine lacks. If the file imported it before the
            # change, that is the environment, not the change — say nothing.
            before = originals.get(rel, "")
            if re.search(rf"^\s*(import|from)\s+{re.escape(missing)}\b", before, re.M):
                continue
            out[rel] = (f"It imports '{missing}', which is not installed for this "
                        f"project. Use a library the project already depends on, "
                        f"or the standard library.")
        else:
            out[rel] = f"Importing it fails:\n{err}"
    for prof in _PROFILES:
        if prof.name in stack["profiles"]:
            for rel, p in prof.read_report(report).items():
                out[rel] = (out.get(rel, "") + " " + p).strip()
    return out


def _froze(exc: subprocess.TimeoutExpired, candidates: list[str], limit: float) -> dict:
    """What a test process that had to be killed was doing, from the progress
    lines it wrote before it stopped."""
    err = exc.stderr or ""
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    running, qt = "", []
    for line in err.splitlines():
        if line.startswith("@@RUN@@ "):
            running = line[8:].strip()
        elif line.startswith("@@QT@@ "):
            qt.append(line[7:].strip())
    if running:
        msg = (f"Calling run() FROZE the whole process (killed after {limit:.0f}s), "
               f"called from a worker thread as the app calls it.")
        if qt:
            msg += " Qt reported: " + " | ".join(qt[:3]) + "."
        if any("thread" in q.lower() or "parent" in q.lower() for q in qt) or not qt:
            msg += (" This is what building Qt widgets off the Qt thread does. Remove "
                    "every PyQt6 widget from this file and use the thread-safe player "
                    "methods (AGENTS.md).")
        return {running: msg}
    return {rel: f"Loading it hung for {limit:.0f}s — something runs at import time "
                 f"and never returns." for rel in candidates}


# ─────────────────────────────────────────────────────────────────────────────
#  The project's own tests
# ─────────────────────────────────────────────────────────────────────────────
_FAIL_RE = (re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M),               # pytest -q
            re.compile(r"^(?:FAIL|ERROR): (\S+ \([^)]+\))", re.M))       # unittest


def run_tests(folder: Path, stack: dict) -> dict | None:
    """{"ok", "failed": [ids], "output"} from the project's own test command,
    or None when it has none that can run."""
    cmd = stack.get("test_cmd")
    if not cmd:
        return None
    env = dict(os.environ, CI="1", PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, cwd=str(folder), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "failed": ["(the test run timed out)"],
                "output": f"Tests did not finish within {_TEST_TIMEOUT}s."}
    except OSError as e:
        return None if "not found" in str(e).lower() else \
            {"ok": False, "failed": ["(could not start)"], "output": str(e)}
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    failed = sorted({m for rx in _FAIL_RE for m in rx.findall(output)})
    # "no tests ran" (pytest 5, unittest "Ran 0 tests") is not a failure.
    if r.returncode == 5 or re.search(r"Ran 0 tests", output):
        return {"ok": True, "failed": [], "output": output[-3000:]}
    return {"ok": r.returncode == 0, "failed": failed, "output": output[-3000:]}


def test_regressions(before: dict | None, after: dict | None) -> str:
    """What the change broke, judged against how the tests stood before it."""
    if after is None or after["ok"]:
        return ""
    if before is not None and not before["ok"]:
        new = sorted(set(after["failed"]) - set(before["failed"]))
        if not new:
            return ""                     # it was failing the same way already
        return ("These tests passed before the change and fail now: "
                + ", ".join(new[:10]) + "\n\n" + after["output"][-2000:])
    return "The project's tests fail after the change:\n" + after["output"][-2000:]


# ─────────────────────────────────────────────────────────────────────────────
#  Profiles
# ─────────────────────────────────────────────────────────────────────────────
class _JarvisProfile:
    """This assistant, improved by itself. Its rules cannot be read from the
    files alone: tools run on worker threads, Qt widgets may only be made on
    the main thread, and the screen is reached through thread-safe `player`
    methods. Learned the hard way — see AGENTS.md."""

    name = "jarvis"
    _WIDGETS = ("PyQt6.QtWidgets", "PyQt6.QtGui")
    _TOOL_DIRS = ("plugins/", "actions/")
    # run() is only called when nothing in the plugin can act on the world:
    # smoke-testing "send a message" with made-up arguments would send it.
    _SIDE_EFFECTS = re.compile(
        r"subprocess|os\.system|os\.remove|os\.unlink|os\.rmdir|shutil\.(rmtree|move)|"
        r"\.unlink\(|send_message|smtplib|requests\.(post|put|delete)|"
        r"httpx\.(post|put|delete)|webbrowser|pyautogui|keyboard\.|mouse\.|win32api|"
        r"ctypes\.windll|\.write_text\(|open\([^)]*['\"][wa]")

    def detect(self, folder: Path) -> bool:
        return (folder / "core" / "plugin_loader.py").is_file() and (folder / "ui.py").is_file()

    def guide(self, folder: Path) -> str:
        api = self._player_api(folder / "ui.py")
        return ("## Thread-safe `player` methods (generated from ui.py)\n\n```\n"
                + api + "\n```") if api else ""

    @staticmethod
    def _player_api(ui_file: Path) -> str:
        try:
            tree = ast.parse(ui_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return ""
        lines = []
        for node in tree.body:
            if not (isinstance(node, ast.ClassDef) and node.name == "JarvisUI"):
                continue
            for f in node.body:
                if not isinstance(f, ast.FunctionDef) or f.name.startswith("_"):
                    continue
                doc = ast.get_docstring(f) or ""
                if not doc.lower().startswith("thread-safe") and f.name != "write_log":
                    continue
                args = ast.unparse(f.args).replace("self, ", "").replace("self", "")
                lines.append(f"player.{f.name}({args})")
                for d in (doc or "Thread-safe: append a line to the activity log.").splitlines():
                    lines.append(f"    {d}")
        return "\n".join(lines)

    def static(self, folder: Path, written: dict[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}

        def add(rel, msg):
            out[rel] = (out.get(rel, "") + " " + msg).strip()

        for rel, text in written.items():
            if not rel.endswith(".py"):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            is_tool = rel.startswith(self._TOOL_DIRS)
            for node in ast.walk(tree):
                if is_tool and isinstance(node, (ast.Import, ast.ImportFrom)):
                    mod = (node.module or "") if isinstance(node, ast.ImportFrom) else \
                        ",".join(a.name for a in node.names)
                    if any(w in mod for w in self._WIDGETS):
                        add(rel, "It imports PyQt6 widgets, but files in plugins/ and "
                                 "actions/ run on worker threads, where creating a widget "
                                 "fails with 'Cannot set parent, new parent is in a "
                                 "different thread' and nothing appears. Remove every "
                                 "PyQt6 import and show things through the thread-safe "
                                 "player methods instead (for a quiz: player.show_quiz). "
                                 "See AGENTS.md.")
                        break
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "QApplication" and rel != "ui.py"):
                    add(rel, "It creates a QApplication; the app already has exactly one "
                             "(in ui.py) and a second breaks the window. Remove it.")
            if rel.startswith("plugins/") and not Path(rel).name.startswith("_"):
                if not re.search(r"^PLUGIN\s*=", text, re.M) or \
                        not re.search(r"^def run\(", text, re.M):
                    add(rel, "A plugin needs a module-level PLUGIN dict and a module-level "
                             "def run(parameters, player=None, session_memory=None) -> str. "
                             "See plugins/_template.py.")
        return out

    def runnable(self, written: dict[str, str], log=None) -> list[str]:
        """Changed plugins safe to call with made-up arguments."""
        run, skipped = [], []
        for rel, text in written.items():
            if rel.startswith("plugins/") and rel.endswith(".py") \
                    and not Path(rel).name.startswith("_"):
                (skipped if self._SIDE_EFFECTS.search(text) else run).append(rel)
        if skipped and log:
            log("Not running " + ", ".join(skipped) + " — it can act on your computer, "
                "so it was only loaded and checked, not called.")
        return run

    @staticmethod
    def read_report(report: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel, res in (report.get("plugins") or {}).items():
            problems = []
            if res.get("error"):
                problems.append(res["error"])
            threaded = [w for w in res.get("warnings") or []
                        if "thread" in w.lower() or "parent" in w.lower()]
            if threaded:
                problems.append(
                    "Called from a worker thread, as the app calls it, Qt reported: "
                    + " | ".join(threaded[:3]) + " — it builds widgets off the Qt thread, "
                    "so nothing appears. Use the thread-safe player methods (AGENTS.md).")
            if "show_quiz:BAD_QUESTIONS" in (res.get("calls") or []):
                problems.append("player.show_quiz was given questions in the wrong shape; "
                                "it needs a non-empty list of {'question','options','answer'} dicts.")
            if not res.get("error") and not isinstance(res.get("returned"), str):
                problems.append(f"run() returned {type(res.get('returned')).__name__}, "
                                f"not the short string the assistant speaks.")
            if problems:
                params = json.dumps(res.get("params") or {})
                out[rel] = (f"Test: run({params}) on a worker thread with every model call "
                            f"returning nothing. " + " ".join(problems))
        return out


_PROFILES = [_JarvisProfile()]


def static_problems(folder: Path, written: dict[str, str], stack: dict) -> dict[str, str]:
    """Syntax for every format, plus every matching profile's rules."""
    out = syntax_problems(folder, written)
    for prof in _PROFILES:
        if prof.name in stack["profiles"]:
            for rel, p in prof.static(folder, written).items():
                out[rel] = (out.get(rel, "") + " " + p).strip()
    return out
