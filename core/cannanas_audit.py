"""Fail-closed Cannanas host audit: append-only JSONL, 0640, fsynced pre-POST.

Secret-free: every entry is fixed-schema and pseudonymous (stable device id,
auth-session digest, user-turn id, action, payload hash, complete preview,
state, returned object id). Bearer values, MCP confirmation tokens, durable
phone secrets, Gemini session material and raw API responses are never
recorded — the API refuses anything not in its named parameters.

Writes: the `confirmed` transition is durably committed BEFORE the MCP confirm
call runs; when the audit is unavailable the write fails closed (zero POSTs).
Reads: audit failure is non-fatal and produces exactly one bounded operator
warning. The host audit is additional to the adapter's own audit.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import stat
import threading

FILE_MODE = 0o640
DIR_MODE = 0o700

EVENT_PREPARED = 'prepared'
EVENT_CONFIRMED = 'confirmed'
EVENT_RESULT = 'result'
EVENT_READ = 'read'
EVENT_CANCELLED = 'cancelled'
EVENTS = frozenset({EVENT_PREPARED, EVENT_CONFIRMED, EVENT_RESULT, EVENT_READ, EVENT_CANCELLED})

READ_AUDIT_DEGRADED_WARNING = 'Cannanas-Audit für Lesen nicht verfügbar: nur Protokoll-Ausfall'


class CannanasAuditClosed(RuntimeError):
    """The audit sink is unavailable: writes fail closed before any POST."""


class _Finisher:
    """One post-transition writer per confirmed write."""

    def __init__(self, audit: 'CannanasAudit', entry: dict):
        self._audit = audit
        self._entry = dict(entry)
        self._done = False

    def __call__(self, state: str, result_id: str = '') -> None:
        if self._done:
            raise RuntimeError('write transition already recorded')
        self._done = True
        self._audit.record(EVENT_RESULT, action=self._entry['action'],
                           payload_hash=self._entry['payload_hash'],
                           preview=self._entry['preview'],
                           stable_device_id=self._entry['device_id'],
                           auth_session_id=self._entry['auth_session_id'],
                           user_turn_id=self._entry['user_turn_id'],
                           state=state, result_id=result_id)


class CannanasAudit:
    """Append-only audit sink; opening fails closed on unsafe targets."""

    def __init__(self, path) -> None:
        self._path = os.fspath(path)
        self._lock = threading.Lock()
        self._warned = False

    # ── record ─────────────────────────────────────────────────────────────

    def record(self, event: str, *, action, payload_hash, preview,
               stable_device_id, auth_session_id, user_turn_id,
               state, result_id) -> bool:
        """Durably commit one entry; raises CannanasAuditClosed on failure."""
        if event not in EVENTS:
            raise ValueError('unknown audit event')
        entry = {
            'event': event,
            'timestamp': _timestamp(),
            'device_id': str(stable_device_id or ''),
            'auth_session_id': str(auth_session_id or ''),
            'user_turn_id': int(user_turn_id or 0),
            'action': str(action or ''),
            'payload_hash': str(payload_hash or ''),
            'preview': str(preview or ''),
            'state': str(state or ''),
            'result_id': str(result_id or ''),
        }
        line = (json_line(entry) + '\n').encode('utf-8')
        with self._lock:
            self._write(line)
        return True

    def record_read(self, event: str, *, action, payload_hash, stable_device_id,
                    auth_session_id, user_turn_id, state, result_id='',
                    warn=None) -> bool:
        """Non-fatal audit for reads: exactly one bounded operator warning."""
        try:
            return self.record(event, action=action, payload_hash=payload_hash,
                               preview='', stable_device_id=stable_device_id,
                               auth_session_id=auth_session_id,
                               user_turn_id=user_turn_id, state=state,
                               result_id=result_id)
        except CannanasAuditClosed:
            with self._lock:
                first = not self._warned
                self._warned = True
            if first and warn is not None:
                warn()
            return False

    # ── write protocol ─────────────────────────────────────────────────────

    @contextlib.contextmanager
    def pre_post(self, *, action, payload_hash, preview, stable_device_id,
                 auth_session_id, user_turn_id):
        """Durably commit the `confirmed` transition before the confirm call.

        The contextmanager body IS the confirm call — by the time the caller
        runs inside `with`, the audit entry is already fsynced. If the audit
        cannot commit, nothing is yielded and no POST can happen.
        """
        self.record(EVENT_CONFIRMED, action=action, payload_hash=payload_hash,
                    preview=preview, stable_device_id=stable_device_id,
                    auth_session_id=auth_session_id, user_turn_id=user_turn_id,
                    state='executing', result_id='')
        yield _Finisher(self, {
            'action': action, 'payload_hash': payload_hash, 'preview': preview,
            'device_id': stable_device_id, 'auth_session_id': auth_session_id,
            'user_turn_id': user_turn_id,
        })

    # ── sink handling ──────────────────────────────────────────────────────

    def _write(self, line: bytes) -> None:
        path = self._path
        try:
            self._ensure_dir()
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                         FILE_MODE)
        except OSError as exc:
            raise _closed(str(exc.errno or ''), exc) from None
        try:
            st = os.fstat(fd)
            mode = stat.S_IMODE(st.st_mode)
            if not stat.S_ISREG(st.st_mode):
                raise _closed('not-regular')
            if mode != FILE_MODE:
                raise _closed(f'file-mode-{oct(mode)}')
            os.write(fd, line)
            os.fsync(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _ensure_dir(self) -> None:
        path = os.path.dirname(os.path.abspath(self._path))
        if os.path.isdir(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != DIR_MODE:
                raise _closed(f'dir-mode-{oct(mode)}')
            return
        raise _closed('audit-directory-missing')


def _closed(reason: str, exc: BaseException | None = None) -> CannanasAuditClosed:
    detail = f' ({reason})' if reason else ''
    error = CannanasAuditClosed(f'Cannanas-Schreibprüfung nicht verfügbar{detail}')
    if exc is not None:
        error.__cause__ = exc
    return error


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def json_line(entry: dict) -> str:
    import json
    return json.dumps(entry, separators=(',', ':'), ensure_ascii=False)