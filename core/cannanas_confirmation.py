"""Single owner of pending Cannanas confirmations (host-side, in-memory).

One pending action per JARVIS session; a new prepare cancels the previous.
Consumption is an atomic compare-and-swap `pending → executing` that happens
BEFORE any adapter confirm call, so concurrent or replayed confirmations
produce exactly one executor and zero duplicate POSTs. Everything is process
memory only: a JARVIS restart fences by construction, and an adapter restart
is fenced by an explicit invalidation hook. Confirmation tokens never reach
the model and never serialize.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

MAX_PENDING_TTL_SECONDS = 600.0
DEFAULT_PENDING_TTL_SECONDS = 300.0   # five minutes

REFUSAL_NONE = ''
REFUSAL_WRONG_SESSION = 'wrong_session'
REFUSAL_WRONG_DEVICE = 'wrong_device'
REFUSAL_WRONG_AUTH_SESSION = 'wrong_auth_session'
REFUSAL_WRONG_TURN = 'wrong_turn'
REFUSAL_WRONG_BOOT = 'wrong_boot'
REFUSAL_WRONG_PAYLOAD = 'wrong_payload'
REFUSAL_EXPIRED = 'expired'
REFUSAL_ALREADY_USED = 'already_used'
REFUSAL_CANCELLED = 'cancelled'

PENDING_STATE = 'pending'
EXECUTING_STATE = 'executing'
CONSUMED_STATE = 'consumed'
EXPIRED_STATE = 'expired'
CANCELLED_STATE = 'cancelled'


class OpaqueToken:
    """Repr-redacted MCP confirmation token; host-only state, never serialized."""

    __slots__ = ('_value',)

    def __init__(self, value: str) -> None:
        self._value = str(value)

    def get(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return 'OpaqueToken(<redacted>)'

    def __str__(self) -> str:
        return '<redacted>'

    def __eq__(self, other) -> bool:
        if not isinstance(other, OpaqueToken):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class PendingConfirmation:
    action: str                       # e.g. 'plant_create'
    club_id: str
    payload_hash: str
    complete_preview: str
    jarvis_session_id: str
    stable_device_id: str
    auth_session_id: str
    prepared_turn_id: int
    adapter_boot_id: str
    created_at: float
    expires_at: float
    state: str
    opaque_mcp_token: OpaqueToken

    def __post_init__(self) -> None:
        if self.expires_at - self.created_at > MAX_PENDING_TTL_SECONDS:
            raise ValueError('pending confirmation TTL exceeds the 600 s cap')
        if self.state not in (PENDING_STATE, EXECUTING_STATE, CONSUMED_STATE,
                              EXPIRED_STATE, CANCELLED_STATE):
            raise ValueError('unknown pending state')
        if not isinstance(self.opaque_mcp_token, OpaqueToken):
            raise TypeError('opaque_mcp_token must be an OpaqueToken')


class ConfirmationCoordinator:
    """Thread-safe owner of pending confirmations; no network imports."""

    def __init__(self, *, clock=time.monotonic,
                 ttl_seconds: float = DEFAULT_PENDING_TTL_SECONDS) -> None:
        self._clock = clock
        self._ttl = min(float(ttl_seconds), MAX_PENDING_TTL_SECONDS)
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.RLock()

    # ── owner API ──────────────────────────────────────────────────────────

    def store(self, pending: PendingConfirmation) -> PendingConfirmation:
        """Store the new pending; a previous pending for the session is cancelled."""
        with self._lock:
            previous = self._pending.get(pending.jarvis_session_id)
            if previous is not None:
                self._pending[pending.jarvis_session_id] = _cancel(previous)
            self._pending[pending.jarvis_session_id] = pending
            return pending

    def consume(self, jarvis_session_id: str, turn_context, expected: dict
                ) -> 'tuple[PendingConfirmation | None, str]':
        """Atomic pending → executing compare-and-swap before the adapter call.

        Returns (pending, '') on success or (None, refusal). Replays and races
        lose the CAS; there is exactly one executor per pending.
        """
        with self._lock:
            pending = self._pending.get(jarvis_session_id)
            if pending is None:
                return None, REFUSAL_WRONG_SESSION
            now = self._clock()
            if pending.state == PENDING_STATE and now >= pending.expires_at:
                pending = _stamp(pending, EXPIRED_STATE)
                self._pending[jarvis_session_id] = pending
                return None, REFUSAL_EXPIRED
            if pending.state == CANCELLED_STATE:
                return None, REFUSAL_CANCELLED
            if pending.state in (EXPIRED_STATE, CONSUMED_STATE, EXECUTING_STATE):
                return None, REFUSAL_ALREADY_USED
            expected = expected or {}
            if pending.adapter_boot_id != str(expected.get('adapter_boot_id', '')):
                return None, REFUSAL_WRONG_BOOT
            if (pending.action != str(expected.get('action', ''))
                    or pending.club_id != str(expected.get('club_id', ''))
                    or pending.payload_hash != str(expected.get('payload_hash', ''))):
                return None, REFUSAL_WRONG_PAYLOAD
            if turn_context is None:
                return None, REFUSAL_WRONG_TURN
            turn_id = getattr(turn_context, 'user_turn_id', None)
            if not isinstance(turn_id, int) or turn_id <= pending.prepared_turn_id:
                # Same-turn assent and pre-prepare turns can never confirm.
                return None, REFUSAL_WRONG_TURN
            turn_env = getattr(turn_context, 'envelope', None)
            device = getattr(turn_env, 'stable_device_id', None)
            auth = getattr(turn_env, 'auth_session_id', None)
            if device != pending.stable_device_id:
                return None, REFUSAL_WRONG_DEVICE
            if auth != pending.auth_session_id:
                return None, REFUSAL_WRONG_AUTH_SESSION
            executing = _stamp(pending, EXECUTING_STATE)
            self._pending[jarvis_session_id] = executing
            return executing, REFUSAL_NONE

    def mark_consumed(self, jarvis_session_id: str) -> PendingConfirmation | None:
        with self._lock:
            pending = self._pending.get(jarvis_session_id)
            if pending is None:
                return None
            consumed = _stamp(pending, CONSUMED_STATE)
            # The consumed record stays as a replay fence: any further consume
            # for the session is refused with already_used, and the next store
            # replaces it.
            self._pending[jarvis_session_id] = consumed
            return consumed

    def cancel(self, jarvis_session_id: str) -> bool:
        with self._lock:
            pending = self._pending.get(jarvis_session_id)
            if pending is None or pending.state != PENDING_STATE:
                return False
            self._pending[jarvis_session_id] = _cancel(pending)
            return True

    def current(self, jarvis_session_id: str) -> PendingConfirmation | None:
        with self._lock:
            pending = self._pending.get(jarvis_session_id)
            if pending is None:
                return None
            if pending.state == PENDING_STATE and self._clock() >= pending.expires_at:
                expired = _stamp(pending, EXPIRED_STATE)
                self._pending[jarvis_session_id] = expired
                return expired
            return pending

    def invalidate_all_on_adapter_restart(self) -> None:
        """Adapter-restart fence: every pending becomes unconsumable."""
        with self._lock:
            for session_id, pending in list(self._pending.items()):
                if pending.state in (PENDING_STATE, EXECUTING_STATE):
                    self._pending[session_id] = _cancel(pending)

    def reset(self) -> None:
        """Test helper: empty the whole coordinator (JARVIS restart simulation)."""
        with self._lock:
            self._pending.clear()


def _stamp(pending: PendingConfirmation, state: str, *,
           created_at: float | None = None,
           expires_at: float | None = None) -> PendingConfirmation:
    import dataclasses
    changes: dict = {'state': state}
    if created_at is not None:
        changes['created_at'] = created_at
    if expires_at is not None:
        changes['expires_at'] = expires_at
    return dataclasses.replace(pending, **changes)


def _cancel(pending: PendingConfirmation) -> PendingConfirmation:
    return _stamp(pending, CANCELLED_STATE)