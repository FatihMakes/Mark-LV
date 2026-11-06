"""Voice-accessible controller for the opt-in Gesture Studio."""
from __future__ import annotations

import json
import threading
from pathlib import Path

PLUGIN = {
    "name": "gesture_studio",
    "description": (
        "Start, stop, or check MARK LVI webcam hand-gesture control. Use when "
        "the user asks to enable gesture control, move the mouse with a hand, "
        "drag with a pinch, scroll, control media volume/playback, or stop "
        "hand tracking. Always require an explicit start command before camera use."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {
                "type": "STRING",
                "enum": ["start", "stop", "status"],
                "description": "start enables camera tracking; stop disables it; status checks state."
            },
            "camera_index": {
                "type": "INTEGER",
                "description": "Optional webcam index. Usually 0."
            }
        },
        "required": ["command"]
    },
}

_ENGINE = None
_LOCK = threading.Lock()


def _status(player, message):
    if player:
        try:
            player.write_log("GESTURE: " + str(message))
        except Exception:
            pass


def run(parameters: dict, player=None, session_memory=None) -> str:
    global _ENGINE
    command = str(parameters.get("command", "status")).strip().lower()
    try:
        with _LOCK:
            if _ENGINE is None:
                from core.gesture_engine import GestureEngine
                _ENGINE = GestureEngine(
                    on_status=lambda s: _status(player, s),
                    on_gesture=lambda s: _status(player, s)
                )
            engine = _ENGINE

        if command == "status":
            return "Gesture control is active." if engine.running else "Gesture control is stopped."
        if command == "stop":
            engine.stop()
            _status(player, "STOPPED — webcam released")
            return "Gesture control stopped and the mouse drag was released."
        if command == "start":
            camera_index = parameters.get("camera_index", 0)
            ok, message = engine.start(camera_index)
            _status(player, message)
            return message
        return "Choose start, stop, or status."
    except Exception as exc:
        return "Gesture Studio could not run: " + str(exc)[:180]
