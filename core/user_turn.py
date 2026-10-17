"""Finalized user-turn identity: immutable turn context for tool dispatch."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from core.turn_provenance import IngressEnvelope


@dataclass(frozen=True)
class FinalizedTurn:
    envelope: IngressEnvelope
    finalized_at: float
    identity: str

    @property
    def user_turn_id(self) -> int:
        return self.envelope.user_turn_id

    @property
    def channel(self) -> str:
        return self.envelope.channel

    @property
    def stable_device_id(self) -> str | None:
        return self.envelope.stable_device_id

    @property
    def auth_session_id(self) -> str:
        return self.envelope.auth_session_id

    @property
    def capture_id(self) -> str | None:
        return self.envelope.capture_id


class TurnRegistry:
    """Tracks the currently finalized turn per stream, in process memory only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, FinalizedTurn] = {}

    def register(self, finalized_turn: FinalizedTurn) -> FinalizedTurn:
        with self._lock:
            self._current[finalized_turn.channel] = finalized_turn
        return finalized_turn

    def current(self, channel: str) -> FinalizedTurn | None:
        with self._lock:
            return self._current.get(channel)

    def clear(self) -> None:
        with self._lock:
            self._current.clear()


turn_registry = TurnRegistry()



def finalize_turn(envelope: IngressEnvelope, *,
                  registry: TurnRegistry | None = None,
                  clock=time.monotonic) -> FinalizedTurn:
    """Pin a completed, final ingress envelope as the finalized user turn.

    The turn identity is the envelope's own user turn id and never changes
    afterwards; a response generation never creates or finalizes a turn.
    """
    from core.turn_provenance import IngressEnvelope as _Envelope  # local import

    if not isinstance(envelope, _Envelope) or not envelope.final:
        # Fail closed: only a completed ingress turn becomes user-turn identity.
        raise ValueError('Only a final ingress envelope finalizes a user turn')
    turn = FinalizedTurn(
        envelope=envelope,
        finalized_at=clock(),
        identity=f'{envelope.channel}:{envelope.user_turn_id}',
    )
    (registry or turn_registry).register(turn)
    return turn