"""Spotify control plugin for Mark-LV headless JARVIS.

Provides one trusted Gemini tool (`spotify_control`) that drives playback on the
exact LaCasa receiver via `SpotifyController.perform()` from Task 4. Importing
this module never reads or writes credentials and never performs network I/O —
the controller is created lazily by `get_controller()` only when `run()` is
actually invoked.
"""

from __future__ import annotations

import threading
from typing import Callable

from core.spotify import SpotifyAuthorizationRequired, SpotifyController


# ---- Plugin metadata -------------------------------------------------------

PLUGIN = {
    "name": "spotify_control",
    "description": (
        "Steuerung der Spotify-Wiedergabe auf dem exakten LaCasa-Empfänger "
        "(raspotify (lacasa-server)). Aktionen: play, pause, resume, next, "
        "previous, volume, current. Für 'play' einen Suchbegriff (query) "
        "angeben, für 'volume' die Lautstärke in Prozent."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "Aktion: play|pause|resume|next|previous|volume|current",
                "enum": ["play", "pause", "resume", "next", "previous", "volume", "current"],
            },
            "query": {
                "type": "STRING",
                "description": "Suchbegriff für 'play' (Pflicht bei play)",
            },
            "kind": {
                "type": "STRING",
                "description": "Inhaltstyp für 'play': track|album|artist (Standard: track)",
                "enum": ["track", "album", "artist"],
                "default": "track",
            },
            "volume_percent": {
                "type": "INTEGER",
                "description": "Lautstärke 0–100 (Pflicht bei volume)",
                "minimum": 0,
                "maximum": 100,
            },
        },
        "required": ["action"],
    },
}


# ---- Lazy controller factory ----------------------------------------------

# The default controller reads the OAuth store and may perform a token refresh,
# which is a network operation. It must therefore never run at import time and
# only when a real action is dispatched. Tests and the headless registry replace
# this with a fake via `patch.object(plugin, "get_controller", ...)`.

_default_controller: SpotifyController | None = None
_default_controller_lock = threading.Lock()


def get_controller() -> SpotifyController:
    """Return the configured SpotifyController, creating it lazily on first use."""
    from core.spotify import default_controller
    return default_controller()


# ---- Validation helpers ----------------------------------------------------

_ACTIONS = ("play", "pause", "resume", "next", "previous", "volume", "current")
_KINDS = ("track", "album", "artist")


def _validate(parameters: dict) -> str | None:
    """Return a German error string, or None when the parameters are valid."""
    action = parameters.get("action")
    if action not in _ACTIONS:
        return (
            "Unbekannter Spotify-Befehl. "
            "Verfügbare Aktionen: play, pause, resume, next, previous, volume, current."
        )

    if action == "volume":
        vp = parameters.get("volume_percent")
        if vp is None or isinstance(vp, bool) or type(vp) is not int or not (0 <= vp <= 100):
            return "volume_percent muss eine ganze Zahl zwischen 0 und 100 sein."

    if action == "play":
        query = parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            return "query (Suchbegriff) ist für 'play' erforderlich."
        kind = parameters.get("kind", "track")
        if kind not in _KINDS:
            return "kind (Inhaltstyp) muss track, album oder artist sein."

    return None


# ---- Public entry point ----------------------------------------------------

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Validate the action-specific contract and call `get_controller().perform()` once.

    Returns a German string. Operational Spotify failures are caught inside
    `SpotifyController.perform()` and rendered as German text; this function
    never raises an operational Spotify failure into Gemini.
    """
    error = _validate(parameters)
    if error is not None:
        return error

    action = parameters["action"]
    try:
        controller = get_controller()
    except Exception:  # noqa: BLE001 — never raise into Gemini, never leak internals
        return "Spotify ist zurzeit nicht verbunden. Bitte einmal neu autorisieren."

    try:
        return controller.perform(
            action=action,
            query=parameters.get("query"),
            kind=parameters.get("kind", "track"),
            volume_percent=parameters.get("volume_percent"),
        )
    except SpotifyAuthorizationRequired:
        return (
            "Spotify muss einmal verbunden werden. "
            "Bitte im Dashboard einmal neu autorisieren."
        )
    except Exception as exc:  # noqa: BLE001 — last-resort guard, controller already
        # returns strings, but an unexpected error must not propagate.
        return f"Ein unerwarteter Fehler ist aufgetreten: {type(exc).__name__}"
