"""
Drop-in JARVIS plugin template.

Copy this file, rename it (no leading underscore, e.g., my_plugin.py), fill in PLUGIN,
PLUGIN_SETTINGS (optional), and run().
No other file needs to change — JARVIS discovers this automatically at startup.

ONE RULE IF YOUR PLUGIN NEEDS TO ASK A MODEL SOMETHING
    Go through `core.gemini`. Never build your own `genai.Client`.

        from core import gemini
        answer = gemini.text(prompt, tier=gemini.FAST)          # short decisions
        answer = gemini.text(prompt, tier=gemini.SMART,         # reasoning, code
                             timeout_ms=60_000)
        data   = gemini.as_json(prompt, tier=gemini.SMART)      # structured reply
        resp   = gemini.call(contents, tier=gemini.SMART)       # the full object

    You get three things that are impossible to add afterwards: a deadline on
    every call, a ladder that steps to another model when one is out of quota or
    not answering, and — since Mark LVI — whatever provider the user configured
    in ⚙ → ENGINE & PROVIDERS, so your plugin keeps working on Ollama or another
    API when Gemini's text quota is gone.
"""

from typing import Any, Optional
from memory.config_manager import get_plugin_setting, get_plugin_config

# ── 1. Tool Declaration (Required) ───────────────────────────────────────────
PLUGIN = {
    "name": "my_plugin",                     # snake_case, unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    "description": (
        "One or two sentences Gemini uses to decide when to call this tool. "
        "Be explicit about trigger phrases and, if it could be confused with "
        "another tool, say which tool NOT to use instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {
                "type": "STRING",
                "description": "Operation mode: 'url', 'html', 'spec', 'text', 'quiz', 'review', or 'close'",
            },
            "query": {
                "type": "STRING",
                "description": "Optional search term, content payload, or address",
            },
        },
        "required": [],   # omit or leave empty for a zero-argument tool
    },
}

# ── 2. Plugin Settings Schema (Optional) ──────────────────────────────────────
# If declared, JARVIS automatically generates a GUI form in ⚙ → PLUGIN SETTINGS.
# Stored configuration values can be retrieved with get_plugin_setting / get_plugin_config.
PLUGIN_SETTINGS = {
    "namespace": "my_plugin",
    "title": "MY PLUGIN CONFIGURATION",
    "fields": [
        {
            "key": "api_key",
            "label": "API Key / Auth Token",
            "type": "password",  # "text" | "password" | "choice" | "toggle"
            "placeholder": "Enter secret token...",
            "default": "",
        },
        {
            "key": "display_theme",
            "label": "HUD Display Theme",
            "type": "choice",
            "options": ["dark_cyan", "amber", "matrix_green"],
            "default": "dark_cyan",
        },
        {
            "key": "refresh_interval",
            "label": "Auto-refresh Interval (sec)",
            "type": "text",
            "placeholder": "10",
            "default": "10",
        },
        {
            "key": "enable_sound",
            "label": "Enable Audio Alerts",
            "type": "toggle",
            "default": True,
        },
    ],
}


