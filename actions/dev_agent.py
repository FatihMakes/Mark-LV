import subprocess
import sys
import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import gemini

MODEL_PLANNER    = gemini.SMART
MODEL_WRITER     = gemini.SMART

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _coding_provider() -> str:
    """The service chosen under ENGINE & PROVIDERS → CODING, or ''."""
    try:
        from memory.config_manager import get_coding_provider
        return get_coding_provider()
    except Exception:
        return ""


def _get_model(model_name: str = gemini.SMART):
    """Planning and writing whole files — the reasoning tier, and a long
    deadline because the answer is a source file rather than a sentence.

    A coding model chosen in the panel is tried first, with the Gemini ladder
    behind it. Its deadline is longer still: a reasoning model thinks before
    it writes, and a whole file from one routinely takes past a minute."""
    provider = _coding_provider()

    class _W:
        def generate_content(self, contents):
            resp = gemini.call(contents, tier=model_name,
                               timeout_ms=180000 if provider else 60000,
                               provider=provider)
            if resp is None:
                raise RuntimeError("every model on the ladder failed")
            return resp

    return _W()


def _strip_fences(text: str) -> str:
    # A reasoning model's thinking, when a server passes it through inline.
    # Saved to disk it is a syntax error on line one of every file.
    text = re.sub(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>", "", text or "")
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:

    low = output.lower()

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


def _has_error(output: str, run_command: str) -> bool:
    
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    error_type = _classify_error(output)
    return error_type != "none"

class RateLimitError(Exception):
    pass


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        full_path = project_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    if not dependencies:
        return "No external dependencies."

    to_install = []
    for dep in dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    if not to_install:
        return f"All dependencies already installed: {', '.join(dependencies)}"

    print(f"[DevAgent] 📦 Installing: {to_install}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(project_dir)
        )
        if result.returncode == 0:
            return f"Installed: {', '.join(to_install)}"
        return f"Install warning (non-fatal): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Dependency install timed out (non-fatal)."
    except Exception as e:
        return f"Install error (non-fatal): {e}"

def _open_vscode(project_dir: Path) -> bool:
    vscode_candidates = [
        "code",
        rf"C:\Users\{Path.home().name}\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
    ]
    for cmd in vscode_candidates:
        try:
            subprocess.Popen(
                [cmd, str(project_dir)],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.5)
            print(f"[DevAgent] 💻 VSCode opened: {project_dir}")
            return True
        except Exception:
            continue
    return False

def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    print(f"[DevAgent] 🚀 Running: {run_command}")
    try:
        parts = run_command.split()
        if parts[0].lower() == "python":
            parts[0] = sys.executable

        result = subprocess.run(
            parts,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(project_dir)
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        combined_parts = []
        if stdout:
            combined_parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            combined_parts.append(f"STDERR:\n{stderr}")

        return "\n\n".join(combined_parts) if combined_parts else "Ran with no output."

    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s — long-running app (server/GUI) is likely working."
    except FileNotFoundError as e:
        return f"Command not found: {e}"
    except Exception as e:
        return f"Run error: {e}"

def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    """If there is a ModuleNotFoundError, tries to auto-install the missing package."""
    pattern = re.compile(
        r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE
    )
    match = pattern.search(error_output)
    if not match:
        return False

    pkg = match.group(1).replace("_", "-").split(".")[0]
    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False

def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            full_path = project_dir / fix_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:

    def log(msg: str):
        _log(player, msg)

    provider = _coding_provider()
    log(f"Coding with {provider.partition('@')[2] or provider}, Gemini as backup."
        if provider else "Coding with the background-request models.")
    _update("planning")
    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir  = PROJECTS_DIR / proj_name
    project_dir.mkdir(parents=True, exist_ok=True)

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")
    with _state_lock:
        if _JOB:
            _JOB["folder"] = proj_name
            _JOB["path"] = str(project_dir)
            _JOB["summary"] = (f"New {language} project '{proj_name}': "
                               f"{len(files)} files, run with `{run_command}`.")
            _JOB["files"] = {f.get("path", ""): {"action": "create", "state": "waiting",
                                                 "what": str(f.get("description", ""))}
                             for f in files if f.get("path")}
    _update("writing")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        _update(file=file_path, file_state="writing")
        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                _update(file=file_path, file_state="done")
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    _update(file=file_path, file_state="failed")
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                _update(file=file_path, file_state="failed")
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    if dependencies:
        _update("installing")
        log(f"Installing {', '.join(dependencies)}...")
        install_result = _install_dependencies(dependencies, project_dir)
        log(install_result)

    _open_vscode(project_dir)

    last_output   = ""
    auto_installs = 0  

    _update("testing")
    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        last_output = _run_project(run_command, project_dir, timeout)
        log(f"Output preview: {last_output[:150]}")

        if not _has_error(last_output, run_command):
            msg = (
                f"Project '{proj_name}' is working, sir. "
                f"Built in {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to: {project_dir}"
            )
            if speak: speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = _classify_error(last_output)
        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        log(f"Fixing errors (type: {error_type})...")
        try:
            updated = _fix_files(
                error_output=last_output,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
            )
            file_codes.update(updated)
            for fixed in updated:
                _update(file=fixed, file_state="done")
                log(f"Fixed {fixed}")
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast error:\n{last_output[:600]}"


# ─────────────────────────────────────────────────────────────────────────────
#  Improving a folder that already exists
# ─────────────────────────────────────────────────────────────────────────────
# Building from nothing is the easy half. What people actually have is a folder
# that half works, and what they want is "add a settings page to this" or "fix
# whatever is wrong with it" — said out loud, with the folder dropped into the
# upload box. This is that.
#
# WHAT IT WILL NOT DO
#   * Write outside the folder. Every path the model names is resolved and
#     checked against the folder before anything touches the disk.
#   * Delete anything. Removing a user's file on a model's say-so is the one
#     mistake that is not cheap to undo, so the plan cannot ask for it.
#   * Run the project. A folder somebody hands over may be a server, a GUI or a
#     script that sends email. What IS run, in a separate process: changed
#     modules are imported, and in a JARVIS project each changed plugin is
#     called the way the app calls it — unless its code can act on the world
#     (send, delete, launch), in which case it is only loaded and validated.
#     See core/code_checks.py.
#
# WHAT IT READS FIRST
#   The project's own rules (AGENTS.md, CLAUDE.md or CONTRIBUTING.md) and, for
#   JARVIS itself, the thread-safe `player` methods read out of ui.py. Without
#   them a model writes code that is valid and wrong for this app — widgets
#   built off the Qt thread, a separate window instead of the quiz board.
#
# WHAT MAKES IT SAFE TO SAY YES TO
#   Every file is copied to .jarvis_backup/<time>/ inside the folder before it
#   is changed, and the whole job is one entry on the shared undo stack: "undo"
#   puts every file back and removes the ones it created.

_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
              "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
              ".idea", ".vscode", ".next", "target", ".jarvis_backup"}
_BACKUP_DIR     = ".jarvis_backup"
_MAX_FILE_CHARS = 120_000     # bigger than this is generated or data, not code
_CONTEXT_CHARS  = 350_000     # file text sent to plan; MiniMax M3.1 takes 512K tokens
_MAX_TREE       = 1500        # paths listed for the model
_MAX_CHANGES    = 12          # files one request may touch
_FIX_ROUNDS     = 2
_TEST_ROUNDS    = 2           # test → fix → test again

# One folder job at a time. Two at once would write the same files from two
# plans and leave a backup folder that restores neither.
_job_lock = threading.Lock()
_job_name = ""

# The current (or last) folder job, as the panel draws it and as the assistant
# reports it when asked "what is it doing?" or "what did it do?". Replaced as a
# whole when a job starts; mutated only by the job's own thread, and copied
# under `_state_lock` by anybody reading it.
_state_lock = threading.Lock()
_JOB: dict = {}
_player_ref = None


_PHASES = {"improve": ["reading", "planning", "writing", "checking", "testing"],
           "build": ["planning", "writing", "installing", "testing"]}


def _new_job(folder: Path, model: str, instruction: str, player,
             kind: str = "improve") -> None:
    global _JOB, _player_ref
    with _state_lock:
        _JOB = {"folder": folder.name, "path": str(folder), "model": model,
                "kind": kind, "phases": _PHASES[kind],
                "request": instruction, "status": "starting",
                "started": time.time(), "finished": None, "summary": "",
                "files": {}, "steps": [], "result": ""}
    _player_ref = player
    _publish()


def _job_snapshot() -> dict:
    with _state_lock:
        snap = json.loads(json.dumps(_JOB)) if _JOB else {}
    return snap


def _publish() -> None:
    """Hand the job's current state to the interface, if it can draw one."""
    show = getattr(_player_ref, "show_code_job", None)
    if show is None:
        return
    try:
        show(_job_snapshot())
    except Exception:
        pass


def _update(status: str | None = None, file: str | None = None,
            file_state: str | None = None, **fields) -> None:
    with _state_lock:
        if not _JOB:
            return
        if status:
            _JOB["status"] = status
        if file and file_state:
            _JOB["files"].setdefault(file, {})["state"] = file_state
        _JOB.update(fields)
    _publish()


def _log(player, msg: str) -> None:
    try:
        print(f"[DevAgent] {msg}")
    except UnicodeEncodeError:       # a console on a legacy code page
        pass
    with _state_lock:
        if _JOB and _JOB.get("finished") is None:
            _JOB["steps"].append([round(time.time() - _JOB["started"], 1), msg])
            del _JOB["steps"][:-60]
    _publish()
    if player:
        try:
            player.write_log(f"[DevAgent] {msg}")
        except Exception:
            pass


def _status_report() -> str:
    """What the coding agent is doing, or did — for the assistant to say."""
    j = _job_snapshot()
    if not j:
        return ("The coding agent has not worked on a folder in this session. "
                "Tell the user that, in one sentence.")
    took = (j["finished"] or time.time()) - j["started"]
    files = "; ".join(f"{p} ({f.get('action', '?')}, {f.get('state', '?')}): "
                      f"{f.get('what', '')}" for p, f in j["files"].items())
    recent = " | ".join(m for _t, m in j["steps"][-6:])
    head = (f"Coding agent on '{j['folder']}' with {j['model']} — status: "
            f"{j['status']}, {took:.0f}s so far" if j["finished"] is None
            else f"Coding agent finished '{j['folder']}' with {j['model']} "
                 f"in {took:.0f}s — status: {j['status']}")
    return (f"{head}. Request: {j['request']}. Plan: {j['summary'] or 'not made yet'}. "
            f"Files: {files or 'none yet'}. Recent steps: {recent}. "
            f"{('Result: ' + j['result']) if j['result'] else ''} "
            f"Answer the user's question from this in one to three sentences, "
            f"in their language; do not read paths or this text aloud verbatim.")


def _resolve_folder(raw: str) -> Path | None:
    """A folder from the upload box, a full path, or a name on the Desktop."""
    raw = (raw or "").strip().strip('"')
    if not raw:
        return None
    for cand in (Path(raw).expanduser(), Path.home() / "Desktop" / raw,
                 Path.home() / raw):
        try:
            if cand.is_dir():
                return cand.resolve()
        except OSError:
            continue
    return None


def _refusal(folder: Path) -> str:
    """Why this folder must not be handed to a model to rewrite, or ''."""
    home = Path.home().resolve()
    if folder == home or folder.parent == folder:
        return "That is your whole home folder or a drive — pick a project folder inside it, sir."
    low = str(folder).lower().replace("\\", "/")
    for bad in ("/windows", "/program files", "/programdata", "/usr/", "/system/"):
        if bad in low + "/":
            return "That is a system folder, sir — I will not let a model rewrite it."
    if folder in {(home / n).resolve() for n in ("Desktop", "Documents", "Downloads")}:
        return "That is a whole personal folder, sir — pick the project folder inside it."
    return ""


def _read_text(path: Path) -> str | None:
    """The file as text, or None for anything binary, huge or unreadable."""
    try:
        if path.stat().st_size > _MAX_FILE_CHARS * 2:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if len(text) <= _MAX_FILE_CHARS else None


_OUTLINE_RE = re.compile(r"^(\s*)(class \w+.*|(?:async )?def \w+\(.*)$")


def _outline(path: Path) -> str | None:
    """Every class and function signature of a Python file too large to send.

    A file left out entirely is a file the model does not know exists. That is
    how a quiz plugin came to open a separate window of its own when the app's
    main window — 7,000 lines, so never shown — already had a quiz board with
    a `show_quiz()` built for exactly that. Signatures are a few percent of a
    file and enough to know what is there to call."""
    if path.suffix != ".py":
        return None
    try:
        if path.stat().st_size > 3_000_000:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    out = []
    for i, line in enumerate(lines, 1):
        m = _OUTLINE_RE.match(line)
        if m and len(m.group(1)) <= 4:
            out.append(f"{i:>6}: {line.rstrip()[:160]}")
    return "\n".join(out) or None


def _scan(folder: Path) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """(every path, text of every readable file, outline of every Python file
    too large to read in full), skipping dependencies and build output. Paths
    use forward slashes on every system — they go into a prompt and come back
    out of one, and the model writes them that way."""
    import os
    from core.code_checks import Protection
    protection = Protection(folder)
    tree: list[str] = []
    texts: dict[str, str] = {}
    outlines: dict[str, str] = {}
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            full = Path(root) / name
            rel = full.relative_to(folder).as_posix()
            if protection.reason(rel):
                # Named so the model knows it exists; its contents never go
                # into a prompt — they may be somebody's keys.
                tree.append(f"{rel}   [PROTECTED: never read, never change]")
                continue
            tree.append(rel)
            text = _read_text(full)
            if text is not None:
                texts[rel] = text
            elif (outline := _outline(full)) is not None:
                outlines[rel] = outline
            if len(tree) >= 20_000:
                return tree, texts, outlines
    return tree, texts, outlines


def _context(tree: list[str], texts: dict[str, str], budget: int,
             outlines: dict[str, str] | None = None) -> str:
    """The folder as the model sees it: the full list of paths, then as many
    files as fit — smallest first, so a budget spent on one generated file does
    not crowd out the twenty that hold the actual logic."""
    shown = tree[:_MAX_TREE]
    out = ["FILES IN THE FOLDER:"] + [f"  {p}" for p in shown]
    if len(tree) > len(shown):
        out.append(f"  … and {len(tree) - len(shown)} more")
    used, left_out = 0, []
    for rel in sorted(texts, key=lambda r: len(texts[r])):
        body = texts[rel]
        if used + len(body) > budget:
            left_out.append(rel)
            continue
        out.append(f"\n===== {rel} =====\n{body}")
        used += len(body)
    for rel, outline in (outlines or {}).items():
        if used + len(outline) > budget:
            left_out.append(rel)
            continue
        out.append(f"\n===== {rel} (OUTLINE ONLY — too large to show; these are "
                   f"its classes and functions, with line numbers) =====\n{outline}")
        used += len(outline)
    if left_out:
        out.append("\n(Not shown for length: " + ", ".join(left_out[:40]) + ")")
    return "\n".join(out)


def _json_object(text: str) -> dict:
    """The JSON object in a model's answer, tolerating prose around it —
    with the same decoder plugins get from gemini.parse_json."""
    value = gemini.parse_json(_strip_fences(text))
    if not isinstance(value, dict):
        raise ValueError(f"no JSON object in the reply: {text[:160]!r}")
    return value


def _safe_rel(folder: Path, rel: str) -> str | None:
    """A model-supplied path, as a clean relative path inside the folder, or
    None if it points anywhere else."""
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("~") or ":" in rel:
        return None
    parts = Path(rel).parts
    if ".." in parts or any(p in _SKIP_DIRS for p in parts):
        return None
    try:
        (folder / rel).resolve().relative_to(folder)
    except (ValueError, OSError):
        return None
    return Path(rel).as_posix()


def _plan_changes(folder: Path, instruction: str, context: str) -> dict:
    prompt = f"""You are a senior engineer changing an existing project.

Project folder: {folder.name}
Request: {instruction}

{context}

Decide which files to change or create to fulfil the request. Return ONLY JSON:
{{
  "summary": "one or two sentences, spoken to the user, saying what you will do",
  "changes": [
    {{"path": "relative/path.ext", "action": "edit", "what": "exactly what to change in this file"}},
    {{"path": "new/file.ext", "action": "create", "what": "what this new file contains"}}
  ]
}}

Rules:
- "action" is "edit" for a file listed above, "create" for a new one. Never delete or rename files.
- Files marked PROTECTED (secrets, local settings, personal data) must never be
  changed, and nothing needs them to be: settings are changed by the program at
  run time through its own settings code, not by editing its config files.
- Paths are relative to the project folder, with forward slashes.
- Change only what the request needs — at most {_MAX_CHANGES} files.
- Keep the project's existing style, structure and libraries. Before writing
  anything new, look for something the project already has for the job — an
  existing function, panel or helper — and use it.
- Each file is written separately, so the files can only fit together if the
  plan says how. Order "changes" so that a file comes BEFORE every file that
  imports it, calls it or launches it. In "what", name the exact functions,
  arguments, command-line flags and data keys a file provides or uses, and use
  the same names in every file that refers to them.
- If the request is unclear or impossible, return an empty "changes" list and explain in "summary".

JSON:"""
    resp = _get_model(MODEL_PLANNER).generate_content(prompt)
    return _json_object(resp.text)


_PLACEHOLDER = re.compile(
    r"(rest of (the )?(file|code)|remaining code|unchanged code|code unchanged|"
    r"same as before|existing code here|\.\.\. ?\(?unchanged)", re.IGNORECASE)


def _rejected(rel: str, old: str, new: str) -> str:
    """Why a new version of a file must not be saved, or ''."""
    if _looks_truncated(old, new):
        return "the model returned an incomplete file"
    from core.code_checks import dropped_data
    lost = dropped_data(rel, old, new) if old else ""
    if lost:
        return f"the new version silently drops existing settings ({lost})"
    return ""


def _looks_truncated(old: str, new: str) -> bool:
    """A model that answers with "# rest of the file unchanged" has not written
    the file — saving it would delete everything that comment stands for."""
    if not new.strip():
        return True
    if _PLACEHOLDER.search(new) and not _PLACEHOLDER.search(old or ""):
        return True
    return bool(old) and len(old) > 2000 and len(new) < len(old) * 0.3


_WRITTEN_CHARS = 150_000    # code from earlier files of the same job, per prompt


def _written_block(written: dict[str, str], rel: str) -> str:
    """The files this job has already written, in full — the ones `rel` must
    agree with. The reason this exists: a plugin and the window it launched
    were written in separate calls, each seeing only a one-line description of
    the other, and the plugin passed command-line flags the window did not
    accept. It failed silently on every use."""
    parts, used = [], 0
    for path, text in written.items():
        if path == rel or used + len(text) > _WRITTEN_CHARS:
            continue
        parts.append(f"===== {path} (ALREADY WRITTEN in this change — final) =====\n{text}")
        used += len(text)
    if not parts:
        return ""
    return ("\nFiles already written as part of this same change. They are final:\n"
            "use exactly the names, signatures, command-line flags and data keys\n"
            "they define — do not guess or invent your own.\n\n"
            + "\n".join(parts) + "\n")


def _write_change(change: dict, plan: dict, instruction: str, context: str,
                  current: str, error: str = "",
                  written: dict[str, str] | None = None) -> str:
    rel, action = change["path"], change.get("action", "edit")
    other = "\n".join(f"  - [{c.get('action')}] {c['path']}: {c.get('what', '')}"
                      for c in plan["changes"] if c["path"] != rel)
    fix = (f"\nThe previous version of this file has a problem that must be "
           f"fixed:\n{error}\n") if error else ""
    shown = (f"Current content of {rel}:\n{current}" if current
             else "This file does not exist yet.")
    prompt = f"""You are a senior engineer changing an existing project.

Overall request: {instruction}
This file: {rel} ({'create a new file' if action == 'create' else 'edit the existing file'})
What to do in this file: {change.get('what', '')}
Other files being changed at the same time:
{other or '  (none)'}
{fix}{_written_block(written or {}, rel)}
The rest of the project, for reference:
{context}

{shown}

Rules for the code:
- Anything this file imports, calls or launches from another project file must
  match that file exactly — names, arguments, command-line flags, data keys.
- Never throw away errors from code you start or call: no output sent to
  DEVNULL without a log, no success reported without checking it happened.

Return ONLY the complete new content of {rel} — every line, not just the
changed parts. No explanation, no markdown fences, no placeholders such as
"rest of file unchanged".

Content of {rel}:"""
    resp = _get_model(MODEL_WRITER).generate_content(prompt)
    return _strip_fences(resp.text) + "\n"


def _review_interfaces(instruction: str, written: dict[str, str],
                       texts: dict[str, str], rules: str = "") -> dict[str, str]:
    """{path: problem} for files of this change that do not fit together.

    A syntax check passes two files that are each perfectly valid and cannot
    work with one another. This asks the model to read every file of the
    change side by side — plus the existing files they refer to — and name
    the mismatches: a flag one passes that the other does not accept, a
    function imported under a name that does not exist, a key written as one
    word and read as another."""
    if len(written) < 1:
        return {}
    shown, used = [], 0
    for path, text in written.items():
        shown.append(f"===== {path} (NEW/CHANGED) =====\n{text}")
        used += len(text)
    # Existing files the changed ones mention by module or file name.
    for path, text in texts.items():
        if path in written or used > _WRITTEN_CHARS * 2:
            continue
        stem = Path(path).stem
        if any(stem and (stem in t) for t in written.values()):
            shown.append(f"===== {path} (EXISTING, unchanged) =====\n{text}")
            used += len(text)
    # The project's rules and API list go in too. Without them the reviewer
    # saw a plugin calling player.show_quiz, could not find show_quiz in the
    # files it was shown (ui.py is too large to include), and reported a
    # problem that did not exist — a rewrite of a correct file for nothing.
    known = (f"PROJECT RULES AND THE PROJECT'S EXISTING API — everything listed "
             f"here exists; do not report it as missing:\n{rules}\n\n") if rules else ""
    prompt = f"""You are reviewing a code change before it ships.
Request it implements: {instruction}

{known}{chr(10).join(shown)}

Check ONLY whether these files work TOGETHER: every import resolves to a name
that exists, every call matches the callee's parameters, every command-line
flag passed to another file is one that file's argument parser accepts, every
data key written is the key that is read, every file path points at a file
that exists. Ignore style. Report only what you can point to in the code
above — never a function you merely could not see.

Return ONLY JSON: {{"problems": [{{"path": "file that must change", "problem": "what is wrong and exactly how to fix it"}}]}}
Return {{"problems": []}} when everything fits.

JSON:"""
    resp = _get_model(MODEL_PLANNER).generate_content(prompt)
    out: dict[str, str] = {}
    for item in (_json_object(resp.text).get("problems") or []):
        if isinstance(item, dict) and item.get("path") in written and item.get("problem"):
            out[item["path"]] = (out.get(item["path"], "") + " " + str(item["problem"])).strip()
    return out


def _syntax_error(rel: str, text: str) -> str:
    """'' when the file parses, else the error. Only formats that can be
    checked without running anything."""
    try:
        if rel.endswith(".py"):
            compile(text, rel, "exec")
        elif rel.endswith(".json"):
            json.loads(text)
    except SyntaxError as e:
        return f"{type(e).__name__}: {e.msg} (line {e.lineno})"
    except ValueError as e:
        return f"invalid JSON: {e}"
    return ""


_BRIEF_DIR = BASE_DIR / "config" / "project_briefs"


def _project_brief(folder: Path, context: str, stack: dict, player=None) -> str:
    """How this project works, written by the model once and cached.

    A project without an AGENTS.md still has rules — how its parts connect,
    where new features go, what runs where — they are just not written down.
    Every file the model writes is better for knowing them, so they are worked
    out first, from the whole project at once, and kept until the project's
    files change. Kept in this app's config folder, not in the user's project."""
    from core.code_checks import fingerprint
    key = hashlib.sha1(str(folder).lower().encode()).hexdigest()[:16]
    cache = _BRIEF_DIR / f"{key}.json"
    fp = fingerprint(folder)
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("fingerprint") == fp and saved.get("brief"):
            _log(player, "Using what I learned about this project last time.")
            return saved["brief"]
    except (OSError, ValueError):
        pass
    _log(player, "Studying how this project works…")
    prompt = f"""You are a senior engineer joining this project. Study it and write the
rules another engineer must follow to change it WITHOUT breaking it.

{context}

Write at most 60 lines of Markdown, specific to THIS project — no generic advice:
- Architecture: the main parts, what each does, how they connect and in what
  order things run (startup, threads/processes/event loops, request flow).
- Extension points: how a new feature of the usual kind is added here (a new
  route, component, command, plugin, model, screen…) — which files, which
  registration step, which existing helpers to reuse.
- Runtime constraints that are easy to break: threading, async, global state,
  UI-thread rules, configuration and secrets, file locations.
- Conventions: naming, error handling, logging, testing style.
- Things that already exist and must be reused rather than rebuilt.

Markdown:"""
    try:
        brief = _strip_fences(_get_model(MODEL_PLANNER).generate_content(prompt).text)
    except Exception as e:
        _log(player, f"Could not study the project: {str(e)[:150]}")
        return ""
    brief = "## How this project works (studied from its code)\n\n" + brief.strip()
    try:
        _BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"folder": str(folder), "fingerprint": fp,
                                     "brief": brief}), encoding="utf-8")
    except OSError:
        pass
    return brief


