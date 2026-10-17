"""In-memory bearer sessions and login attempt limiting for the dashboard."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SessionRecord:
    token: str
    session_key: str
    device_id: str | None
    created_at: float
    expires_at: float


class SessionRegistry:
    """One-hour, non-sliding bearer sessions kept only in process memory."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._records: dict[str, SessionRecord] = {}

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [token for token, record in self._records.items() if now >= record.expires_at]
        for token in expired:
            del self._records[token]

    def issue(self, device_id: str | None) -> SessionRecord:
        self._purge_expired()
        created_at = self._clock()
        record = SessionRecord(
            token=secrets.token_urlsafe(32),
            session_key=secrets.token_urlsafe(32),
            device_id=device_id,
            created_at=created_at,
            expires_at=created_at + self._ttl_seconds,
        )
        self._records[record.token] = record
        return record

    def authenticate(self, token: str) -> SessionRecord | None:
        self._purge_expired()
        return self._records.get(token) if isinstance(token, str) else None

    def revoke_device(self, device_id: str) -> set[str]:
        self._purge_expired()
        tokens = {
            token
            for token, record in self._records.items()
            if record.device_id == device_id
        }
        for token in tokens:
            del self._records[token]
        return tokens

    def revoke_transient(self) -> set[str]:
        self._purge_expired()
        tokens = {
            token
            for token, record in self._records.items()
            if record.device_id is None
        }
        for token in tokens:
            del self._records[token]
        return tokens

    def discard(self, token: str) -> None:
        self._records.pop(token, None)


class AttemptLimiter:
    """Fixed-window process-local attempt limiter keyed by trusted buckets."""

    def __init__(
        self,
        limit: int = 10,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._attempts: dict[str, list[float]] = {}

    def allow(self, bucket: str) -> bool:
        now = self._clock()
        attempts = [
            at
            for at in self._attempts.get(bucket, [])
            if at > now - self._window_seconds
        ]
        self._attempts[bucket] = attempts
        if len(attempts) >= self._limit:
            return False
        attempts.append(now)
        return True