# ── 3. Tool Entrypoint (Required) ─────────────────────────────────────────────
def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: dict of the args Gemini extracted, matching PLUGIN['parameters'].
    player: the JarvisUI instance (may be None in tests/headless). Provides thread-safe methods:
            - Activity log: player.write_log(text)
            - Bottom panel: player.show_content(title, text)
            - Interactive quiz: player.show_quiz(topic, questions, grade=None)
            - Structured review: player.show_review(title, summary, findings)
            - Video / image in HUD: player.show_video(url, title), player.show_image(path, title)
            - Universal Plugin HUD Display:
                * player.show_hud_view(plugin_id, title, content, content_type="url", replace=True)
                * player.update_hud_view(plugin_id, content, content_type=None)
                * player.close_hud_view(plugin_id)
                * player.get_hud_view_state() -> dict
    session_memory: reserved, usually None today.
    Return a short natural-language string — this is spoken back to the user.
    Never raise: catch your own errors and return a spoken error string instead.
    """
    mode = str(parameters.get("mode") or "spec").lower().strip()
    query = str(parameters.get("query") or "").strip()

    # Read user preferences from ⚙ → PLUGIN SETTINGS
    api_key = get_plugin_setting("my_plugin", "api_key", default="")
    theme = get_plugin_setting("my_plugin", "display_theme", default="dark_cyan")
    enable_sound = get_plugin_setting("my_plugin", "enable_sound", default=True)

    try:
        # ── Example A: Bottom Content Panel ──────────────────────────────────
        if mode == "content" and player:
            player.show_content(
                "SYSTEM ADVISORY",
                f"Plugin execution completed successfully.\nTheme: {theme}\nQuery: {query}"
            )
            return "Sir, I have posted the report to the lower panel."

        # ── Example B: Interactive Quiz on Screen ────────────────────────────
        if mode == "quiz" and player:
            quiz_questions = [
                {
                    "question": "What is the primary power source of the Mark LVII?",
                    "options": ["Arc Reactor", "Combustion Engine", "Solar Arrays", "Uranium Core"],
                    "answer": "Arc Reactor",
                    "note": "Repulsor flight and weapon systems run from the chest arc reactor."
                }
            ]
            player.show_quiz("SUIT SYSTEMS DIAGNOSTIC", quiz_questions)
            return "Diagnostic quiz initialized on the HUD, Sir."

        # ── Example C: Structured Document Review ────────────────────────────
        if mode == "review" and player:
            findings = [
                {
                    "heading": "Thermal Regulation",
                    "detail": "Coolant distribution optimal across all thrusters.",
                    "severity": "note",
                    "quote": "Thruster core 99.4% nominal",
                    "suggestion": "Maintain current power profile."
                }
            ]
            player.show_review("SYSTEM CHECK REVIEW", "Review of primary suit sub-assemblies.", findings)
            return "System review is now displayed on screen."

        # ── Example D: Universal Plugin HUD Display (URL Mode) ───────────────
        if mode == "url" and player:
            target_url = query if query.startswith("http") else "http://127.0.0.1:8765/"
            player.show_hud_view(
                plugin_id="my_plugin",
                title="REMOTE DASHBOARD",
                content=target_url,
                content_type="url",
                replace=True
            )
            return "Displaying the remote web dashboard on the main HUD."

        # ── Example E: Universal Plugin HUD Display (HTML Mode) ──────────────
        if mode == "html" and player:
            html_doc = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body {{
                        background: #000c14;
                        color: #00e5ff;
                        font-family: 'Consolas', monospace;
                        padding: 24px;
                    }}
                    h1 {{ border-bottom: 1px solid rgba(0, 229, 255, 0.4); padding-bottom: 8px; }}
                    .card {{ background: rgba(0, 20, 32, 0.7); border: 1px solid #005b66; border-radius: 4px; padding: 12px; margin-top: 12px; }}
                </style>
            </head>
            <body>
                <h1>◈ MY PLUGIN INTERFACE</h1>
                <div class="card">
                    <p>Current Theme: <strong>{theme}</strong></p>
                    <p>Status: <strong>ACTIVE AND RUNNING</strong></p>
                    <p>Parameters Query: <em>{query or 'None provided'}</em></p>
                </div>
            </body>
            </html>
            """
            player.show_hud_view(
                plugin_id="my_plugin",
                title="HTML INTERFACE",
                content=html_doc,
                content_type="html",
                replace=True
            )
            return "Custom HTML interface rendered on the main display."

        # ── Example F: Universal Plugin HUD Display (Declarative Spec Mode) ──
        if mode == "spec" and player:
            spec_layout = {
                "type": "container",
                "children": [
                    {"type": "heading", "text": "SYSTEM METRICS & TELEMETRY"},
                    {"type": "metric", "label": "CPU LOAD", "value": "34.2%", "delta": "-2.1%"},
                    {"type": "metric", "label": "CORE TEMP", "value": "41°C", "delta": "Nominal"},
                    {"type": "progress", "label": "BUFFER HEALTH", "value": 92, "max": 100},
                    {
                        "type": "key_value",
                        "data": {
                            "ACTIVE THEME": theme,
                            "AUDIO ALERTS": "ENABLED" if enable_sound else "MUTED",
                            "AUTHENTICATION": "VALID" if api_key else "UNCONFIGURED"
                        }
                    },
                    {
                        "type": "table",
                        "headers": ["SUBSYSTEM", "STATUS", "LATENCY"],
                        "rows": [
                            ["Repulsor Mesh", "Online", "1.2 ms"],
                            ["Avionics Bus", "Synchronized", "0.8 ms"],
                            ["Neural Link", "Active", "2.4 ms"]
                        ]
                    }
                ]
            }
            player.show_hud_view(
                plugin_id="my_plugin",
                title="DIAGNOSTIC TELEMETRY",
                content=spec_layout,
                content_type="spec",
                replace=True
            )
            return "Diagnostic telemetry spec rendered on the main HUD."

        # ── Example G: Universal Plugin HUD Display (Text Mode) ──────────────
        if mode == "text" and player:
            summary_text = (
                "JARVIS MARK LVII - SUB-ROUTINE LOG\n"
                "------------------------------------\n"
                f"TIMESTAMP: 2026-11-13\n"
                f"TARGET: {query or 'Localhost'}\n"
                f"THEME PROFILE: {theme}\n"
                "STATUS: All secondary relays operational.\n"
            )
            player.show_hud_view(
                plugin_id="my_plugin",
                title="SYSTEM LOG DUMP",
                content=summary_text,
                content_type="text",
                replace=True
            )
            return "Text telemetry posted to the main display."

        # ── Example H: Close Plugin HUD and Restore Avatar ───────────────────
        if mode == "close" and player:
            player.close_hud_view("my_plugin")
            return "Closed the plugin display and restored the avatar."

        # Fallback response for unhandled mode
        result_text = f"Executed my_plugin with mode '{mode}'."

    except Exception as exc:
        return f"Sir, my_plugin encountered an issue: {exc}"

    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass

    return result_text