def _refresh_brief(folder: Path) -> None:
    """Mark a cached study as current for the project as it now stands."""
    try:
        from core.code_checks import fingerprint
        key = hashlib.sha1(str(folder).lower().encode()).hexdigest()[:16]
        cache = _BRIEF_DIR / f"{key}.json"
        saved = json.loads(cache.read_text(encoding="utf-8"))
        saved["fingerprint"] = fingerprint(folder)
        cache.write_text(json.dumps(saved), encoding="utf-8")
    except (OSError, ValueError, ImportError):
        pass


def _blame(output: str, written: dict[str, str]) -> list[str]:
    """Which changed files a failing test run points at — the ones its output
    names, or, when it names none, every changed source file."""
    named = [r for r in written if r in output or Path(r).name in output]
    if named:
        return named
    code = [r for r in written if Path(r).suffix not in (".md", ".txt", ".rst")]
    return code[:4]


def _test_and_fix(folder: Path, written: dict[str, str], changes: list,
                  plan: dict, instruction: str, context: str, player,
                  stack: dict | None = None, originals: dict | None = None,
                  baseline: dict | None = None) -> dict:
    """Check the written files — syntax for every format, safe imports, the
    project's own tests against how they stood before, and any profile's
    rules — rewrite whatever fails, repeat. Returns what still fails."""
    try:
        from core.code_checks import (detect_stack, run_tests, runtime_problems,
                                      static_problems, test_regressions)
    except Exception as e:
        _log(player, f"Testing unavailable: {str(e)[:120]}")
        return {}
    stack = stack or detect_stack(folder)
    originals = originals or {}
    _update("testing")
    problems: dict[str, str] = {}
    for round_no in range(_TEST_ROUNDS + 1):
        _log(player, "Testing the changed files…")
        problems = static_problems(folder, written, stack)
        # A file already failing a rule is rewritten anyway; running it first
        # would only spend up to half a minute watching it freeze.
        to_run = {r: t for r, t in written.items() if r not in problems}
        for rel, p in runtime_problems(folder, to_run, originals, stack,
                                       log=lambda m: _log(player, m)).items():
            problems[rel] = (problems.get(rel, "") + " " + p).strip()
        if stack.get("test_cmd") and not problems:
            _log(player, f"Running the project's own tests ({stack['test_name']})…")
            broke = test_regressions(baseline, run_tests(folder, stack))
            if broke:
                for rel in _blame(broke, written):
                    problems[rel] = (problems.get(rel, "") + " " + broke).strip()
            else:
                _log(player, "The project's tests pass as they did before.")
        problems = {r: p for r, p in problems.items() if r in written}
        if not problems:
            _log(player, "All changed files passed their tests.")
            return {}
        for rel, p in problems.items():
            _update(file=rel, file_state="broken")
            _log(player, f"Test failed — {rel}: {p[:220]}")
        if round_no == _TEST_ROUNDS:
            break
        for rel, p in problems.items():
            c = next((x for x in changes if x["path"] == rel), None)
            if c is None:
                continue
            _update(file=rel, file_state="fixing")
            _log(player, f"Fixing {rel}…")
            try:
                new = _write_change(c, plan, instruction, context, written[rel],
                                    error=p, written=written)
            except Exception as e:
                _log(player, f"Fix failed for {rel}: {str(e)[:150]}")
                continue
            why = _rejected(rel, written[rel], new) or (
                "it does not parse" if _syntax_error(rel, new) else "")
            if why:
                _log(player, f"Kept the previous {rel}: {why}.")
                continue
            (folder / rel).write_text(new, encoding="utf-8", newline="")
            written[rel] = new
            _update(file=rel, file_state="done")
    return problems


