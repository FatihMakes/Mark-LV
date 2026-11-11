# Rules for coding agents working on JARVIS

The built-in coding agent (`actions/dev_agent.py`) reads this file before every
change it makes to this project, and so should any other agent or contributor.
Each rule below exists because breaking it produced code that looked right,
passed a syntax check and did nothing when used.

## How the app runs

- `main.py` holds the voice session on an asyncio event loop in a background
  thread. The window — the `gui/` package, built by `ui.py` — lives on the
  **Qt main thread**.
- Tools — `actions/*.py` and `plugins/*.py` — run on **worker threads**, never
  on the Qt main thread.
- There is exactly **one** `QApplication`, created by `ui.py`.
- Long work the user asks for in the background — research, drafting — is the
  task engine's job (`core/tasks/`, agents in `agents/*.md`), not a plugin's.

## Threading: the rule that breaks most generated code

Code in `actions/` and `plugins/` must **never create or touch a Qt widget**.
That means no `QApplication`, `QWidget`, `QMainWindow`, `QDialog`, `QThread` or
anything else from `PyQt6.QtWidgets` or `PyQt6.QtGui`. Creating a widget from a
worker thread prints `QObject::setParent: Cannot set parent, new parent is in a
different thread` and nothing appears on screen.

To show something, call the **thread-safe methods on `player`** (the `JarvisUI`
object passed to every tool). They hand the work to the Qt thread for you. The
current list, with signatures, is appended below this file by the coding agent
automatically. The ones plugins usually need:

| Need | Call |
|---|---|
| A line in the activity log | `player.write_log("text")` |
| Text in the panel under the head | `player.show_content(title, text)` |
| A quiz the user answers on screen | `player.show_quiz(topic, questions, grade=None)` |
| A structured review | `player.show_review(title, summary, findings)` |
| Play a video in the HUD | `player.show_video(path_or_url, title)` |

`show_quiz` takes `questions` as a list of dicts:
`{"question": str, "options": [str, ...] (empty list for a typed answer),
"answer": str, "note": str (shown after answering, optional)}`.
`grade(question, given) -> True | False | None` is optional; `None` means
"let the assistant judge this answer". The user answers at their own pace and
the result comes back to the conversation by itself — `run()` returns at once.

Do **not** start a separate GUI program (`subprocess` running a PyQt script)
to work around this rule. It hides every error from the user, and the app
already has a screen for it.

## Plugins

A plugin is one file `plugins/<name>.py` (no leading underscore) with:

- `PLUGIN = {"name": ..., "description": ..., "parameters": {"type": "OBJECT", ...}}`
- `def run(parameters: dict, player=None, session_memory=None) -> str`

`run()` must:

- return a **short string** — the assistant turns it into speech;
- **never raise** — catch errors and return a sentence saying what went wrong;
- return **within a few seconds** — put slow work on a `threading.Thread`
  and show its result through `player` when it is done;
- treat `player` as possibly `None` (tests and the phone dashboard).

Helper modules may live beside it with a leading underscore
(`plugins/_helper.py`); the loader skips those. See `plugins/_template.py`.

A new plugin is **enabled automatically** the moment its file exists — nothing
needs to be registered or switched on. Never edit `config/api_keys.json` to
"enable" one; that file holds the user's API keys and settings.

## Asking a model for something

Always go through `core.gemini` — it has deadlines, a fallback ladder and the
user's chosen provider:

```python
from core import gemini
text = gemini.text(prompt, tier=gemini.FAST)       # short answers
data = gemini.as_json(prompt, tier=gemini.SMART)    # structured, may return None
```

Every call can fail or return `None`. Code must handle that — for example with
a small built-in fallback — instead of crashing.

## Changing the window (`gui/`)

- The window is the `gui/` package: `window.py` (MainWindow — columns, drawers,
  content panel), `widgets.py`, `overlays.py`, `providers_panel.py`, `hud.py`.
  `ui.py` keeps only `JarvisUI` and re-exports every older name.
- Widgets are created and changed only on the Qt thread.
- To let other threads trigger a change: add a `pyqtSignal` to `MainWindow` in
  `gui/window.py`, connect it to a slot in its `__init__`, and add a thin public
  method on `JarvisUI` in `ui.py` that only emits the signal. `show_content` /
  `_content_sig` / `_show_content` is the pattern to copy.
- Colours come from the `C` palette in `gui/theme.py`; stylesheets use only its
  hex values, so the live re-theme can repaint them. Fonts come from
  `gui/style.py` — `hud_font(size)`, `mono_font(size)`, `px_font(px)` — never a
  hard-coded family.

## General

- Never send a child process's output to `DEVNULL` without logging it, and
  never report success without checking that the thing actually happened.
- Settings and keys live in `config/api_keys.json` through
  `memory/config_manager.py`; never write that file directly.
- Keep to the libraries already in `requirements.txt`.
