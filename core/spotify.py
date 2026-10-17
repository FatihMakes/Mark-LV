"""Private Spotify OAuth store with six-month authorization model."""
from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REDIRECT_URI = "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback"
TARGET_DEVICE_NAME = "raspotify (lacasa-server)"
REQUIRED_SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
)


class SpotifyConfigError(ValueError):
    """Permanent store configuration failure; headless maps to exit 78."""


class SpotifyAuthorizationRequired(RuntimeError):
    """Authorization data missing or expired."""


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise SpotifyConfigError("timestamp must be a string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SpotifyConfigError("invalid UTC timestamp") from None
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise SpotifyConfigError("timestamp must use UTC")
    return dt


def _six_calendar_months(start: datetime) -> datetime:
    months = start.month + 6
    year = start.year + (months - 1) // 12
    month = (months - 1) % 12 + 1
    import calendar
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class OAuthRecord:
    client_id: str
    authorized_at: datetime
    authorization_expires_at: datetime
    access_token: str | None = None
    access_expires_at: datetime | None = None
    refresh_token: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)


_RECORD_FIELDS = frozenset(
    {
        "client_id",
        "authorized_at",
        "authorization_expires_at",
        "access_token",
        "access_expires_at",
        "refresh_token",
        "scopes",
    }
)


def _require_utc_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SpotifyConfigError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SpotifyConfigError(f"{field_name} must use UTC")
    return value