def _improve_folder(folder: Path, instruction: str, player=None) -> str:
    """Plan, back up, write, check, fix, register undo. Runs to completion and
    returns the summary; the caller decides whether that is on a thread."""
    started = time.monotonic()
    provider = _coding_provider()
    who = provider.partition("@")[2] or provider or "the background-request models"
    _log(player, f"Improving '{folder.name}' with {who}…")

    _update("reading")
    tree, texts, outlines = _scan(folder)
    if not texts:
        return f"I found no readable source files in '{folder.name}', sir."
    _log(player, f"Read {len(texts)} of {len(tree)} files"
                 + (f", outlined {len(outlines)} large ones." if outlines else "."))
    context = _context(tree, texts, _CONTEXT_CHARS, outlines)
    # Understand the project before touching it: what it is, its own rules,
    # and — when it has no rules file — a brief written once and cached.
    stack, baseline = None, None
    try:
        from core.code_checks import (describe_stack, detect_stack, project_guide,
                                      rules_file, run_tests)
        stack = detect_stack(folder)
        _log(player, f"Project: {describe_stack(stack)}")
        guide = project_guide(folder, stack)
        rf = rules_file(folder)
        if rf is not None:
            _log(player, f"Read the project's rules ({rf.name}).")
        else:
            brief = _project_brief(folder, context, stack, player)
            if brief:
                guide = brief + "\n\n" + guide
        if stack.get("test_cmd"):
            # How the tests stand BEFORE the change, so afterwards only
            # failures the change caused are blamed on it.
            _log(player, f"Running the project's tests first ({stack['test_name']})…")
            baseline = run_tests(folder, stack)
            if baseline and not baseline["ok"]:
                _log(player, f"{len(baseline['failed']) or 'Some'} test(s) already "
                             f"fail before any change; those will not be blamed on it.")
    except Exception as e:
        guide = ""
        _log(player, f"Could not study the project: {str(e)[:150]}")
    if guide:
        # In front of everything else, because it decides HOW the code must
        # be written — the files only show what is already there.
        context = ("PROJECT RULES — every change must follow these exactly:\n"
                   f"{guide}\n\n{context}")

    _update("planning")
    _log(player, "Planning the changes…")
    try:
        plan = _plan_changes(folder, instruction, context)
    except Exception as e:
        return f"Planning failed, sir: {str(e)[:200]}"

    from core.code_checks import Protection
    protection = Protection(folder)
    changes = []
    for c in plan.get("changes") or []:
        if not isinstance(c, dict):
            continue
        rel = _safe_rel(folder, c.get("path", ""))
        if rel is None:
            _log(player, f"Skipped a path outside the folder: {c.get('path')!r}")
            continue
        why = protection.reason(rel)
        if why:
            _log(player, f"Refused to touch {rel}: {why}.")
            continue
        if any(x["path"] == rel for x in changes):
            continue
        action = "edit" if (folder / rel).exists() else "create"
        if action == "edit" and rel not in texts:
            _log(player, f"Skipped {rel}: not a text file I can edit safely.")
            continue
        changes.append({**c, "path": rel, "action": action})
    changes = changes[:_MAX_CHANGES]
    plan["changes"] = changes
    summary = str(plan.get("summary") or "").strip()
    if not changes:
        return summary or "I could not work out a change to make, sir."
    with _state_lock:
        if _JOB:                  # absent when called outside a background job
            _JOB["files"] = {c["path"]: {"action": c["action"],
                                         "what": str(c.get("what", "")),
                                         "state": "waiting"} for c in changes}
            _JOB["summary"] = summary
    _update("writing")
    _log(player, f"Plan: {summary}")
    for c in changes:
        _log(player, f"  {c['action']:6} {c['path']}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = folder / _BACKUP_DIR / stamp
    edited: list[str] = []
    created: list[str] = []
    written: dict[str, str] = {}

    for c in changes:
        rel = c["path"]
        target = folder / rel
        current = texts.get(rel, "")
        _update(file=rel, file_state="writing")
        _log(player, f"Writing {rel}…")
        try:
            new = _write_change(c, plan, instruction, context, current,
                                written=written)
        except Exception as e:
            _update(file=rel, file_state="failed")
            _log(player, f"Could not write {rel}: {str(e)[:150]}")
            continue
        why = _rejected(rel, current, new)
        if why:
            _update(file=rel, file_state="skipped")
            _log(player, f"Kept {rel} as it was — {why}.")
            continue
        if c["action"] == "edit":
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            edited.append(rel)
        else:
            created.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new, encoding="utf-8", newline="")
        written[rel] = new
        _update(file=rel, file_state="done")

    if not written:
        return ("I planned the change but could not write any of it, sir. "
                "Nothing in the folder was touched.")

    # Check that the files fit each other, then that each one parses.
    _update("checking")
    _log(player, "Checking that the changed files fit together…")
    try:
        mismatched = _review_interfaces(instruction, written, texts, rules=guide)
    except Exception as e:
        mismatched = {}
        _log(player, f"Cross-file review skipped: {str(e)[:150]}")
    for rel, problem in mismatched.items():
        _update(file=rel, file_state="fixing")
        _log(player, f"Fixing {rel}: {problem[:160]}")
        c = next(x for x in changes if x["path"] == rel)
        try:
            new = _write_change(c, plan, instruction, context, written[rel],
                                error=problem, written=written)
        except Exception as e:
            _log(player, f"Fix failed for {rel}: {str(e)[:150]}")
            continue
        why = _rejected(rel, written[rel], new)
        if why:
            _log(player, f"Kept the previous {rel} — {why}.")
        else:
            (folder / rel).write_text(new, encoding="utf-8", newline="")
            written[rel] = new
        _update(file=rel, file_state="done")
    if not mismatched:
        _log(player, "The changed files fit together.")

    for round_no in range(_FIX_ROUNDS + 1):
        broken = {rel: err for rel, text in written.items()
                  if (err := _syntax_error(rel, text))}
        if not broken or round_no == _FIX_ROUNDS:
            break
        for rel, err in broken.items():
            _update(file=rel, file_state="fixing")
            _log(player, f"Fixing {rel}: {err}")
            c = next(x for x in changes if x["path"] == rel)
            try:
                new = _write_change(c, plan, instruction, context,
                                    written[rel], error=err, written=written)
            except Exception as e:
                _log(player, f"Fix failed for {rel}: {str(e)[:150]}")
                continue
            why = _rejected(rel, written[rel], new)
            if why:
                _log(player, f"Kept the previous {rel} — {why}.")
            else:
                (folder / rel).write_text(new, encoding="utf-8", newline="")
                written[rel] = new
            _update(file=rel, file_state="done")
    still_broken = [rel for rel, text in written.items() if _syntax_error(rel, text)]
    for rel in still_broken:
        _update(file=rel, file_state="broken")

    # Test the result the way the app will use it, and fix what fails.
    failing = _test_and_fix(folder, written, changes, plan, instruction,
                            context, player, stack=stack, originals=texts,
                            baseline=baseline)

    def restore() -> str:
        for rel in edited:
            try:
                shutil.copy2(backup / rel, folder / rel)
            except OSError:
                pass
        for rel in created:
            try:
                (folder / rel).unlink()
            except OSError:
                pass
        return f"Put '{folder.name}' back as it was before the change."

    try:
        from core.undo import push_undo
        push_undo(f"changes to {folder.name}", restore)
    except Exception:
        pass

    # What was learned about the project still holds after a change made
    # within its rules — without this, every job would invalidate the study
    # and pay for it again on the next request.
    _refresh_brief(folder)

    _open_vscode(folder)
    took = time.monotonic() - started
    parts = []
    if edited:
        parts.append(f"changed {len(edited)} file{'s' if len(edited) != 1 else ''} "
                     f"({', '.join(edited)})")
    if created:
        parts.append(f"created {len(created)} ({', '.join(created)})")
    msg = f"Done with '{folder.name}' in {took:.0f}s: {' and '.join(parts)}."
    if summary:
        msg += f" {summary}"
    if still_broken:
        msg += f" These still have syntax errors: {', '.join(still_broken)}."
    if failing:
        msg += (" These still fail their test and need a look: "
                + "; ".join(f"{r} — {p[:160]}" for r, p in failing.items()) + ".")
    elif written:
        msg += " Every changed file loaded and passed its test."
    if edited:
        msg += f" Originals are in {_BACKUP_DIR}/{stamp}; say 'undo' to restore them."
    else:
        msg += " Say 'undo' to remove the new files."
    _log(player, msg)
    return msg


