"""
Loads a project's changed Python code in a throwaway process, under the
project's own interpreter, and prints one JSON report. Works for any project;
the JARVIS profile adds running changed plugins the way the app runs them.
Started by core/code_checks.py — never imported by the app.

    python _smoke_harness.py <project folder> <json: {"imports": [...], "plugins": [...]}>

For every module in "imports": import it. For every plugin in "plugins": load
it the way core/plugin_loader does, validate PLUGIN, then call run() ON A
WORKER THREAD while the main thread runs a Qt event loop — which is exactly the
arrangement in the real app, and the only one in which a plugin that builds
widgets off the Qt thread shows its fault.

Nothing here can reach a model or the network on the plugin's behalf:
core.gemini is replaced with stubs that answer "nothing came back", which is
also the case every plugin must survive.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
import threading
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.abspath(sys.argv[1])
SPEC = json.loads(sys.argv[2])
sys.path.insert(0, ROOT)
os.chdir(ROOT)

report = {"imports": {}, "plugins": {}}
qt_messages: list[str] = []


def _trace(kind: str, text: str) -> None:
    """A progress line on stderr, flushed at once.

    A plugin that builds Qt widgets on a worker thread can block inside Qt
    while holding the interpreter lock — after that nothing in this process
    runs again, not even a watchdog. These lines are what the parent reads
    when it has to kill the process, so it can still say what happened."""
    try:
        sys.stderr.write(f"@@{kind}@@ {text}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _stub_models() -> None:
    try:
        from core import gemini
    except Exception:
        return
    gemini.call = lambda *a, **k: None
    gemini.text = lambda *a, **k: k.get("default", "")
    gemini.as_json = lambda *a, **k: k.get("default")


def _sample(schema: dict) -> dict:
    """Plausible arguments from a PLUGIN parameters schema."""
    out = {}
    props = (schema or {}).get("properties") or {}
    for name, spec in props.items():
        spec = spec or {}
        kind = str(spec.get("type", "STRING")).upper()
        if spec.get("enum"):
            out[name] = spec["enum"][0]
        elif kind == "INTEGER":
            out[name] = max(1, int(spec.get("minimum", 1) or 1))
        elif kind == "NUMBER":
            out[name] = 1.0
        elif kind == "BOOLEAN":
            out[name] = False
        elif kind == "ARRAY":
            out[name] = []
        elif kind == "OBJECT":
            out[name] = {}
        else:
            out[name] = "python basics"
    return out


class Player:
    """Stands in for JarvisUI. Records every call; every method is safe to call
    from any thread, as the real ones are."""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(*args, **kwargs):
            self.calls.append(name)
            if name == "show_quiz":
                qs = args[1] if len(args) > 1 else kwargs.get("questions")
                if not isinstance(qs, list) or not qs or not all(
                        isinstance(q, dict) and q.get("question") for q in qs):
                    self.calls.append("show_quiz:BAD_QUESTIONS")
            return None
        return method

    # attributes some tools read
    muted = False
    current_file = None


def main() -> None:
    # The JARVIS profile recreates the app's arrangement: one QApplication on
    # this thread, model calls stubbed. Any other project gets neither — its
    # own `core` package, if it has one, is not ours to import.
    app = None
    if SPEC.get("profile") == "jarvis":
        try:
            from PyQt6.QtCore import qInstallMessageHandler
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance() or QApplication([])

            def handler(_mode, _ctx, msg):
                qt_messages.append(str(msg))
                _trace("QT", str(msg))
            qInstallMessageHandler(handler)
        except Exception:
            app = None
        _stub_models()

    for mod in SPEC.get("imports", []):
        try:
            importlib.import_module(mod)
            report["imports"][mod] = ""
        except BaseException:
            report["imports"][mod] = traceback.format_exc(limit=6)[-1500:]

    for rel in SPEC.get("plugins", []):
        res = {"error": "", "warnings": [], "calls": [], "returned": None, "seconds": 0}
        report["plugins"][rel] = res
        path = os.path.join(ROOT, rel)
        stem = os.path.splitext(os.path.basename(rel))[0]
        try:
            from core.plugin_loader import _call_run, _validate
            spec = importlib.util.spec_from_file_location(f"plugins.{stem}", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"plugins.{stem}"] = module
            spec.loader.exec_module(module)
            rec = _validate(module, os.path.basename(rel))
            if not rec.valid:
                res["error"] = f"The plugin loader rejects it: {rec.error}"
                continue
            params = _sample(rec.parameters)
        except BaseException:
            res["error"] = "Importing it failed:\n" + traceback.format_exc(limit=6)[-1500:]
            continue

        player = Player()
        box: dict = {}
        before = len(qt_messages)

        def call():
            try:
                box["value"] = _call_run(rec.run, params, player, None)
            except BaseException:
                box["error"] = traceback.format_exc(limit=8)[-1800:]

        t0 = time.monotonic()
        _trace("RUN", rel)
        worker = threading.Thread(target=call, daemon=True)   # as the app runs tools
        worker.start()
        # Only WAIT here — no Qt calls on this thread while the plugin runs. A
        # plugin that builds a window on its own thread can deadlock against a
        # main thread pumping events, and then even this process's watchdog
        # cannot run. Qt reports the cross-thread fault at the moment the
        # widget is made, on the worker, so nothing is missed by waiting.
        worker.join(timeout=25)
        if app is not None and not worker.is_alive():
            for _ in range(20):
                app.processEvents()
                time.sleep(0.01)
        res["seconds"] = round(time.monotonic() - t0, 1)
        res["calls"] = player.calls
        res["warnings"] = qt_messages[before:][:10]
        res["params"] = params
        if worker.is_alive():
            res["error"] = ("run() did not return within 25 seconds. Tools must return "
                            "at once and do slow work on a background thread.")
        elif "error" in box:
            res["error"] = "run() raised:\n" + box["error"]
        else:
            res["returned"] = box.get("value")

    sys.stdout = sys.__stdout__
    print("@@REPORT@@" + json.dumps(report, default=str))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    # Plugins print; keep their output out of the report line.
    sys.stdout = io.StringIO()
    main()
