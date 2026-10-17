"""Host-side Cannanas wiring: boot identity, confirmation grammar, lifecycle.

This module owns everything the model never sees: configuration checks (fail
before network I/O, naming only missing environment variables), the adapter
client lifecycle with the pinned release, the pending-confirmation
coordinator, the deterministic German confirmation grammar, the host audit,
and degraded readiness — an adapter failure never exits JARVIS, Spotify or
Home Assistant.

No network imports here: the coordinator/grammar are pure host state
machines and the MCP client imports its SDK only when the child starts.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

from core.cannanas_audit import CannanasAudit, CannanasAuditClosed
from core.cannanas_client import (
    ADAPTER_TOOLS,
    PINNED_ADAPTER_VERSION,
    PINNED_WHEEL_SHA256,
    CannanasClient,
    CannanasPreSendRefusal,
    CannanasUnavailable,
    child_environment,
)
from core.cannanas_confirmation import (
    ConfirmationCoordinator,
    DEFAULT_PENDING_TTL_SECONDS,
    OpaqueToken,
    PendingConfirmation,
)

# ── boot identity ──────────────────────────────────────────────────────────

# Fresh per JARVIS process: supplied with every prepare/confirm as
# adapter_boot_id so a restart fences ordinary pending confirmations.
_ADAPTER_BOOT_ID = None


def adapter_boot_id() -> str:
    """Process-lifetime boot identity (uuid4), identical within one process."""
    global _ADAPTER_BOOT_ID
    if _ADAPTER_BOOT_ID is None:
        _ADAPTER_BOOT_ID = uuid.uuid4().hex
    return _ADAPTER_BOOT_ID


# ── deterministic, bounded German confirmation grammar ─────────────────────
# Exact bounded forms only. Everything outside these sets is 'none' — no fuzzy
# matching, no model interpretation. The grammar decision belongs to the host,
# never to Gemini.
_AFFIRM_FORMS = frozenset({
    'ja', 'ok', 'okay', 'bestätigen', 'bestätige', 'bitte bestätigen',
    'ja bitte', 'speichern', 'speichere',
})
_DENY_FORMS = frozenset({
    'nein', 'abbrechen', 'brich ab', 'abbruch', 'stop', 'stopp', 'stoppe',
    'stoppen',
})
_TRAILING_PUNCTUATION = '.,!?:;'


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ''
    text = text.strip().lower()
    # Trim trailing punctuation only; never fuzzy-match inside the utterance.
    return text.rstrip(_TRAILING_PUNCTUATION)


def confirmation_grammar(text) -> str:
    """{'affirm' | 'deny' | 'none'} for one finalized utterance."""
    normalized = _normalize(text)
    if not normalized:
        return 'none'
    if normalized in _AFFIRM_FORMS:
        return 'affirm'
    if normalized in _DENY_FORMS:
        return 'deny'
    return 'none'


# ── configuration (host env) ───────────────────────────────────────────────

ENV_ADAPTER_COMMAND = 'JARVIS_CANNANAS_ADAPTER_COMMAND'
ENV_ADAPTER_ARGS = 'JARVIS_CANNANAS_ADAPTER_ARGS'
ENV_HOST_AUDIT = 'JARVIS_CANNANAS_AUDIT_FILE'
ADAPTER_ENV_VARS = ('CANNANAS_API_KEY', 'CANNANAS_CLUB_ID',
                    'CANNANAS_STATE_DIR', 'CANNANAS_AUDIT_FILE')
REQUIRED_ENV_VARS = (ENV_ADAPTER_COMMAND, ENV_HOST_AUDIT) + ADAPTER_ENV_VARS

MAX_RECORDS = 25
SPOKEN_RECORD_LIMIT = 5

_UNAVAILABLE = 'Cannanas ist derzeit nicht verfügbar.'
_NOT_CONFIRMABLE_SHORT = ('Die Vorschau ist nicht mehr gültig. '
                          'Bitte erneut vorbereiten.')
_CONFIRM_INSTRUCTION = ('Bitte bestätigen Sie mit einem klaren „ja“, '
                        'oder sagen Sie „abbrechen“.')

REFUSAL_SPOKEN = {
    'wrong_session': _NOT_CONFIRMABLE_SHORT,
    'wrong_device': ('Diese Vorschau gehört zu einem anderen Gerät. '
                     'Bitte erneut auf demselben Gerät bestätigen.'),
    'wrong_auth_session': _NOT_CONFIRMABLE_SHORT,
    'wrong_turn': ('Die Bestätigung kommt nicht nach der Vorschau. '
                   'Bitte erneut vorbereiten.'),
    'wrong_boot': _NOT_CONFIRMABLE_SHORT,
    'wrong_payload': _NOT_CONFIRMABLE_SHORT,
    'expired': _NOT_CONFIRMABLE_SHORT,
    'already_used': ('Diese Vorschau wurde bereits bearbeitet. '
                     'Bitte erneut vorbereiten.'),
    'cancelled': ('Die Vorschau wurde abgebrochen. Bitte erneut vorbereiten.'),
}


def missing_config_variables(env=None) -> tuple:
    """The missing variable NAMES in a fixed order — values never leak."""
    source = os.environ if env is None else env
    return tuple(name for name in REQUIRED_ENV_VARS if not source.get(name))


def _unavailability_text(missing) -> str:
    if missing:
        return (f'Cannanas ist derzeit nicht verfügbar. '
                f'Es fehlt: {", ".join(missing)}.')
    return _UNAVAILABLE


def prepare_binding_for(turn) -> dict | None:
    """The adapter binding from a finalized turn; None without a device."""
    stable_device_id = getattr(turn, 'stable_device_id', None)
    if not stable_device_id:
        return None
    return {
        'jarvis_session_id': stable_device_id,
        'stable_device_id': stable_device_id,
        'auth_session_id': getattr(turn, 'auth_session_id', ''),
        'prepared_turn_id': str(getattr(turn, 'user_turn_id', '')),
        'adapter_boot_id': adapter_boot_id(),
    }


# ── bounded, secret-free record projection ─────────────────────────────────

_UNSAFE_KEY_MARKERS = ('token', 'secret', 'password', 'bearer', 'authorization')


def _compact_record(record) -> dict:
    if not isinstance(record, dict):
        return {}
    safe = {}
    for key, value in record.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in _UNSAFE_KEY_MARKERS):
            continue
        safe[key] = value
    return safe


_RESULT_RECORD_KEYS = ('plants', 'zones', 'strains', 'batches', 'materials')


def _result_records(result) -> list:
    for key in _RESULT_RECORD_KEYS:
        records = result.get(key)
        if isinstance(records, list):
            return records
    return []


def build_read_summary(prefix, records, exact, truncated) -> str:
    """Speak at most five records; qualify unproven totals ('mindestens')."""
    names = ', '.join(
        str(record.get('name') or record.get('room') or '?')
        for record in records[:SPOKEN_RECORD_LIMIT])
    if not names:
        return f'{prefix}: keine Einträge.'
    spoken = f'{prefix}: {names}.'
    extra = ''
    if len(records) > SPOKEN_RECORD_LIMIT:
        extra += (f' (Erste {SPOKEN_RECORD_LIMIT} von {len(records)} — '
                  'weitere auf Anfrage.)')
    if truncated:
        extra += ' (Liste gekürzt.)'
    if not exact:
        spoken = f'{prefix} (mindestens {len(records)}): {names}.'
    spoken += extra
    return spoken


def _record_payload(prefix, result, exact, truncated) -> dict:
    records = [_compact_record(record) for record in _result_records(result)]
    return {
        'ok': True,
        'summary': build_read_summary(prefix, records, exact, truncated),
        'records': records[:MAX_RECORDS],
        'spoken_records': min(SPOKEN_RECORD_LIMIT, len(records)),
        'total_records': len(records),
        'exact': bool(exact),
        'truncated': bool(truncated),
        'requires_confirmation': False,
    }


# ── adapter tool mapping (read + prepare) ──────────────────────────────────

def _int_param(params, key, default):
    try:
        value = int(params.get(key))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_RECORDS))


def read_tool_call(action: str, params: dict) -> 'tuple[str, dict, str]':
    """Map the closed model action to (adapter tool, adapter args, spoken prefix)."""
    params = params or {}
    query = str(params.get('query') or '').strip()
    room = str(params.get('room') or '').strip()
    status = str(params.get('status') or '').strip()
    if action == 'overview':
        return 'get_cannanas_overview', {}, 'Übersicht'
    if action == 'find_zones':
        return 'find_zones', {'query': query,
                              'limit': _int_param(params, 'count', 20)}, 'Räume'
    if action == 'find_strains':
        return 'find_strains', {'query': query,
                                'limit': _int_param(params, 'count', 20)}, 'Sorten'
    if action == 'find_plants':
        return 'find_plants', {'query': query, 'room': room,
                               'limit': _int_param(params, 'count', 20)}, 'Pflanzen'
    if action == 'find_propagation_material':
        return ('find_propagation_material',
                {'query': query, 'limit': _int_param(params, 'count', 20)},
                'Vermehrungsmaterial')
    if action == 'list_recent_batches':
        return ('list_recent_batches',
                {'limit': _int_param(params, 'count', 10)}, 'Chargen')
    if action == 'list_plants':
        return 'list_plants', {'room': room, 'status': status,
                               'limit': _int_param(params, 'count', 50)}, 'Pflanzen'
    if action == 'count_plants_by_room':
        return 'count_plants_by_room', {'room': room}, 'Pflanzen je Raum'
    raise ValueError(f'unknown read action: {action}')


def prepare_tool_call(action: str, params: dict, binding: dict) -> 'tuple[str, dict]':
    """Map the closed prepare action to (adapter tool, args incl. binding)."""
    from core.cannanas_actions import CANNANAS_PREPARE_ACTIONS
    params = params or {}
    plant_name = str(params.get('plant_name') or '').strip()
    room = str(params.get('room') or '').strip()
    status = str(params.get('status') or '').strip()
    strain = str(params.get('query') or '').strip()
    notes = str(params.get('text') or '').strip()
    count = _int_param(params, 'count', 1)
    if action not in CANNANAS_PREPARE_ACTIONS:
        raise ValueError(f'unknown prepare action: {action}')
    if action == 'prepare_plant_create':
        args = {'plant_name': plant_name, 'room': room, 'strain': strain,
                'status': status}
        if notes:
            args['notes'] = notes
        return action, {**args, 'binding': binding}
    if action == 'prepare_strain_create':
        args: dict = {'name': plant_name, 'identifier': ''}
        if notes:
            args['notes'] = notes
        return action, {**args, 'binding': binding}
    if action == 'prepare_batch_create':
        args = {'room': room, 'strain': strain, 'plant_count': count,
                'status': status}
        if notes:
            args['notes'] = notes
        return action, {**args, 'binding': binding}
    if action == 'prepare_plant_update':
        fields: dict = {}
        if status:
            fields['status'] = status
        if notes:
            fields['notes'] = notes
        if plant_name:
            fields['name'] = plant_name
        return action, {'plant': plant_name, 'fields': fields,
                        'binding': binding}
    if action == 'prepare_batch_update':
        fields = {}
        if status:
            fields['status'] = status
        if notes:
            fields['notes'] = notes
        if strain:
            fields['name'] = strain
        return action, {'batch': plant_name, 'fields': fields,
                        'binding': binding}
    if action == 'prepare_diary_create':
        return action, {'plant_or_batch': plant_name or room,
                        'text': strain or notes, 'measurements': None,
                        'binding': binding}
    raise ValueError(f'unknown prepare action: {action}')


# ── process singleton ──────────────────────────────────────────────────────

_HOST: 'CannanasHost | None' = None
_HOST_LOCK = threading.Lock()


def get_host(env=None) -> 'CannanasHost':
    """The one host for the JARVIS process; tests may set_host instead."""
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = CannanasHost(env=env)
        return _HOST


def set_host(host) -> None:
    """Explicit override (tests); the process singleton follows it."""
    global _HOST
    with _HOST_LOCK:
        _HOST = host


def complete_preview_text(preview: dict) -> str:
    """Complete, deterministic German preview line from the adapter preview."""
    preview = preview if isinstance(preview, dict) else {}
    order = ('plant_name', 'name', 'room', 'strain', 'status',
             'plant_count', 'count', 'notes', 'identifier')
    parts = []
    for key in order:
        value = preview.get(key)
        if key in preview and value not in (None, ''):
            parts.append(f'{key}: {value}')
    for key, value in preview.items():
        if key not in order and key != 'message' and value not in (None, ''):
            parts.append(f'{key}: {value}')
    if not parts:
        return 'Eintrag unverändert.'
    return ' '.join(parts)


# ── the host ───────────────────────────────────────────────────────────────

class CannanasHost:
    """Configuration, client lifecycle, coordinator, audit and confirm flow."""

    def __init__(self, *, env=None, client=None, audit=None,
                 coordinator: ConfirmationCoordinator | None = None) -> None:
        self._env = os.environ if env is None else env
        self._missing = missing_config_variables(self._env)
        self._client = client
        self._client_started = client is not None
        self._audit = audit if audit is not None else None
        self.coordinator = coordinator if coordinator is not None \
            else ConfirmationCoordinator()
        self._boot = adapter_boot_id()
        self._degraded_reason = ''
        self._lock = threading.RLock()
        if self._missing:
            self._degraded_reason = _unavailability_text(self._missing)
        elif self._audit is None:
            self._audit = CannanasAudit(self._env[ENV_HOST_AUDIT])

    # ── readiness ──────────────────────────────────────────────────────────

    def available(self) -> bool:
        """Read readiness. Writes additionally require a verified release."""
        with self._lock:
            if self._missing:
                return False
            if self._degraded_reason and 'reads allowed' in self._degraded_reason:
                return True   # read-only-safe degraded state: reads continue
            if self._degraded_reason:
                return False
            if self._client is not None and getattr(self._client, 'degraded', False):
                return False
            return True

    def availability_text(self) -> str:
        with self._lock:
            if self._missing:
                return _unavailability_text(self._missing)
            if self._degraded_reason:
                return f'Cannanas ist derzeit nicht verfügbar ({self._degraded_reason}).'
            if getattr(self._client, 'degraded', False) if self._client else False:
                return (f'Cannanas ist derzeit nicht verfügbar '
                        f'(client: {self._client.degraded_reason}).')
            return ''

    def boot_id(self) -> str:
        return self._boot

    def mark_degraded(self, reason: str) -> None:
        with self._lock:
            self._degraded_reason = reason

    def client_if_started(self):
        return self._client if self._client_started else None

    def missing_variables(self) -> tuple:
        return self._missing

    def on_client_restart(self) -> None:
        """Client restart fence: adapter restart invalidates pending confirms."""
        self.coordinator.invalidate_all_on_adapter_restart()

    # ── client lifecycle ───────────────────────────────────────────────────

    def require_client(self):
        with self._lock:
            if self._missing:
                raise CannanasUnavailable(_unavailability_text(self._missing))
            if self._client is None:
                self._client = CannanasClient({
                    'command': self._env[ENV_ADAPTER_COMMAND],
                    'args': (self._env.get(ENV_ADAPTER_ARGS) or '').split(),
                    'adapter_version': PINNED_ADAPTER_VERSION,
                    'artifact_sha256': PINNED_WHEEL_SHA256,
                    'expected_tools': ADAPTER_TOOLS(),
                    'adapter_env': child_environment({
                        key: self._env.get(key, '') for key in ADAPTER_ENV_VARS}),
                }, on_restart=self.on_client_restart)
                started = self._client.start()
                self._client_started = True
                if not started:
                    if not self._degraded_reason:
                        self.mark_degraded(
                            f'client: {getattr(self._client, "degraded_reason", "start failed")}')
                    raise CannanasUnavailable(self.availability_text())
                if self._client.read_only_degraded:
                    self._degraded_reason = (
                        'release verification failed: only reads allowed')
                    # Reads stay allowed in the read-only degraded state.
                    return self._client
                return self._client
            self._client_started = True
            if getattr(self._client, 'degraded', False):
                if not self._degraded_reason:
                    self.mark_degraded(f'client: {self._client.degraded_reason}')
                raise CannanasUnavailable(self.availability_text())
            if (getattr(self._client, 'read_only_degraded', False)
                    and (getattr(self, '_degraded_reason', '') == ''
                         or 'reads allowed' not in str(self._degraded_reason))):
                self._degraded_reason = 'release verification failed: only reads allowed'
            return self._client

    # ── plugin-facing flows ────────────────────────────────────────────────

    def call_read(self, action: str, params: dict) -> dict:
        """One read through the client; audit failure is non-fatal for reads."""
        tool, arguments, prefix = read_tool_call(action, params)
        if not self.available():
            raise CannanasUnavailable(self.availability_text())
        client = self.require_client()
        result = client.call(tool, arguments)
        exact = bool(result.get('exact', True))
        truncated = bool(result.get('truncated', False))
        payload = _record_payload(prefix, result, exact, truncated)
        if action == 'count_plants_by_room':
            room = str(result.get('room') or '')
            if room:
                count = result.get('plant_count')
                summary = f'Pflanzen im Raum {room}: {count}.'
            else:
                by_room = result.get('by_room') or {}
                total = result.get('total_plants')
                listing = ', '.join(f'{room}: {count}'
                                    for room, count in by_room.items())
                summary = (f'Pflanzen je Raum: {listing}.' if listing
                           else f'Insgesamt {total} Pflanzen.')
            payload['summary'] = summary
            payload['records'] = []
        self._audit_read(action)
        return payload

    def _audit_read(self, action: str) -> None:
        audit = self._audit
        if audit is None:
            return
        try:
            audit.record_read(
                'read', action=action, payload_hash='',
                stable_device_id='', auth_session_id='',
                user_turn_id=0, state='read',
                warn=lambda: print(
                    '[Cannanas] Prüfunterlage für Lesen nicht verfügbar '
                    '(einmalige Warnung).'))  # one bounded operator warning
        except CannanasAuditClosed:
            pass  # reads degrade, never fail JARVIS

    # ── prepare ────────────────────────────────────────────────────────────

    def prepare(self, action: str, params: dict, turn) -> dict:
        """Run the adapter prepare_*; store the pending; return the preview."""
        if not self.available():
            raise CannanasUnavailable(self.availability_text())
        if getattr(self._client, 'read_only_degraded', False) if self._client else False:
            raise CannanasUnavailable(
                'Cannanas-Schreibvorgänge sind deaktiviert: '
                'Release-Verifizierung fehlgeschlagen.')
        binding = prepare_binding_for(turn)
        if binding is None:
            raise CannanasUnavailable(
                'Cannanas-Änderungen sind nur für zugeordnete Geräte möglich.')
        tool, arguments = prepare_tool_call(action, params, binding)
        client = self.require_client()
        result = client.call(tool, arguments)
        club_id = str(self._env.get('CANNANAS_CLUB_ID') or 'club-bound')
        pending = PendingConfirmation(
            action=action.removeprefix('prepare_'),
            club_id=club_id,
            payload_hash=str(result.get('payload_hash') or ''),
            complete_preview=complete_preview_text(result.get('preview') or {}),
            jarvis_session_id=binding['jarvis_session_id'],
            stable_device_id=binding['stable_device_id'],
            auth_session_id=binding['auth_session_id'],
            prepared_turn_id=int(binding['prepared_turn_id']),
            adapter_boot_id=self._boot,
            created_at=time.monotonic(),
            expires_at=time.monotonic() + DEFAULT_PENDING_TTL_SECONDS,
            state='pending',
            opaque_mcp_token=OpaqueToken(str(result.get('confirmation_token') or '')),
        )
        self.coordinator.store(pending)
        audit = self._audit
        if audit is not None:
            try:
                audit.record(
                    'prepared', action=pending.action,
                    payload_hash=pending.payload_hash,
                    preview=pending.complete_preview,
                    stable_device_id=pending.stable_device_id,
                    auth_session_id=pending.auth_session_id,
                    user_turn_id=pending.prepared_turn_id,
                    state='pending', result_id='')
            except CannanasAuditClosed:
                # Nothing written yet: cancel instead of an unaccounted preview.
                self.coordinator.cancel(pending.jarvis_session_id)
                raise CannanasUnavailable(
                    'Cannanas-Schreibprüfung nicht verfügbar. '
                    'Bitte erneut versuchen.')
        preview = dict(result.get('preview') or {}) \
            if isinstance(result.get('preview') or {}, dict) else {}
        payload = {
            'ok': True,
            'summary': (f'Vorschau: {pending.complete_preview} '
                        f'{_CONFIRM_INSTRUCTION}'),
            'preview': preview,
            'requires_confirmation': True,
            'exact': bool(result.get('exact', True)),
        }
        return payload

    # ── host confirm flow (outside the model path) ─────────────────────────

    def evaluate(self, turn, text) -> dict:
        """Grammar check after a finalized turn; handled by the host only."""
        try:
            session_id = getattr(turn, 'stable_device_id', None) or ''
            pending = self.coordinator.current(session_id)
        except Exception:
            pending = None
        if turn is None or pending is None or pending.state != 'pending':
            return {'decision': 'none', 'spoken': '', 'handled': False}
        decision = confirmation_grammar(text)
        if decision == 'none':
            # Unrelated speech leaves the pending untouched (until TTL).
            return {'decision': 'none', 'spoken': '', 'handled': False}
        if decision == 'deny':
            self.coordinator.cancel(session_id)
            return {'decision': 'deny',
                    'spoken': 'Abgebrochen — die Vorschau wurde verworfen.',
                    'handled': True}
        outcome = self.confirm(turn)
        outcome['decision'] = 'affirm'
        return outcome

    def confirm(self, turn) -> dict:
        """Atomic consume → audit-before-POST → classify; never retried."""
        session_id = getattr(turn, 'stable_device_id', None) or ''
        pending = self.coordinator.current(session_id)
        expected = {
            'action': pending.action,
            'club_id': pending.club_id,
            'payload_hash': pending.payload_hash,
            'adapter_boot_id': pending.adapter_boot_id,
        } if pending is not None else {}
        executing, refusal = self.coordinator.consume(session_id, turn, expected)
        if executing is None:
            return {'decision': 'affirm',
                    'spoken': REFUSAL_SPOKEN.get(refusal, _NOT_CONFIRMABLE_SHORT),
                    'handled': True, 'refusal': refusal,
                    'classification': 'refused'}
        try:
            with self._audit.pre_post(
                    action=executing.action,
                    payload_hash=executing.payload_hash,
                    preview=executing.complete_preview,
                    stable_device_id=executing.stable_device_id,
                    auth_session_id=executing.auth_session_id,
                    user_turn_id=getattr(turn, 'user_turn_id', 0)) as finish:
                binding = {
                    'jarvis_session_id': executing.jarvis_session_id,
                    'stable_device_id': executing.stable_device_id,
                    'auth_session_id': executing.auth_session_id,
                    'prepared_turn_id': str(executing.prepared_turn_id),
                    'adapter_boot_id': executing.adapter_boot_id,
                }
                tool = f'confirm_{executing.action}'
                try:
                    result = self.require_client().call(
                        tool, {'confirmation_token': executing.opaque_mcp_token.get(),
                               'binding': binding})
                except CannanasPreSendRefusal:
                    # Refused before the transport: provably no bytes were sent.
                    finish('failed', '')
                    self.coordinator.mark_consumed(session_id)
                    return {'decision': 'affirm',
                            'spoken': ('Cannanas ist derzeit nicht verfügbar. '
                                       'Kein Eintrag vorgenommen. Bitte erneut vorbereiten.'),
                            'handled': True, 'classification': 'failed'}
                except Exception:
                    # Bytes may have been sent: never retried, stays unknown.
                    finish('unknown', '')
                    self.coordinator.mark_consumed(session_id)
                    return {'decision': 'affirm',
                            'spoken': ('Unklar — der Vorgang wird nicht wiederholt. '
                                       'Bitte in Cannanas kontrollieren.'),
                            'handled': True, 'classification': 'unknown'}
            classification, result_id, spoken = classify_write_result(result)
            finish(classification, result_id)
            self.coordinator.mark_consumed(session_id)
            return {'decision': 'affirm', 'spoken': spoken, 'handled': True,
                    'classification': classification, 'result_id': result_id}
        except CannanasAuditClosed:
            # The audit refused before any POST: the pending is spent.
            self.coordinator.mark_consumed(session_id)
            return {'decision': 'affirm',
                    'spoken': ('Cannanas-Schreibprüfung nicht verfügbar. '
                               'Kein Eintrag vorgenommen. Bitte erneut vorbereiten.'),
                    'handled': True, 'classification': 'failed'}


def classify_write_result(result) -> 'tuple[str, str, str]':
    """ok with id → succeeded; ok w/o id → created_but_unverified; else failed."""
    result = result if isinstance(result, dict) else {}
    result_id = str(result.get('result_id') or result.get('id')
                    or result.get('returned_object_id') or '')
    if result.get('ok'):
        if result_id:
            return ('succeeded', result_id, f'Erledigt — Objekt-ID {result_id}.')
        return ('created_but_unverified', '',
                'Der Vorgang war vermutlich erfolgreich, konnte aber nicht über '
                'die Objekt-ID bestätigt werden.')
    return ('failed', '', 'Der Eintrag ist fehlgeschlagen. '
            'Es wurde nichts wiederholt.')