def _start_improve(folder_raw: str, instruction: str, speak=None, player=None) -> str:
    """Start a folder job in the background and answer at once.

    A job reads the whole project and writes several files, each a minute or
    more from a reasoning model. Holding the voice connection's tool call open
    for that long leaves the assistant mute for the duration — so the job runs
    on its own thread, the assistant says it has started, and the result is
    handed back into the conversation when it is ready."""
    global _job_name
    folder = _resolve_folder(folder_raw)
    if folder is None:
        return ("I need a project folder for that, sir — drop one into the upload "
                "box, or tell me its path.")
    why = _refusal(folder)
    if why:
        return why
    if not _job_lock.acquire(blocking=False):
        return f"I am still working on '{_job_name}', sir. One folder at a time."
    _job_name = folder.name

    who = _coding_provider().partition("@")[2] or "the coding model"
    _new_job(folder, who, instruction, player, kind="improve")
    return _run_job(lambda: _improve_folder(folder, instruction, player),
                    folder.name, who, speak, player)


def _run_job(job, name: str, who: str, speak=None, player=None) -> str:
    """Run a coding job on its own thread and answer the tool call at once.

    Both kinds of job take minutes. Run inside the tool call, they held the
    voice connection's receive loop for the whole time: the assistant could not
    answer anything said meanwhile, and a connection left waiting that long is
    one the server may drop. So the call returns straight away, the panel shows
    the work, and the result comes back into the conversation when it is done.
    The caller must already hold `_job_lock`; it is released here."""
    def work():
        status = "done"
        try:
            result = job()
            with _state_lock:
                wrote = any(f.get("state") == "done" for f in _JOB["files"].values())
            if not wrote:
                status = "nothing changed"
            elif ("couldn't fully fix" in result or "could not" in result.lower()
                  or "still fail their test" in result):
                status = "finished with problems"
        except Exception as e:
            status = "failed"
            result = f"The coding job on '{name}' failed: {type(e).__name__}: {str(e)[:200]}"
            _log(player, result)
        finally:
            _job_lock.release()
        _update(status, result=result, finished=time.time())
        if speak:
            speak(f"[DEV_AGENT_DONE] {result[:900]} | Tell the user briefly, in one "
                  f"or two sentences, what was done. Do not read file paths aloud.")

    threading.Thread(target=work, daemon=True, name="dev_agent-job").start()
    # Kept short on purpose: this is what the assistant turns into speech, and
    # every word here is time before the user hears anything.
    return (f"[STARTED] The coding agent ({who}) is now working on "
            f"'{name}' in the background; progress is on screen. Say ONE "
            f"short sentence that it has started and you will tell them when it "
            f"finishes. Nothing else.")


