"""RED/GREEN tests for the private Spotify OAuth store (Task 1)."""
import base64
import hashlib
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

from core.spotify import (
    OAuthRecord,
    OAuthStore,
    REQUIRED_SCOPES,
    REDIRECT_URI,
    SpotifyConfigError,
    TARGET_DEVICE_NAME,
    SpotifyOAuth,
    AuthorizationStart,
    AuthorizationStatus,
)
from urllib.parse import parse_qs, urlsplit


ACCESS_SECRET = "access-secret-7d21c5"
REFRESH_SECRET = "refresh-secret-2e199a"


def valid_store_data(**overrides):
    data = {
        "client_id": "SpotifyClientID",
        "authorized_at": "2026-08-31T12:00:00+00:00",
        "authorization_expires_at": "2027-02-28T12:00:00+00:00",
        "access_token": ACCESS_SECRET,
        "access_expires_at": "2026-09-01T12:00:00+00:00",
        "refresh_token": REFRESH_SECRET,
        "scopes": list(REQUIRED_SCOPES),
    }
    data.update(overrides)
    return data


def authorized_record(authorized_at="2026-08-31T12:00:00Z"):
    start = datetime.fromisoformat(authorized_at.replace("Z", "+00:00"))
    return OAuthRecord(
        client_id="SpotifyClientID",
        authorized_at=start,
        authorization_expires_at=datetime(2027, 2, 28, 12, 0, tzinfo=timezone.utc),
        access_token=ACCESS_SECRET,
        access_expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        refresh_token=REFRESH_SECRET,
        scopes=(
            "user-read-playback-state",
            "user-read-currently-playing",
            "user-modify-playback-state",
        ),
    )


