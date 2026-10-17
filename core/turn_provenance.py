"""Immutable ingress provenance identity for every accepted user input.

Each ingress source (dashboard HTTP text, control-WebSocket text, phone PCM)
attaches one frozen envelope carrying the durable stable-device identity that
owns the bearer session, a pseudonymous auth-session identity, the audio
capture identity (the existing epoch/capture tuple), and the finalized
user-turn id. The envelope never carries bearer or device secrets: the
auth-session id is a one-way digest of the bearer session's key, and the
durable device id is the identifier the store already uses.
"""
from __future__ import annotations

import hashlib
import threading
import dataclasses
from dataclasses import dataclass

CHANNEL_HTTP_TEXT = 'http_text'
CHANNEL_CONTROL_WS_TEXT = 'control_ws_text'
CHANNEL_PHONE_PCM = 'phone_pcm'
CHANNELS = frozenset((CHANNEL_HTTP_TEXT, CHANNEL_CONTROL_WS_TEXT, CHANNEL_PHONE_PCM))


def auth_session_id_of(session_key: str) -> str:
    """Pseudonymous auth-session identity, derived one-way from the record key."""
    return hashlib.sha256(str(session_key).encode('utf-8')).hexdigest()


class TurnIdentity:
    """Monotonic user-turn identity issued once per accepted ingress turn."""

    def __init__(self, *, first: int = 1) -> None:
        self._lock = threading.Lock()
        self._last = first - 1

    def issue(self) -> int:
        with self._lock:
            self._last += 1
            return self._last

    def current(self) -> int:
        with self._lock:
            return self._last


_TURN_IDENTITY = TurnIdentity()


@dataclass(frozen=True)
class IngressEnvelope:
    stable_device_id: str | None
    auth_session_id: str
    capture_id: str | None
    user_turn_id: int
    channel: str
    final: bool = False


def create_stable(channel: str, *, stable_device_id: str | None,
                  auth_session_id: str, capture_id: str | None = None,
                  final: bool = False, identity: TurnIdentity | None = None
                  ) -> IngressEnvelope:
    """Stamp a fresh envelope with a new monotonically increasing user turn id."""
    issuer = identity or _TURN_IDENTITY
    return IngressEnvelope(
        stable_device_id=stable_device_id,
        auth_session_id=auth_session_id,
        capture_id=capture_id,
        user_turn_id=issuer.issue(),
        channel=channel,
        final=final,
    )


def final_variant(envelope: IngressEnvelope) -> IngressEnvelope:
    """The same turn identity, marked final (e.g. the PCM stream-end frame)."""
    return dataclasses.replace(envelope, final=True)


def validate(envelope: object, *, known_devices: 'frozenset[str] | set[str] | None' = None,
             active_auth_sessions: 'set[str] | frozenset[str] | None' = None) -> bool:
    """Fail closed on mixed, revoked, superseded or unknown provenance sources.

    Without the optional sets the check is purely structural; with them it also
    rejects revoked/unknown devices and superseded auth sessions.
    """
    if not isinstance(envelope, IngressEnvelope):
        return False
    if envelope.channel not in CHANNELS:
        return False
    if not isinstance(envelope.auth_session_id, str) or not envelope.auth_session_id:
        return False
    if not isinstance(envelope.user_turn_id, int) or isinstance(envelope.user_turn_id, bool):
        return False
    if envelope.user_turn_id < 1:
        return False
    if not isinstance(envelope.stable_device_id, (str, type(None))):
        return False
    if not isinstance(envelope.capture_id, (str, type(None))):
        return False
    if not isinstance(envelope.final, bool):
        return False
    if envelope.channel == CHANNEL_PHONE_PCM and (not envelope.capture_id
                                                  or not envelope.stable_device_id):
        # A durable device identity without its auth session is likewise mixed.
        return False
    if envelope.stable_device_id and not envelope.auth_session_id:
        return False
    if known_devices is not None:
        if envelope.stable_device_id is None or envelope.stable_device_id not in known_devices:
            return False
    if active_auth_sessions is not None and envelope.auth_session_id not in active_auth_sessions:
        return False
    return True


class ProvenanceText(str):
    """Command text that carries its immutable ingress envelope unchanged:
    it still equals and serializes as plain text for every existing consumer."""

    __slots__ = ('envelope',)

    def __new__(cls, text: str, envelope: IngressEnvelope) -> 'ProvenanceText':
        obj = str.__new__(cls, text)
        obj.envelope = envelope
        return obj