def _start_build(description: str, language: str, project_name: str,
                 timeout: int, speak=None, player=None) -> str:
    """A new project, built in the background like a folder job."""
    global _job_name
    if not _job_lock.acquire(blocking=False):
        return f"I am still working on '{_job_name}', sir. One job at a time."
    name = re.sub(r"[^\w\-]", "_", project_name) if project_name else "new project"
    _job_name = name
    who = _coding_provider().partition("@")[2] or "the coding model"
    _new_job(PROJECTS_DIR / name, who, description, player, kind="build")
    return _run_job(lambda: _build_project(description, language, project_name,
                                           timeout, speak=None, player=player),
                    name, who, speak, player)


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    mode         = str(p.get("mode") or "").strip().lower()
    folder       = str(p.get("folder") or "").strip()
    if mode == "status":
        return _status_report()
    if mode == "improve" or (folder and mode != "build"):
        if not description:
            return "Tell me what to change in the folder, sir."
        return _start_improve(folder, description, speak=speak, player=player)
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))

    if not description:
        return "Please describe the project you want me to build, sir."

    return _start_build(description, language, project_name, timeout,
                        speak=speak, player=player)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "dev_agent",
    "description": (
        "The coding agent, running on the user's chosen coding model. "
        "mode='improve' changes an EXISTING project folder — the uploaded one or "
        "a path they name: fixes bugs, adds features, refactors. When a folder is "
        "uploaded, ANY request to add, create or change something is improve. "
        "Both modes run in the background with progress on screen. "
        "mode='build' creates a brand-new standalone project, only when no "
        "folder is meant. mode='status' reports what the coding agent is doing or "
        "did — use it whenever the user asks about the coding work. "
        "Before calling with improve or build, FIRST say one short sentence that "
        "you are handing it to the coding agent, then call it in the same turn."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "description": {
                "type": "STRING",
                "description": "build: what the project should do. improve: what to change in the folder, in the user's words"
            },
            "mode": {
                "type": "STRING",
                "description": "'improve' to change an existing folder, 'build' for a new project, 'status' to ask what the coding agent is doing or did"
            },
            "folder": {
                "type": "STRING",
                "description": "improve only: the folder path. Leave empty to use the folder in the upload box"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "project_name": {
                "type": "STRING",
                "description": "Optional project folder name"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Run timeout in seconds (default: 30)"
            }
        },
        "required": [
            "description"
        ]
    },
    "handler": dev_agent,
}
