"""Trusted Cannanas voice plugin — the single model-facing door.

Read actions are fast and bounded; prepare actions return the complete German
preview with a fixed confirmation instruction. Confirmation state, tokens and
club ids never reach Gemini: confirm/cancel are host state-machine operations
(core/cannanas_confirmation.py + the host grammar), never plugin actions.
"""
from __future__ import annotations

from core.cannanas_actions import (
    CANNANAS_MODEL_ACTIONS,
    CANNANAS_PREPARE_ACTIONS,
    CANNANAS_READ_ACTIONS,
)
from core.cannanas_host import CannanasUnavailable, get_host

PLUGIN = {
    'name': 'cannanas',
    'description': (
        'Cannanas-Grow: Pflanzen, Räume, Sorten, Chargen und Vermehrungsmaterial '
        'nachfragen. Neue Pflanzen, Sorten, Chargen oder Tagebucheinträge werden '
        'nur nach ausdrücklicher Bestätigung angelegt.'),
    'parameters': {
        'type': 'OBJECT',
        'properties': {
            'action': {'type': 'STRING',
                       'description': 'Geschlossene Aktionsliste',
                       'enum': sorted(CANNANAS_MODEL_ACTIONS)},
            'query': {'type': 'STRING', 'description': 'Suchbegriff (Name/ID)'},
            'room': {'type': 'STRING', 'description': 'Raum (Zone)'},
            'status': {'type': 'STRING', 'description': 'Status (z. B. SEED, FLOWERING)'},
            'plant_name': {'type': 'STRING', 'description': 'Name der Pflanze/Charge'},
            'count': {'type': 'INTEGER', 'description': 'Anzahl/Maximalwert'},
            'text': {'type': 'STRING', 'description': 'Notizen / Tagebuchtext'},
        },
        'required': ['action'],
    },
    # deliberately no 'behavior': confirmations must be synchronous when they
    # occur; read actions are fast and bounded.
}


def run(parameters: dict, player=None, session_memory=None, host=None) -> dict:
    """Dispatch one closed-enum action; never raise to the model path."""
    args = dict(parameters or {})
    action = str(args.get('action') or '').strip()
    if action not in CANNANAS_MODEL_ACTIONS:
        # Defense in depth behind tool_allowed: unknown/host-only actions get
        # a fixed refusal and never reach the adapter.
        return {
            'ok': False,
            'summary': 'Diese Aktion ist für sprechende Steuerung nicht freigegeben.',
        }
    resolved = host if host is not None else get_host()
    try:
        if action in CANNANAS_READ_ACTIONS:
            return resolved.call_read(action, args)
        if action in CANNANAS_PREPARE_ACTIONS:
            return resolved.prepare(action, args, session_memory)
        # Unreachable behind tool_allowed, kept for fail-closed completeness.
        return {
            'ok': False,
            'summary': 'Diese Aktion ist für sprechende Steuerung nicht freigegeben.',
        }
    except CannanasUnavailable as unavailable:
        text = str(unavailable) or ''
        # Internal diagnostics never leak; only the fixed configuration text
        # (variable names only) or the bounded German unavailability is spoken.
        if 'nicht verfügbar' in text or 'nicht konfiguriert' in text:
            return {'ok': False, 'summary': text}
        return {
            'ok': False,
            'summary': ('Cannanas ist derzeit nicht erreichbar. '
                        'Bitte später erneut versuchen.'),
        }
    except Exception:
        return {
            'ok': False,
            'summary': ('Cannanas ist derzeit nicht verfügbar. '
                        'JARVIS und die übrigen Funktionen laufen weiter.'),
        }