def write_store(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


class SpotifyOAuthStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg_dir = Path(self.tmp.name) / "config"
        self.cfg_dir.mkdir()
        self.store_path = self.cfg_dir / "spotify_oauth.json"

    def test_constants_are_exact(self):
        self.assertEqual(
            REDIRECT_URI,
            "https://lacasa-server.tail7a9c2.ts.net/api/spotify/callback",
        )
        self.assertEqual(TARGET_DEVICE_NAME, "raspotify (lacasa-server)")
        self.assertEqual(
            REQUIRED_SCOPES,
            (
                "user-read-playback-state",
                "user-read-currently-playing",
                "user-modify-playback-state",
            ),
        )

    def test_store_round_trip_is_private_and_six_calendar_months(self):
        store = OAuthStore(self.store_path)
        store.save(authorized_record())
        loaded = store.load()
        self.assertEqual(
            loaded.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(self.store_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("client_secret", self.store_path.read_text())
        self.assertEqual(loaded.client_id, "SpotifyClientID")

    def test_missing_file_is_not_authorized(self):
        self.assertIsNone(OAuthStore(self.store_path).load())

    def test_unsafe_mode_fails_closed(self):
        write_store(self.store_path, {})
        os.chmod(self.store_path, 0o644)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_symlinked_store_fails_closed(self):
        real = self.cfg_dir / "real.json"
        real.write_text("{}")
        os.symlink(real, self.store_path)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_broken_store_symlink_fails_closed(self):
        self.store_path.symlink_to(self.cfg_dir / "missing.json")
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_symlinked_ancestor_with_private_store_fails_closed(self):
        real_dir = Path(self.tmp.name) / "realdir"
        real_dir.mkdir()
        link_dir = self.cfg_dir.parent / "linkdir"
        link_dir.symlink_to(real_dir)
        bad_path = link_dir / "config" / "spotify_oauth.json"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        write_store(bad_path, valid_store_data())
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(bad_path).load()

    def test_directory_at_store_path_maps_to_config_error(self):
        self.store_path.mkdir()
        os.chmod(self.store_path, 0o600)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_fifo_at_store_path_fails_closed_without_blocking(self):
        os.mkfifo(self.store_path, 0o600)
        os.chmod(self.store_path, 0o600)
        probe = """
import sys
from pathlib import Path
from core.spotify import OAuthStore, SpotifyConfigError

try:
    OAuthStore(Path(sys.argv[1])).load()
except SpotifyConfigError:
    raise SystemExit(0)
except Exception as exc:
    print(type(exc).__name__, file=sys.stderr)
    raise SystemExit(2)
raise SystemExit(3)
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", probe, str(self.store_path)],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("loading a FIFO blocked instead of raising SpotifyConfigError")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_foreign_file_owner_fails_closed(self):
        write_store(self.store_path, valid_store_data())
        real_fstat = os.fstat

        def foreign_regular_file(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                values = list(result)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return result

        with mock.patch("core.spotify.os.fstat", side_effect=foreign_regular_file):
            with self.assertRaises(SpotifyConfigError):
                OAuthStore(self.store_path).load()

    def test_load_uses_opened_inode_when_path_is_swapped_after_validation(self):
        write_store(self.store_path, valid_store_data())
        malicious = self.cfg_dir / "malicious.json"
        write_store(malicious, valid_store_data(client_id="AttackerClientID"))
        displaced = self.cfg_dir / "displaced.json"
        real_islink = os.path.islink
        real_fstat = os.fstat
        swapped = False

        def swap_path():
            nonlocal swapped
            if not swapped:
                self.store_path.rename(displaced)
                self.store_path.symlink_to(malicious)
                swapped = True

        def racing_islink(path):
            result = real_islink(path)
            if Path(path) == self.store_path:
                swap_path()
            return result

        def racing_fstat(fd):
            result = real_fstat(fd)
            if stat.S_ISREG(result.st_mode):
                swap_path()
            return result

        with mock.patch("core.spotify.os.path.islink", side_effect=racing_islink), mock.patch(
            "core.spotify.os.fstat", side_effect=racing_fstat
        ):
            loaded = OAuthStore(self.store_path).load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.client_id, "SpotifyClientID")

    def test_save_rejects_existing_destination_symlink(self):
        real = self.cfg_dir / "real.json"
        real.write_text("sentinel")
        self.store_path.symlink_to(real)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).save(authorized_record())
        self.assertTrue(self.store_path.is_symlink())
        self.assertEqual(real.read_text(), "sentinel")

    def test_corrupt_json_fails_closed(self):
        self.store_path.write_text("{not json")
        os.chmod(self.store_path, 0o600)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_invalid_timestamps_fails_closed(self):
        data = {
            "client_id": "SpotifyClientID",
            "authorized_at": "2026-13-99T12:00:00Z",
            "authorization_expires_at": "2027-02-28T12:00:00+00:00",
            "access_token": "old-access",
            "access_expires_at": "2026-09-01T12:00:00+00:00",
            "refresh_token": "old-refresh",
            "scopes": list(REQUIRED_SCOPES),
        }
        write_store(self.store_path, data)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_load_requires_every_exact_field(self):
        for field_name in valid_store_data():
            with self.subTest(field_name=field_name):
                data = valid_store_data()
                del data[field_name]
                write_store(self.store_path, data)
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_unknown_fields(self):
        write_store(self.store_path, valid_store_data(client_secret="must-not-be-here"))
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_load_rejects_wrong_field_types_as_config_errors(self):
        wrong_values = {
            "client_id": 123,
            "authorized_at": 123,
            "authorization_expires_at": [],
            "access_token": 123,
            "access_expires_at": {},
            "refresh_token": ["token"],
            "scopes": [list(REQUIRED_SCOPES)],
        }
        for field_name, wrong_value in wrong_values.items():
            with self.subTest(field_name=field_name):
                write_store(self.store_path, valid_store_data(**{field_name: wrong_value}))
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_non_utc_offsets(self):
        cases = (
            {
                "authorized_at": "2026-08-31T13:00:00+01:00",
                "authorization_expires_at": "2027-02-28T13:00:00+01:00",
            },
            {"access_expires_at": "2026-09-01T13:00:00+01:00"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                write_store(self.store_path, valid_store_data(**overrides))
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).load()

    def test_load_rejects_duplicate_scopes(self):
        duplicate_scopes = list(REQUIRED_SCOPES) + [REQUIRED_SCOPES[0]]
        write_store(self.store_path, valid_store_data(scopes=duplicate_scopes))
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_save_validates_record_types_utc_and_six_month_deadline(self):
        plus_one = timezone(timedelta(hours=1))
        invalid_records = (
            "not-an-oauth-record",
            replace(authorized_record(), access_token=123),
            replace(
                authorized_record(),
                authorized_at=datetime(2026, 8, 31, 13, 0, tzinfo=plus_one),
            ),
            replace(
                authorized_record(),
                authorization_expires_at=datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc),
            ),
            replace(authorized_record(), scopes=list(REQUIRED_SCOPES)),
        )
        for record in invalid_records:
            with self.subTest(record=record):
                self.store_path.unlink(missing_ok=True)
                with self.assertRaises(SpotifyConfigError):
                    OAuthStore(self.store_path).save(record)  # type: ignore[arg-type]
                self.assertFalse(self.store_path.exists())

    def test_config_errors_and_logs_never_include_token_values(self):
        write_store(self.store_path, valid_store_data(authorized_at=ACCESS_SECRET))
        log_output = StringIO()
        handler = logging.StreamHandler(log_output)
        spotify_logger = logging.getLogger("core.spotify")
        old_level = spotify_logger.level
        spotify_logger.setLevel(logging.DEBUG)
        handler.setLevel(logging.DEBUG)
        spotify_logger.addHandler(handler)
        self.addCleanup(spotify_logger.removeHandler, handler)
        self.addCleanup(spotify_logger.setLevel, old_level)

        with self.assertRaises(SpotifyConfigError) as raised:
            OAuthStore(self.store_path).load()

        combined = f"{raised.exception}\n{log_output.getvalue()}"
        self.assertNotIn(ACCESS_SECRET, combined)
        self.assertNotIn(REFRESH_SECRET, combined)

    def test_missing_required_scopes_fails_closed(self):
        data = {
            "client_id": "SpotifyClientID",
            "authorized_at": "2026-08-31T12:00:00Z",
            "authorization_expires_at": "2027-02-28T12:00:00+00:00",
            "access_token": "old-access",
            "access_expires_at": "2026-09-01T12:00:00+00:00",
            "refresh_token": "old-refresh",
            "scopes": ["user-read-playback-state"],
        }
        write_store(self.store_path, data)
        with self.assertRaises(SpotifyConfigError):
            OAuthStore(self.store_path).load()

    def test_invalidation_write_error_propagates_and_preserves_old_file(self):
        store = OAuthStore(self.store_path)
        store.save(authorized_record())

        with mock.patch("os.replace", side_effect=OSError("write failed")):
            with self.assertRaises(OSError) as raised:
                store.invalidate_tokens()

        self.assertNotIn(ACCESS_SECRET, str(raised.exception))
        self.assertNotIn(REFRESH_SECRET, str(raised.exception))
        loaded = store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.access_token, ACCESS_SECRET)
        self.assertEqual(loaded.refresh_token, REFRESH_SECRET)
        self.assertEqual(loaded.client_id, "SpotifyClientID")
        self.assertEqual(
            loaded.authorization_expires_at.isoformat(),
            "2027-02-28T12:00:00+00:00",
        )
        self.assertEqual(loaded.scopes, REQUIRED_SCOPES)

    def test_invalidation_clears_only_token_fields(self):
        store = OAuthStore(self.store_path)
        original = authorized_record()
        store.save(original)

        result = store.invalidate_tokens()

        self.assertIsNotNone(result)
        cleared = store.load()
        self.assertIsNotNone(cleared)
        assert cleared is not None
        self.assertIsNone(cleared.access_token)
        self.assertIsNone(cleared.refresh_token)
        self.assertIsNone(cleared.access_expires_at)
        self.assertEqual(cleared.client_id, original.client_id)
        self.assertEqual(cleared.authorized_at, original.authorized_at)
        self.assertEqual(
            cleared.authorization_expires_at,
            original.authorization_expires_at,
        )
        self.assertEqual(cleared.scopes, original.scopes)

    def test_invalidation_missing_store_returns_none(self):
        self.assertIsNone(OAuthStore(self.store_path).invalidate_tokens())


class SpotifyOAuthTests(unittest.TestCase):
    """PKCE flow, one-use callback, and status (Task 2)."""

    UNRESERVED = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    )
    FAKE_VERIFIER = "A" * 43
    FAKE_STATE = "B" * 43

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OAuthStore(Path(self.tmp.name) / "spotify_oauth.json")
        self.base_time = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        self.clock = Mock(return_value=self.base_time)
        self.request = Mock()
        self.oauth = SpotifyOAuth(self.store, request=self.request, clock=self.clock)

    def _fake_token_response(self, status_code=200, json_data=None, raise_exc=None):
        resp = Mock()
        resp.status_code = status_code
        if raise_exc:
            resp.json.side_effect = raise_exc
        else:
            resp.json.return_value = json_data or {}
        return resp

    def _setup_start_with_fake_secrets(self, client_id="SpotifyClientID"):
        tokens = iter([self.FAKE_VERIFIER, self.FAKE_STATE])
        with mock.patch(
            "core.spotify.secrets.token_urlsafe", side_effect=lambda n: next(tokens)
        ):
            return self.oauth.start_authorization(client_id)

    def test_start_authorization_builds_exact_pkce_url(self):
        start = self._setup_start_with_fake_secrets()
        params = parse_qs(urlsplit(start.url).query)
        self.assertEqual(
            urlsplit(start.url)._replace(query="").geturl(),
            "https://accounts.spotify.com/authorize",
        )
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["client_id"], ["SpotifyClientID"])
        self.assertEqual(params["redirect_uri"], [REDIRECT_URI])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["scope"], [" ".join(REQUIRED_SCOPES)])
        self.assertEqual(
            params["code_challenge"],
            [
                base64.urlsafe_b64encode(
                    hashlib.sha256(self.FAKE_VERIFIER.encode("ascii")).digest()
                )
                .rstrip(b"=")
                .decode("ascii")
            ],
        )
        self.assertNotIn("client_secret", params)
        self.assertNotIn("code_verifier", params)
        self.assertEqual(params["state"], [self.FAKE_STATE])
        self.assertTrue(
            all(ch in self.UNRESERVED for ch in self.FAKE_VERIFIER)
        )
        self.assertTrue(43 <= len(self.FAKE_VERIFIER) <= 128)
        self.assertEqual(
            start.state_expires_at,
            self.base_time + timedelta(seconds=600),
        )
        self.assertFalse(self.store.load())

    def test_start_authorization_rejects_invalid_client_ids(self):
        for bad in ("", " " * 10, "x" * 129, "tab\there", "new\nline", "ctrl\x00", "x\x7fy"):
            with self.subTest(client_id=repr(bad)):
                with self.assertRaises(ValueError):
                    self.oauth.start_authorization(bad)

    def test_start_authorization_preserves_client_id(self):
        self._setup_start_with_fake_secrets(client_id="MyExactId!@#")
        saved = self.oauth._pending[self.FAKE_STATE]
        self.assertEqual(saved[0], "MyExactId!@#")

    def test_complete_authorization_exchanges_code_and_saves_record(self):
        token_response = {
            "access_token": ACCESS_SECRET,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": REFRESH_SECRET,
            "scope": " ".join(REQUIRED_SCOPES),
        }
        self.request.return_value = self._fake_token_response(200, token_response)
        self._setup_start_with_fake_secrets()
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code="the-auth-code")
        self.assertTrue(status.authorized)
        self.assertFalse(status.reauthorization_required)
        self.assertEqual(status.authorized_at, self.base_time)
        self.assertEqual(
            status.authorization_expires_at,
            datetime(2027, 2, 28, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(status.access_expires_at, datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc))
        self.request.assert_called_once()
        call = self.request.call_args
        self.assertEqual(call.args[0], "POST")
        self.assertEqual(call.args[1], "https://accounts.spotify.com/api/token")
        self.assertNotIn("auth", call.kwargs)
        form = call.kwargs["data"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "the-auth-code")
        self.assertEqual(form["redirect_uri"], REDIRECT_URI)
        self.assertEqual(form["client_id"], "SpotifyClientID")
        self.assertEqual(form["code_verifier"], self.FAKE_VERIFIER)
        self.assertNotIn("client_secret", form)
        record = self.store.load()
        self.assertEqual(record.access_token, ACCESS_SECRET)
        self.assertEqual(record.refresh_token, REFRESH_SECRET)
        self.assertEqual(record.scopes, REQUIRED_SCOPES)

    def test_complete_authorization_missing_state_no_exchange(self):
        status = self.oauth.complete_authorization(state=None, code="code")
        self.assertFalse(status.authorized)
        self.request.assert_not_called()

    def test_complete_authorization_wrong_state_no_exchange(self):
        status = self.oauth.complete_authorization(state="wrong", code="code")
        self.assertFalse(status.authorized)
        self.request.assert_not_called()

    def test_complete_authorization_stale_state_no_exchange(self):
        self._setup_start_with_fake_secrets()
        self.clock.return_value = self.base_time + timedelta(seconds=600)
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.assertFalse(status.authorized)
        self.request.assert_not_called()

    def test_complete_authorization_replay_no_second_exchange(self):
        token_response = {"access_token": ACCESS_SECRET, "expires_in": 3600,
                          "refresh_token": REFRESH_SECRET,
                          "scope": " ".join(REQUIRED_SCOPES)}
        self.request.return_value = self._fake_token_response(200, token_response)
        self._setup_start_with_fake_secrets()
        self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.request.assert_called_once()

    def test_complete_authorization_missing_code_consumes_state_no_exchange(self):
        self._setup_start_with_fake_secrets()
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code=None)
        self.assertFalse(status.authorized)
        self.request.assert_not_called()

    def test_complete_authorization_access_denied_consumes_state_no_exchange(self):
        self._setup_start_with_fake_secrets()
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, error="access_denied")
        self.assertFalse(status.authorized)
        self.request.assert_not_called()

    def test_complete_authorization_malformed_token_json_no_save(self):
        self._setup_start_with_fake_secrets()
        self.request.return_value = self._fake_token_response(200, {"bad": True})
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.assertFalse(status.authorized)
        self.assertIsNone(self.store.load())

    def test_complete_authorization_missing_scope_no_save(self):
        self._setup_start_with_fake_secrets()
        self.request.return_value = self._fake_token_response(200, {
            "access_token": ACCESS_SECRET, "expires_in": 3600,
            "scope": "user-read-playback-state",
        })
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.assertFalse(status.authorized)
        self.assertIsNone(self.store.load())

    def test_complete_authorization_token_endpoint_failure_no_save(self):
        self._setup_start_with_fake_secrets()
        self.request.return_value = self._fake_token_response(500)
        status = self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.assertFalse(status.authorized)
        self.assertIsNone(self.store.load())

    def test_complete_authorization_one_state_one_exchange_concurrency(self):
        token_response = {"access_token": ACCESS_SECRET, "expires_in": 3600,
                          "refresh_token": REFRESH_SECRET,
                          "scope": " ".join(REQUIRED_SCOPES)}
        self.request.return_value = self._fake_token_response(200, token_response)
        self._setup_start_with_fake_secrets()
        barrier = threading.Barrier(2)
        results = []
        def worker():
            barrier.wait()
            results.append(self.oauth.complete_authorization(state=self.FAKE_STATE, code="code"))
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.request.assert_called_once()
        self.assertEqual(self.store.load().access_token, ACCESS_SECRET)
        self.assertEqual(sum(1 for s in results if s.authorized), 1)

    def test_status_returns_none_when_unauthorized(self):
        status = self.oauth.status()
        self.assertFalse(status.authorized)
        self.assertTrue(status.reauthorization_required)
        self.assertIsNone(status.authorized_at)

    def test_status_reports_expired_authorization(self):
        token_response = {"access_token": ACCESS_SECRET, "expires_in": 3600,
                          "refresh_token": REFRESH_SECRET,
                          "scope": " ".join(REQUIRED_SCOPES)}
        self.request.return_value = self._fake_token_response(200, token_response)
        self._setup_start_with_fake_secrets()
        self.oauth.complete_authorization(state=self.FAKE_STATE, code="code")
        self.clock.return_value = datetime(2027, 3, 1, 12, 0, tzinfo=timezone.utc)
        status = self.oauth.status()
        self.assertFalse(status.authorized)
        self.assertTrue(status.reauthorization_required)


class SpotifyDefaultOAuthTests(unittest.TestCase):
    def test_default_oauth_lazy_singleton(self):
        from core.spotify import default_oauth
        a = default_oauth()
        b = default_oauth()
        self.assertIs(a, b)
        self.assertIsInstance(a, SpotifyOAuth)


if __name__ == "__main__":
    unittest.main()