def _validate_record(record: object) -> OAuthRecord:
    if not isinstance(record, OAuthRecord):
        raise SpotifyConfigError("record must be an OAuthRecord")
    if not isinstance(record.client_id, str) or not record.client_id:
        raise SpotifyConfigError("client_id must be a non-empty string")

    authorized_at = _require_utc_datetime(record.authorized_at, "authorized_at")
    authorization_expires_at = _require_utc_datetime(
        record.authorization_expires_at, "authorization_expires_at"
    )
    if authorization_expires_at != _six_calendar_months(authorized_at):
        raise SpotifyConfigError("authorization_expires_at does not match six-month deadline")

    for field_name, value in (
        ("access_token", record.access_token),
        ("refresh_token", record.refresh_token),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise SpotifyConfigError(f"{field_name} must be a non-empty string or null")
    if record.access_expires_at is not None:
        _require_utc_datetime(record.access_expires_at, "access_expires_at")
    if not isinstance(record.scopes, tuple) or record.scopes != REQUIRED_SCOPES:
        raise SpotifyConfigError("scopes do not exactly match required scopes")
    return record


def _record_from_data(data: object) -> OAuthRecord:
    if not isinstance(data, dict):
        raise SpotifyConfigError("Spotify OAuth store is not an object")
    if set(data) != _RECORD_FIELDS:
        raise SpotifyConfigError("Spotify OAuth store fields do not match the required schema")

    client_id = data["client_id"]
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    raw_scopes = data["scopes"]
    if not isinstance(client_id, str) or not client_id:
        raise SpotifyConfigError("client_id must be a non-empty string")
    for field_name, value in (
        ("access_token", access_token),
        ("refresh_token", refresh_token),
    ):
        if value is not None and (not isinstance(value, str) or not value):
            raise SpotifyConfigError(f"{field_name} must be a non-empty string or null")
    if not isinstance(raw_scopes, list) or not all(isinstance(scope, str) for scope in raw_scopes):
        raise SpotifyConfigError("scopes must be a list of strings")

    raw_access_expires_at = data["access_expires_at"]
    access_expires_at = None
    if raw_access_expires_at is not None:
        access_expires_at = _parse_utc(raw_access_expires_at)
    return _validate_record(
        OAuthRecord(
            client_id=client_id,
            authorized_at=_parse_utc(data["authorized_at"]),
            authorization_expires_at=_parse_utc(data["authorization_expires_at"]),
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            scopes=tuple(raw_scopes),
        )
    )


class OAuthStore:
    def __init__(self, path: Path = Path(__file__).resolve().parents[1] / "config" / "spotify_oauth.json"):
        self.path = Path(path)

    @staticmethod
    def _directory_flags() -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise SpotifyConfigError("secure no-follow filesystem access is unavailable")
        return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW

    @staticmethod
    def _validate_parent_stat(parent_stat: os.stat_result) -> None:
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise SpotifyConfigError("Spotify OAuth store parent is not a directory")
        if parent_stat.st_uid != os.getuid():
            raise SpotifyConfigError("Spotify OAuth store parent has a foreign owner")
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise SpotifyConfigError("Spotify OAuth store parent has unsafe permissions")

    @staticmethod
    def _validate_store_stat(store_stat: os.stat_result) -> None:
        if not stat.S_ISREG(store_stat.st_mode):
            raise SpotifyConfigError("Spotify OAuth store is not a regular file")
        if store_stat.st_uid != os.getuid():
            raise SpotifyConfigError("Spotify OAuth store has a foreign owner")
        if stat.S_IMODE(store_stat.st_mode) != 0o600:
            raise SpotifyConfigError("Spotify OAuth store must have mode 0600")

    def _open_parent(self, *, create: bool) -> tuple[int | None, str]:
        absolute = Path(os.path.abspath(os.fspath(self.path)))
        filename = absolute.name
        if not filename:
            raise SpotifyConfigError("Spotify OAuth store path has no filename")

        flags = self._directory_flags()
        try:
            current_fd = os.open(absolute.anchor, flags)
        except OSError:
            raise SpotifyConfigError("cannot open Spotify OAuth store root") from None

        try:
            for component in absolute.parent.parts[1:]:
                try:
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        os.close(current_fd)
                        return None, filename
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    next_fd = os.open(component, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            self._validate_parent_stat(os.fstat(current_fd))
            return current_fd, filename
        except SpotifyConfigError:
            os.close(current_fd)
            raise
        except OSError:
            os.close(current_fd)
            raise SpotifyConfigError("unsafe Spotify OAuth store path") from None

    def load(self) -> OAuthRecord | None:
        parent_fd, filename = self._open_parent(create=False)
        if parent_fd is None:
            return None
        try:
            try:
                file_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return None
            except OSError:
                raise SpotifyConfigError("cannot safely open Spotify OAuth store") from None
            try:
                self._validate_store_stat(os.fstat(file_fd))
                with os.fdopen(file_fd, "r", encoding="utf-8") as store_file:
                    file_fd = -1
                    data = json.load(store_file)
            except SpotifyConfigError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                raise SpotifyConfigError("malformed Spotify OAuth store") from None
            finally:
                if file_fd != -1:
                    os.close(file_fd)
        finally:
            os.close(parent_fd)
        return _record_from_data(data)

    def save(self, record: OAuthRecord) -> None:
        record = _validate_record(record)
        data = {
            "client_id": record.client_id,
            "authorized_at": record.authorized_at.isoformat(),
            "authorization_expires_at": record.authorization_expires_at.isoformat(),
            "access_token": record.access_token,
            "access_expires_at": record.access_expires_at.isoformat() if record.access_expires_at else None,
            "refresh_token": record.refresh_token,
            "scopes": list(record.scopes),
        }
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
        parent_fd, filename = self._open_parent(create=True)
        assert parent_fd is not None
        temp_name = None
        try:
            try:
                existing = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            except OSError:
                raise SpotifyConfigError("cannot inspect Spotify OAuth store") from None
            else:
                self._validate_store_stat(existing)

            for _ in range(128):
                candidate = f".spotify-oauth-{secrets.token_hex(12)}.tmp"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=parent_fd,
                    )
                except FileExistsError:
                    continue
                temp_name = candidate
                break
            else:
                raise FileExistsError("could not allocate Spotify OAuth temporary file")

            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(
                temp_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = None
            os.fsync(parent_fd)
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)

    def invalidate_tokens(self) -> OAuthRecord | None:
        record = self.load()
        if record is None:
            return None
        cleared = OAuthRecord(
            client_id=record.client_id,
            authorized_at=record.authorized_at,
            authorization_expires_at=record.authorization_expires_at,
            access_token=None,
            access_expires_at=None,
            refresh_token=None,
            scopes=record.scopes,
        )
        self.save(cleared)
        return cleared


# ── PKCE authorization flow (Task 2) ────────────────────────────────────────────

import base64
import hashlib
import requests
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode


TOKEN_ENDPOINT = "https://accounts.spotify.com/api/token"
AUTHORIZE_ENDPOINT = "https://accounts.spotify.com/authorize"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def no_secret_logger(message: str) -> None:
    logger.debug("%s", message)


@dataclass(frozen=True)
class AuthorizationStart:
    url: str
    state_expires_at: datetime


@dataclass(frozen=True)
class AuthorizationStatus:
    authorized: bool
    reauthorization_required: bool
    authorized_at: datetime | None
    authorization_expires_at: datetime | None
    access_expires_at: datetime | None


def _validate_client_id(client_id: object) -> str:
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("client_id must be a non-empty string")
    if len(client_id) > 128:
        raise ValueError("client_id must be at most 128 characters")
    for ch in client_id:
        if ch.isspace() or ord(ch) < 32 or ord(ch) == 127:
            raise ValueError("client_id must be printable with no whitespace or control characters")
    return client_id


class SpotifyOAuth:
    def __init__(
        self,
        store: OAuthStore,
        request: Callable = requests.request,
        clock: Callable[[], datetime] = utc_now,
        pending_ttl_seconds: int = 600,
        logger: Callable[[str], None] = no_secret_logger,
    ):
        self._store = store
        self._request = request
        self._clock = clock
        self._pending_ttl_seconds = pending_ttl_seconds
        self._logger = logger
        self._pending: dict[str, tuple[str, str, datetime]] = {}
        self._lock = threading.Lock()
        self._access_token_lock = threading.Lock()

    def start_authorization(self, client_id: str) -> AuthorizationStart:
        client_id = _validate_client_id(client_id)
        now = self._clock()
        verifier = secrets.token_urlsafe(32).rstrip("=")
        state = secrets.token_urlsafe(16)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        with self._lock:
            self._pending[state] = (client_id, verifier, now)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": " ".join(REQUIRED_SCOPES),
            "state": state,
        }
        url = AUTHORIZE_ENDPOINT + "?" + urlencode(params)
        self._logger("starting spotify authorization for " + client_id)
        return AuthorizationStart(url=url, state_expires_at=now + timedelta(seconds=self._pending_ttl_seconds))

    def complete_authorization(self, *, state: str | None, code: str | None = None, error: str | None = None) -> AuthorizationStatus:
        now = self._clock()
        record = self._store.load()
        if not state:
            return self._status(record, now)
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None:
            return self._status(record, now)
        client_id, verifier, created_at = pending
        if now >= created_at + timedelta(seconds=self._pending_ttl_seconds):
            return self._status(record, now)
        if error:
            return self._status(record, now)
        if not code:
            return self._status(record, now)
        try:
            new_record = self._exchange_token(client_id, code, verifier, now)
        except SpotifyAuthorizationRequired:
            return self._status(record, now)
        self._store.save(new_record)
        return self._status(new_record, now)

    def status(self) -> AuthorizationStatus:
        now = self._clock()
        record = self._store.load()
        return self._status(record, now)

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._access_token_lock:
            return self._access_token_internal(force_refresh=force_refresh)

    def _access_token_internal(self, *, force_refresh: bool = False) -> str:
        record = self._store.load()
        if record is None:
            raise SpotifyAuthorizationRequired("no authorized record")
        now = self._clock()
        if now >= record.authorization_expires_at:
            self._store.invalidate_tokens()
            raise SpotifyAuthorizationRequired("authorization expired (six-month deadline)")
        if record.access_token is None:
            raise SpotifyAuthorizationRequired("no access token")
        if not force_refresh and record.access_expires_at is not None:
            if now < record.access_expires_at - timedelta(seconds=60):
                return record.access_token
        return self._refresh_token(record, now)

    def _refresh_token(self, record: OAuthRecord, now: datetime) -> str:
        if record.refresh_token is None:
            raise SpotifyAuthorizationRequired("no refresh token")
        try:
            new_record = self._do_refresh(record.client_id, record.refresh_token, now)
        except SpotifyAuthorizationRequired:
            # Permanent authorization failure: only SpotifyAuthorizationRequired
            # escapes _do_refresh, and only for invalid_grant / revocation /
            # deadline. Transient failures surface as SpotifyAPIError instead.
            self._store.invalidate_tokens()
            raise
        self._store.save(new_record)
        return new_record.access_token or record.access_token or ""

    @staticmethod
    def _safe_json(response) -> object | None:
        try:
            return response.json()
        except Exception:
            return None

    def _do_refresh(self, client_id: str, refresh_token: str, now: datetime) -> OAuthRecord:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        try:
            response = self._request(
                "POST", TOKEN_ENDPOINT, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            # Transient: never invalidates the stored authorization.
            raise SpotifyAPIError("network", "spotify token refresh unavailable") from exc
        if response.status_code != 200:
            if response.status_code == 400:
                body = self._safe_json(response) or {}
                if not isinstance(body, dict):
                    body = {}
                if body.get("error") == "invalid_grant":
                    raise SpotifyAuthorizationRequired("refresh token invalid")
                raise SpotifyAPIError("bad_response", "refresh request rejected")
            if response.status_code == 429:
                raise SpotifyAPIError("rate_limited", "spotify token refresh rate limited")
            raise SpotifyAPIError("bad_response", f"refresh failed: {response.status_code}")
        try:
            body = response.json()
        except Exception as exc:
            raise SpotifyAPIError("bad_response", "malformed refresh response") from exc
        if not isinstance(body, dict) or "access_token" not in body:
            raise SpotifyAPIError("bad_response", "malformed refresh response")
        access_token = body["access_token"]
        expires_in = body.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in < 0:
            expires_in = 3600
        new_refresh = body.get("refresh_token")
        returned_scopes = set(body.get("scope", "").split()) if isinstance(body.get("scope"), str) else set()
        scopes = REQUIRED_SCOPES if set(REQUIRED_SCOPES).issubset(returned_scopes) else record.scopes
        return OAuthRecord(
            client_id=client_id,
            authorized_at=record.authorized_at,
            authorization_expires_at=record.authorization_expires_at,
            access_token=access_token,
            access_expires_at=now + timedelta(seconds=float(expires_in)),
            refresh_token=new_refresh if new_refresh else refresh_token,
            scopes=scopes,
        )

    def _status(self, record: OAuthRecord | None, now: datetime) -> AuthorizationStatus:
        if record is None:
            return AuthorizationStatus(False, True, None, None, None)
        reauth = now >= record.authorization_expires_at
        if not reauth:
            # Reflect reality after invalidation: a record without tokens can
            # no longer authorize requests even though the deadline is open.
            has_valid_access = record.access_token is not None and (
                record.access_expires_at is None
                or now < record.access_expires_at - timedelta(seconds=60)
            )
            if not (has_valid_access or record.refresh_token is not None):
                reauth = True
        return AuthorizationStatus(
            not reauth, reauth,
            record.authorized_at, record.authorization_expires_at, record.access_expires_at,
        )

    def _exchange_token(self, client_id: str, code: str, verifier: str, now: datetime) -> OAuthRecord:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        }
        response = self._request(
            "POST", TOKEN_ENDPOINT, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if response.status_code != 200:
            raise SpotifyAuthorizationRequired("token endpoint returned non-200")
        try:
            body = response.json()
        except Exception as exc:
            raise SpotifyAuthorizationRequired("malformed token response") from exc
        if not isinstance(body, dict) or "access_token" not in body:
            raise SpotifyAuthorizationRequired("malformed token response")
        access_token = body["access_token"]
        expires_in = body.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in < 0:
            raise SpotifyAuthorizationRequired("malformed token response")
        refresh_token = body.get("refresh_token")
        raw_scopes = body.get("scope", "")
        returned_scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
        if not set(REQUIRED_SCOPES).issubset(returned_scopes):
            raise SpotifyAuthorizationRequired("missing required scope")
        return OAuthRecord(
            client_id=client_id,
            authorized_at=now,
            authorization_expires_at=_six_calendar_months(now),
            access_token=access_token,
            access_expires_at=now + timedelta(seconds=float(expires_in)),
            refresh_token=refresh_token,
            scopes=REQUIRED_SCOPES,
        )


_default_oauth: SpotifyOAuth | None = None
_default_lock = threading.Lock()


def default_oauth() -> SpotifyOAuth:
    global _default_oauth
    if _default_oauth is None:
        with _default_lock:
            if _default_oauth is None:
                _default_oauth = SpotifyOAuth(OAuthStore())
    return _default_oauth


# ── Token access and HTTP API client (Task 3) ────────────────────────────────

class SpotifyAPIError(RuntimeError):
    """Non-secret error category for Spotify Web API failures."""

    def __init__(self, category: str, message: str = ""):
        self.category = category
        super().__init__(message or category)


class SpotifyHTTP:
    """Bounded-retry HTTP client for the Spotify Web API."""

    BASE_URL = "https://api.spotify.com/v1"

    def __init__(
        self,
        oauth: SpotifyOAuth,
        request: Callable = requests.request,
        sleep: Callable[[float], None] = lambda s: time.sleep(s),
        timeout_seconds: float = 10.0,
        max_retry_after_seconds: float = 5.0,
    ):
        self._oauth = oauth
        self._request = request
        self._sleep = sleep
        self._timeout = timeout_seconds
        self._max_retry_after = max_retry_after_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> requests.Response:
        url = self.BASE_URL + path
        headers = {"Authorization": f"Bearer {self._oauth.access_token()}"}
        opts = {"timeout": self._timeout, "allow_redirects": False}
        if params:
            opts["params"] = params
        if json_body is not None:
            opts["json"] = json_body
            headers["Content-Type"] = "application/json"

        did_auth_retry = False
        did_rate_retry = False
        attempts = 0

        while attempts < 3:
            attempts += 1
            try:
                response = self._request(method, url, headers=headers, **opts)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise SpotifyAPIError("network", str(exc)) from exc

            if response.status_code == 200 or response.status_code == 204:
                return response

            if response.status_code == 401:
                if did_auth_retry:
                    # Already retried exactly once after a forced refresh: the
                    # request stays unauthorized (permanent).
                    raise SpotifyAPIError("unauthorized")
                did_auth_retry = True
                token = self._oauth.access_token(force_refresh=True)
                headers = {"Authorization": f"Bearer {token}"}
                continue

            if response.status_code == 403:
                reason = self._classify_403(response)
                raise SpotifyAPIError(reason)

            if response.status_code == 429 and not did_rate_retry:
                retry_after = self._parse_retry_after(response)
                if retry_after is not None and not did_rate_retry:
                    did_rate_retry = True
                    self._sleep(retry_after)
                    continue
                raise SpotifyAPIError("rate_limited")

            if response.status_code >= 500:
                raise SpotifyAPIError("bad_response", f"server error {response.status_code}")

            raise SpotifyAPIError("bad_response", f"unexpected status {response.status_code}")

        raise SpotifyAPIError("bad_response", "too many retry attempts")

    @staticmethod
    def _classify_403(response: requests.Response) -> str:
        text = response.text.lower()
        if "premium" in text or "premium required" in text:
            return "premium"
        if "no active device found" in text:
            return "no_device"
        return "premium"

    @staticmethod
    def _parse_retry_after(response: requests.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (ValueError, TypeError):
            return None
        if value < 0:
            return None
        return min(value, 5.0)


# ── Exact receiver resolution and voice-control actions (Task 4) ──────────────

@dataclass(frozen=True)
class SpotifyDevice:
    id: str
    name: str
    restricted: bool
    supports_volume: bool


class SpotifyController:
    """Controls playback on the exact configured LaCasa receiver."""
    def __init__(self, http: SpotifyHTTP, target_name: str = TARGET_DEVICE_NAME):
        self._http = http
        self._target_name = target_name
    def resolve_device(self, *, require_volume: bool = False) -> SpotifyDevice:
        resp = self._http.request("GET", "/me/player/devices")
        try:
            data = resp.json()
        except Exception as exc:
            raise SpotifyAPIError("bad_response", "malformed devices response") from exc
        if not isinstance(data, dict):
            raise SpotifyAPIError("bad_response", "malformed devices response")
        raw_devices = data.get("devices")
        if not isinstance(raw_devices, list):
            raise SpotifyAPIError("bad_response", "malformed devices payload")
        matches: list[SpotifyDevice] = []
        for item in raw_devices:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            dev_id = item.get("id")
            if name == self._target_name:
                if not isinstance(dev_id, str) or not dev_id:
                    raise SpotifyAPIError("no_device", f"device '{self._target_name}' has invalid ID")
                restricted = bool(item.get("is_restricted", False))
                if restricted:
                    raise SpotifyAPIError("no_device", f"device '{self._target_name}' is restricted")
                supports_volume = bool(item.get("supports_volume", False))
                if require_volume and not supports_volume:
                    raise SpotifyAPIError("no_device", f"device '{self._target_name}' does not support volume control")
                matches.append(SpotifyDevice(id=dev_id, name=name, restricted=restricted, supports_volume=supports_volume))
        if len(matches) == 0:
            raise SpotifyAPIError("no_device", f"Empfänger '{self._target_name}' nicht gefunden")
        if len(matches) > 1:
            raise SpotifyAPIError("no_device", f"Mehrere Empfänger mit Name '{self._target_name}' gefunden")
        return matches[0]
    def perform(
        self,
        action: str,
        *,
        query: str | None = None,
        kind: str = "track",
        volume_percent: int | None = None,
    ) -> str:
        try:
            return self._perform_internal(action, query=query, kind=kind, volume_percent=volume_percent)
        except SpotifyAuthorizationRequired:
            # Authorization is genuinely gone (invalidated/expired): the voice
            # path must request reconnection in German and never leak internals.
            return (
                "Spotify muss einmal verbunden werden. "
                "Bitte im Dashboard einmal neu autorisieren."
            )
        except SpotifyAPIError as err:
            return self._format_error(err)
    def _perform_internal(
        self,
        action: str,
        *,
        query: str | None = None,
        kind: str = "track",
        volume_percent: int | None = None,
    ) -> str:
        if action == "play":
            if not query or not isinstance(query, str) or not query.strip():
                raise SpotifyAPIError("query_required")
            if kind not in ("track", "album", "artist"):
                raise SpotifyAPIError("kind_invalid")
            search_resp = self._http.request("GET", f"/search", params={"q": query.strip(), "type": kind, "limit": 3})
            try:
                search_data = search_resp.json()
            except Exception as exc:
                raise SpotifyAPIError("bad_response", "malformed search response") from exc
            key = kind + "s"
            container = search_data.get(key, {}) if isinstance(search_data, dict) else {}
            items = container.get("items", []) if isinstance(container, dict) else []
            if not items or not isinstance(items, list):
                return f"Nichts gefunden für '{query.strip()}'."
            first = items[0]
            if not isinstance(first, dict) or not first.get("uri"):
                return f"Nichts gefunden für '{query.strip()}'."
            uri = first["uri"]
            device = self.resolve_device()
            if kind == "track":
                play_body = {"uris": [uri]}
            else:
                play_body = {"context_uri": uri}
            self._http.request("PUT", "/me/player/play", params={"device_id": device.id}, json_body=play_body)
            return f"Spielt {kind} '{query.strip()}' auf {self._target_name}."
        elif action == "pause":
            device = self.resolve_device()
            self._http.request("PUT", "/me/player/pause", params={"device_id": device.id})
            return f"Wiedergabe auf {self._target_name} pausiert."
        elif action == "resume":
            device = self.resolve_device()
            self._http.request("PUT", "/me/player/play", params={"device_id": device.id})
            return f"Wiedergabe auf {self._target_name} fortgesetzt."
        elif action == "next":
            device = self.resolve_device()
            self._http.request("POST", "/me/player/next", params={"device_id": device.id})
            return f"Nächster Titel auf {self._target_name}."
        elif action == "previous":
            device = self.resolve_device()
            self._http.request("POST", "/me/player/previous", params={"device_id": device.id})
            return f"Vorheriger Titel auf {self._target_name}."
        elif action == "volume":
            if volume_percent is None or type(volume_percent) is not int or not (0 <= volume_percent <= 100):
                raise SpotifyAPIError("volume_invalid")
            device = self.resolve_device(require_volume=True)
            self._http.request("PUT", "/me/player/volume", params={"device_id": device.id, "volume_percent": volume_percent})
            return f"Lautstärke auf {self._target_name} auf {volume_percent}% gesetzt."
        elif action == "current":
            device = self.resolve_device()
            resp = self._http.request("GET", "/me/player")
            if resp.status_code == 204 or not resp.text:
                return f"Auf {self._target_name} läuft zurzeit nichts."
            try:
                data = resp.json()
            except Exception:
                # Fail closed: a malformed player response means "nothing
                # playing" for the voice path, never a leaked server payload.
                return f"Auf {self._target_name} läuft zurzeit nichts."
            if not isinstance(data, dict):
                return f"Auf {self._target_name} läuft zurzeit nichts."
            curr_device = data.get("device", {})
            curr_dev_id = curr_device.get("id") if isinstance(curr_device, dict) else None
            if curr_dev_id != device.id:
                return f"Auf {self._target_name} läuft zurzeit nichts."
            if not data.get("is_playing"):
                return f"Auf {self._target_name} läuft zurzeit nichts."
            item = data.get("item")
            if not isinstance(item, dict) or not item.get("name"):
                return f"Auf {self._target_name} läuft zurzeit nichts."
            track_name = item.get("name", "")
            artists_raw = item.get("artists", [])
            artist_names = []
            if isinstance(artists_raw, list):
                for a in artists_raw:
                    if isinstance(a, dict) and a.get("name"):
                        artist_names.append(a["name"])
            artist_str = ", ".join(artist_names) if artist_names else "unbekannter Künstler"
            return f"Auf {self._target_name} läuft gerade '{track_name}' von {artist_str}."
        else:
            raise SpotifyAPIError("action_unknown")
    def _format_error(self, err: SpotifyAPIError) -> str:
        cat = err.category
        if cat == "unauthorized":
            return "Spotify-Autorisierung abgelaufen oder ungültig. Bitte einmal neu autorisieren."
        elif cat == "no_device":
            return f"Empfänger '{self._target_name}' ist zurzeit nicht aktiv oder erreichbar."
        elif cat == "premium":
            return "Spotify Premium wird für diese Funktion benötigt."
        elif cat == "rate_limited":
            return "Zu viele Anfragen an Spotify. Bitte kurz warten."
        elif cat == "network":
            return "Spotify API ist zurzeit nicht erreichbar."
        elif cat == "query_required":
            return "query (Suchbegriff) ist für 'play' erforderlich."
        elif cat == "kind_invalid":
            return "kind (Inhaltstyp) muss track, album oder artist sein."
        elif cat == "volume_invalid":
            return "volume_percent muss eine ganze Zahl zwischen 0 und 100 sein."
        elif cat == "action_unknown":
            return "Unbekannte Spotify-Aktion."
        else:
            # Never interpolate the internal message: server text could echo
            # hostile or secret-bearing content into the spoken result.
            return f"Spotify-Fehler ({cat})."
_default_controller: SpotifyController | None = None
_default_controller_lock = threading.Lock()
def default_controller() -> SpotifyController:
    global _default_controller
    if _default_controller is None:
        with _default_controller_lock:
            if _default_controller is None:
                _default_controller = SpotifyController(SpotifyHTTP(default_oauth()))
    return _default_controller
