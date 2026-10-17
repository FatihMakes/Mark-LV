"""Tests for SpotifyAPIError, SpotifyHTTP, and token refresh (Task 3)."""
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

from core.spotify import (
    OAuthRecord,
    OAuthStore,
    SpotifyAPIError,
    SpotifyController,
    SpotifyHTTP,
    SpotifyOAuth,
    SpotifyAuthorizationRequired,
    REQUIRED_SCOPES,
    utc_now,
)


def _make_record(**overrides):
    # Use a future date so the six-month window is still open at test runtime
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    defaults = {
        "client_id": "test-client",
        "authorized_at": now,
        "authorization_expires_at": _six_months_from(now),
        "access_token": "old-token",
        "access_expires_at": now + timedelta(hours=2),
        "refresh_token": "old-refresh",
        "scopes": REQUIRED_SCOPES,
    }
    defaults.update(overrides)
    return OAuthRecord(**defaults)


def _six_months_from(dt):
    import calendar
    months = dt.month + 6
    year = dt.year + (months - 1) // 12
    month = (months - 1) % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


class SpotifyAPIErrorTests(unittest.TestCase):
    def test_category_attribute(self):
        err = SpotifyAPIError("premium", "needs premium")
        self.assertEqual(err.category, "premium")
        self.assertIn("premium", str(err))

    def test_default_message(self):
        err = SpotifyAPIError("network")
        self.assertEqual(err.category, "network")
        self.assertEqual(str(err), "network")


class TokenRefreshTests(unittest.TestCase):
    def setUp(self):
        self.store = MagicMock(spec=OAuthStore)

    def test_access_token_returns_cached_when_not_expiring(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_expires_at=now + timedelta(hours=2),
        )
        oauth = SpotifyOAuth(self.store, clock=lambda: now)
        with patch.object(oauth._store, 'load', return_value=record):
            token = oauth.access_token()
        self.assertEqual(token, "old-token")

    def test_access_token_forces_refresh_when_flag_set(self):
        record = _make_record()
        new_record = _make_record(access_token="new-token")
        oauth = SpotifyOAuth(self.store)

        def mock_load():
            return record
        def mock_save(rec):
            pass

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token(force_refresh=True)
        self.assertEqual(token, "new-token")

    def test_access_token_raises_when_no_record(self):
        oauth = SpotifyOAuth(self.store)
        with patch.object(oauth._store, 'load', return_value=None):
            with self.assertRaises(SpotifyAuthorizationRequired):
                oauth.access_token()

    def test_access_token_raises_when_auth_expired(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=now - timedelta(days=1),
        )
        oauth = SpotifyOAuth(self.store)
        with patch.object(oauth._store, 'load', return_value=record):
            with patch.object(oauth._store, 'invalidate_tokens') as mock_inv:
                with self.assertRaises(SpotifyAuthorizationRequired):
                    oauth.access_token()
                mock_inv.assert_called_once()

    def test_refresh_rotates_token(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            access_token="rotated-token",
            refresh_token="new-refresh",
            access_expires_at=now + timedelta(hours=1),
        )
        oauth = SpotifyOAuth(self.store)

        load_count = [0]
        def mock_load():
            load_count[0] += 1
            return record if load_count[0] == 1 else new_record

        saved = []
        def mock_save(rec):
            saved.append(rec)

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token()
        self.assertEqual(token, "rotated-token")
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].access_token, "rotated-token")

    def test_refresh_preserves_old_refresh_when_not_rotated(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        record = _make_record(
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            access_token="new-token",
            refresh_token=None,
            access_expires_at=now + timedelta(hours=1),
        )
        oauth = SpotifyOAuth(self.store)

        def mock_load():
            return record
        def mock_save(rec):
            pass

        with patch.object(oauth._store, 'load', side_effect=mock_load):
            with patch.object(oauth._store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', return_value=new_record):
                    token = oauth.access_token()
        self.assertEqual(token, "new-token")


class SpotifyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.oauth = MagicMock(spec=SpotifyOAuth)
        self.oauth.access_token.return_value = "test-token"

    def _make_client(self, **kwargs):
        defaults = {
            'oauth': self.oauth,
            'request': lambda method, url, **opts: self._mock_response(200),
            'sleep': lambda s: None,
        }
        defaults.update(kwargs)
        return SpotifyHTTP(**defaults)

    def _mock_response(self, status, text="", headers=None):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = headers or {}
        try:
            resp.json.return_value = json.loads(text) if text else {}
        except json.JSONDecodeError:
            resp.json.return_value = {}
        return resp

    def test_success_returns_response(self):
        client = self._make_client()
        resp = client.request("GET", "/me")
        self.assertEqual(resp.status_code, 200)

    def test_403_premium(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(
                403, '{"error": {"message": "Premium required"}}'
            )
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("PUT", "/me/player/pause")
        self.assertEqual(ctx.exception.category, "premium")

    def test_403_no_device(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(
                403, '{"error": {"message": "No active device found"}}'
            )
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("PUT", "/me/player/pause")
        self.assertEqual(ctx.exception.category, "no_device")

    def test_429_retries_once(self):
        responses = [
            self._mock_response(429, headers={"Retry-After": "1"}),
            self._mock_response(200),
        ]
        call_count = [0]
        def mock_request(*a, **k):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx] if idx < len(responses) else self._mock_response(500)

        sleeps = []
        client = self._make_client(request=mock_request, sleep=lambda s: sleeps.append(s))
        resp = client.request("GET", "/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0], 1.0)

    def test_429_invalid_retry_after_no_retry(self):
        client = self._make_client(
            request=lambda *a, **k: self._mock_response(429, headers={"Retry-After": "invalid"})
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "rate_limited")

    def test_429_clamps_retry_after_to_5(self):
        responses = [
            self._mock_response(429, headers={"Retry-After": "60"}),
            self._mock_response(200),
        ]
        call_count = [0]
        def mock_request(*a, **k):
            idx = call_count[0]
            call_count[0] += 1
            return responses[idx]

        sleeps = []
        client = self._make_client(request=mock_request, sleep=lambda s: sleeps.append(s))
        resp = client.request("GET", "/me")
        self.assertEqual(sleeps[0], 5.0)

    def test_network_error_raises(self):
        import requests
        client = self._make_client(
            request=lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("fail"))
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "network")

    def test_concurrent_access_token_uses_lock(self):
        """Two threads calling access_token should get same token after one refresh."""
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_expires_at=now - timedelta(seconds=10),
        )
        new_record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_token="new-token",
        )
        fake_store = MagicMock(spec=OAuthStore)
        oauth = SpotifyOAuth(fake_store, clock=lambda: now)

        refresh_count = [0]
        def mock_do_refresh(*args, **kwargs):
            refresh_count[0] += 1
            return new_record

        loaded = [False]
        def mock_load():
            if not loaded[0]:
                loaded[0] = True
                return record
            return new_record

        saved = []
        def mock_save(rec):
            saved.append(rec)

        with patch.object(fake_store, 'load', side_effect=mock_load):
            with patch.object(fake_store, 'save', side_effect=mock_save):
                with patch.object(oauth, '_do_refresh', side_effect=mock_do_refresh):
                    results = []
                    errors = []
                    def worker():
                        try:
                            results.append(oauth.access_token())
                        except Exception as e:
                            errors.append(e)

                    t1 = threading.Thread(target=worker)
                    t2 = threading.Thread(target=worker)
                    t1.start()
                    t2.start()
                    t1.join()
                    t2.join()

        self.assertEqual(len(errors), 0, f"unexpected errors: {errors}")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], "new-token")
        self.assertEqual(results[1], "new-token")
        self.assertEqual(refresh_count[0], 1)



if __name__ == "__main__":
    unittest.main()


class ReceiverConfigConsistencyTests(unittest.TestCase):
    """Read only the public deployment template; assert the exact Web API target."""

    def test_raspotify_display_name_and_routing(self):
        from pathlib import Path
        conf = Path(__file__).resolve().parent.parent / "deploy" / "spotify" / "raspotify.conf"
        text = conf.read_text(encoding="utf-8")
        self.assertIn('LIBRESPOT_NAME="raspotify (lacasa-server)"', text)
        self.assertIn('LIBRESPOT_DEVICE="lacasa_spotify"', text)
        self.assertIn('LIBRESPOT_MIXER=softvol', text)


class Spotify401RetryTests(unittest.TestCase):
    """401 must force a refresh via the oauth layer and retry exactly once."""

    def setUp(self):
        self.oauth = MagicMock(spec=SpotifyOAuth)
        self.oauth.access_token = MagicMock(side_effect=["token-a", "fresh-token"])
        self.oauth.invalidate_tokens = MagicMock()

    def _make_client(self, request, sleep=lambda s: None):
        return SpotifyHTTP(oauth=self.oauth, request=request, sleep=sleep, timeout_seconds=1.0)

    def _resp(self, status, headers=None, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = headers or {}
        return resp

    def test_401_forces_refresh_and_retries_once(self):
        responses = [self._resp(401), self._resp(200)]
        auth_headers = []
        attempts = []

        def req(method, url, **opts):
            auth_headers.append(opts.get("headers", {}).get("Authorization"))
            attempts.append(1)
            return responses.pop(0)

        client = self._make_client(request=req)
        resp = client.request("GET", "/me")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(auth_headers, ["Bearer token-a", "Bearer fresh-token"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            self.oauth.access_token.mock_calls,
            [call(), call(force_refresh=True)],
        )
        self.oauth.invalidate_tokens.assert_not_called()

    def test_second_401_raises_unauthorized_without_further_retry(self):
        responses = [self._resp(401), self._resp(401)]
        attempts = []
        client = self._make_client(request=lambda *a, **k: (attempts.append(1), responses.pop(0))[1])
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "unauthorized")
        self.assertEqual(len(attempts), 2)

    def test_401_refresh_authorization_failure_propagates_without_retry(self):
        attempts = []
        client = self._make_client(
            request=lambda *a, **k: (attempts.append(1), self._resp(500))[1]
        )
        self.oauth.access_token = MagicMock(side_effect=SpotifyAuthorizationRequired("no refresh token"))
        with self.assertRaises(SpotifyAuthorizationRequired):
            client.request("GET", "/me")
        self.assertEqual(len(attempts), 0)

    def test_401_refresh_transient_failure_propagates(self):
        attempts = []
        client = self._make_client(
            request=lambda *a, **k: (attempts.append(1), self._resp(500))[1]
        )
        self.oauth.access_token = MagicMock(
            side_effect=SpotifyAPIError("bad_response", "refresh failed: 503")
        )
        with self.assertRaises(SpotifyAPIError):
            client.request("GET", "/me")
        self.assertEqual(len(attempts), 0)

    def test_401_then_429_both_retry_within_ceiling(self):
        responses = [self._resp(401), self._resp(429, headers={"Retry-After": "1"}), self._resp(200)]
        sleeps = []
        client = self._make_client(request=lambda *a, **k: responses.pop(0), sleep=sleeps.append)
        resp = client.request("GET", "/me")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sleeps, [1.0])

    def test_combination_ceiling_never_retries_unbounded(self):
        responses = [self._resp(401), self._resp(429, headers={"Retry-After": "1"}), self._resp(401)]
        attempts = []
        client = self._make_client(
            request=lambda *a, **k: (attempts.append(1), responses.pop(0))[1],
            sleep=lambda s: None,
        )
        with self.assertRaises(SpotifyAPIError) as ctx:
            client.request("GET", "/me")
        self.assertEqual(ctx.exception.category, "unauthorized")
        self.assertEqual(len(attempts), 3)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self.payload is None:
            raise ValueError("malformed JSON")
        return self.payload


class _ScriptedHTTP:
    """Fake SpotifyHTTP recording (method, path, params, json_body) per call."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, path, *, params=None, json_body=None):
        self.calls.append((method, path, dict(params or {}), json_body))
        if not self._responses:
            raise AssertionError(f"unexpected HTTP call #{len(self.calls)}: {method} {path}")
        return self._responses.pop(0)


def _device_record(name="raspotify (lacasa-server)", device_id="dev-1",
                   restricted=False, supports_volume=True, **extra):
    record = {
        "id": device_id,
        "name": name,
        "is_restricted": restricted,
        "supports_volume": supports_volume,
    }
    if "restrict_value" in extra:
        record["is_restricted"] = extra["restrict_value"]
    if "drop_id" in extra:
        record.pop("id")
    return record


def _devices_page(*devices):
    payload = {"devices": list(devices)}
    return _FakeResponse(200, payload=payload, text=json.dumps(payload))


class SpotifyDeviceResolutionTests(unittest.TestCase):
    """Exact receiver resolution fails closed on every hostile payload."""

    TARGET = "raspotify (lacasa-server)"

    def _controller(self, *responses):
        http = _ScriptedHTTP(*responses)
        return SpotifyController(http, target_name=self.TARGET), http

    def test_single_exact_match_returns_device_with_one_device_call(self):
        controller, http = self._controller(_devices_page(
            _device_record(name="iPhone", device_id="phone-1"),
            _device_record(name=self.TARGET, device_id="dev-1"),
        ))
        device = controller.resolve_device()
        self.assertEqual(device.id, "dev-1")
        self.assertEqual(device.name, self.TARGET)
        self.assertFalse(device.restricted)
        self.assertTrue(device.supports_volume)
        self.assertEqual(http.calls, [("GET", "/me/player/devices", {}, None)])

    def test_prefix_and_case_only_near_matches_are_rejected(self):
        for name in ("raspotify (lacasa-server) extra", "Raspotify (lacasa-server)",
                     "raspotify (lacasa-serv)"):
            with self.subTest(name=name):
                controller, http = self._controller(_devices_page(_device_record(name=name)))
                with self.assertRaises(SpotifyAPIError) as ctx:
                    controller.resolve_device()
                self.assertEqual(ctx.exception.category, "no_device")
                self.assertEqual(len(http.calls), 1)

    def test_duplicate_exact_names_fail_closed_without_any_mutation(self):
        controller, http = self._controller(_devices_page(
            _device_record(device_id="dev-1"),
            _device_record(device_id="dev-2"),
        ))
        with self.assertRaises(SpotifyAPIError) as ctx:
            controller.resolve_device()
        self.assertEqual(ctx.exception.category, "no_device")
        self.assertEqual(len(http.calls), 1)
        for call in http.calls:
            self.assertFalse(call[0] in ("PUT", "POST"))

    def test_no_match_fails_closed(self):
        controller, http = self._controller(_devices_page(_device_record(name="iPhone", device_id="phone-1")))
        with self.assertRaises(SpotifyAPIError) as ctx:
            controller.resolve_device()
        self.assertEqual(ctx.exception.category, "no_device")

    def test_restricted_or_invalid_id_matches_fail_closed(self):
        payloads = [
            [_device_record(restricted=True)],
            [_device_record(device_id=None)],
            [_device_record(device_id="")],
            [_device_record(device_id=123)],
            [_device_record(restrict_value="false")],
        ]
        for devices in payloads:
            with self.subTest(devices=devices):
                controller, http = self._controller(_devices_page(*devices))
                with self.assertRaises(SpotifyAPIError) as ctx:
                    controller.resolve_device()
                self.assertEqual(ctx.exception.category, "no_device")
                self.assertEqual(len(http.calls), 1)

    def test_volume_requirement_fails_closed(self):
        for record in (_device_record(supports_volume=False), _device_record(supports_volume=None)):
            with self.subTest(record=record):
                controller, http = self._controller(_devices_page(record))
                with self.assertRaises(SpotifyAPIError) as ctx:
                    controller.resolve_device(require_volume=True)
                self.assertEqual(ctx.exception.category, "no_device")

    def test_malformed_top_level_payload_fails_closed(self):
        for payload in ([], {"foo": 1}, None):
            with self.subTest(payload=payload):
                body = json.dumps(payload) if payload is not None else "garbage"
                controller, _ = self._controller(_FakeResponse(200, payload=payload, text=body))
                with self.assertRaises(SpotifyAPIError) as ctx:
                    controller.resolve_device()
                self.assertEqual(ctx.exception.category, "bad_response")

    def test_server_supplied_text_is_never_echoed(self):
        controller, _ = self._controller(_devices_page(
            _device_record(name="SECRET_TOKEN " + self.TARGET, device_id="dev-x"),
        ))
        with self.assertRaises(SpotifyAPIError) as ctx:
            controller.resolve_device()
        self.assertNotIn("SECRET_TOKEN", str(ctx.exception))


class SpotifyPlayFlowTests(unittest.TestCase):
    def _controller(self, *responses):
        http = _ScriptedHTTP(*responses)
        return SpotifyController(http), http

    def test_play_track_searches_then_plays_exact_body(self):
        search_payload = {"tracks": {"items": [{"uri": "spotify:track:t1", "name": "Bohemian Rhapsody"}]}}
        http = _ScriptedHTTP(
            _FakeResponse(200, payload=search_payload, text=json.dumps(search_payload)),
            _devices_page(_device_record()),
            _FakeResponse(200, payload={}, text="{}"),
        )
        result = SpotifyController(http).perform("play", query="Bohemian Rhapsody")
        self.assertIn("Spielt track 'Bohemian Rhapsody'", result)
        self.assertEqual(
            http.calls[0], ("GET", "/search", {"q": "Bohemian Rhapsody", "type": "track", "limit": 3}, None)
        )
        self.assertEqual(http.calls[1], ("GET", "/me/player/devices", {}, None))
        self.assertEqual(
            http.calls[2], ("PUT", "/me/player/play", {"device_id": "dev-1"}, {"uris": ["spotify:track:t1"]})
        )
        self.assertEqual(len(http.calls), 3)

    def test_play_album_and_artist_use_context_uri(self):
        for kind in ("album", "artist"):
            with self.subTest(kind=kind):
                search_payload = {kind + "s": {"items": [{"uri": f"spotify:{kind}:c1"}]}}
                http = _ScriptedHTTP(
                    _FakeResponse(200, payload=search_payload, text=json.dumps(search_payload)),
                    _devices_page(_device_record()),
                    _FakeResponse(200, payload={}, text="{}"),
                )
                SpotifyController(http).perform("play", query="Queen", kind=kind)
                self.assertEqual(
                    http.calls[2],
                    ("PUT", "/me/player/play", {"device_id": "dev-1"}, {"context_uri": f"spotify:{kind}:c1"}),
                )

    def test_play_no_results_returns_german_clarification_without_device_or_play(self):
        search_payload = {"tracks": {"items": []}}
        http = _ScriptedHTTP(_FakeResponse(200, payload=search_payload, text=json.dumps(search_payload)))
        result = SpotifyController(http).perform("play", query="Schnippschnapp")
        self.assertIn("Nichts gefunden", result)
        self.assertEqual(len(http.calls), 1)

    def test_play_missing_uri_fails_without_device_or_play(self):
        search_payload = {"tracks": {"items": [{"name": "no uri"}]}}
        http = _ScriptedHTTP(_FakeResponse(200, payload=search_payload, text=json.dumps(search_payload)))
        result = SpotifyController(http).perform("play", query="x")
        self.assertIn("Nichts gefunden", result)
        self.assertEqual(len(http.calls), 1)

    def test_play_rejects_blank_query_and_invalid_kind_without_http(self):
        for kwargs in ({"query": "   ", "kind": "track"}, {"query": None, "kind": "track"},
                       {"query": "x", "kind": "podcast"}):
            with self.subTest(kwargs=kwargs):
                http = _ScriptedHTTP()
                SpotifyController(http).perform("play", **kwargs)
                self.assertEqual(http.calls, [])

    def test_play_preserves_literal_query_with_specials(self):
        search_payload = {"tracks": {"items": [{"uri": "spotify:track:t1"}]}}
        query = "R.E.M. & The Band '\"quote\"'"
        http = _ScriptedHTTP(
            _FakeResponse(200, payload=search_payload, text=json.dumps(search_payload)),
            _devices_page(_device_record()),
            _FakeResponse(200, payload={}, text="{}"),
        )
        SpotifyController(http).perform("play", query=query)
        self.assertEqual(http.calls[0][2]["q"], query)

    def test_play_search_failure_returns_fixed_german_error_without_echo(self):
        class _ExplodingHTTP:
            def __init__(self):
                self.calls = []

            def request(self, method, path, *, params=None, json_body=None):
                self.calls.append((method, path))
                raise SpotifyAPIError("bad_response", "server said: SECRET_TOKEN")

        http = _ExplodingHTTP()
        result = SpotifyController(http).perform("play", query="x")
        self.assertNotIn("SECRET_TOKEN", result)


class SpotifyMutationEndpointTests(unittest.TestCase):
    def _controller(self, *responses):
        http = _ScriptedHTTP(_devices_page(_device_record()), *responses)
        return SpotifyController(http), http

    def test_endpoint_method_map(self):
        expectations = {
            "pause": ("PUT", "/me/player/pause"),
            "resume": ("PUT", "/me/player/play"),
            "next": ("POST", "/me/player/next"),
            "previous": ("POST", "/me/player/previous"),
        }
        for action, (method, path) in expectations.items():
            with self.subTest(action=action):
                controller, http = self._controller(_FakeResponse(200, payload={}, text="{}"))
                controller.perform(action)
                self.assertEqual(http.calls[0], ("GET", "/me/player/devices", {}, None))
                self.assertEqual(http.calls[1], (method, path, {"device_id": "dev-1"}, None))
                self.assertEqual(len(http.calls), 2)

    def test_volume_endpoint_with_percent(self):
        for percent in (0, 100):
            with self.subTest(percent=percent):
                controller, http = self._controller(_FakeResponse(200, payload={}, text="{}"))
                result = controller.perform("volume", volume_percent=percent)
                self.assertIn(f"{percent}%", result)
                self.assertEqual(
                    http.calls[1],
                    ("PUT", "/me/player/volume", {"device_id": "dev-1", "volume_percent": percent}, None),
                )

    def test_volume_invalid_values_rejected_without_http(self):
        for value in (-1, 101, 1.5, "50", None, True):
            with self.subTest(value=value):
                http = _ScriptedHTTP()
                result = SpotifyController(http).perform("volume", volume_percent=value)
                self.assertEqual(http.calls, [])
                self.assertNotEqual(result, "")


class SpotifyCurrentTests(unittest.TestCase):
    def _player_payload(self, device_id="dev-1", is_playing=True, item=None):
        if item is None:
            item = {"name": "Bohemian Rhapsody", "artists": [{"name": "Queen"}]}
        return {"device": {"id": device_id}, "is_playing": is_playing, "item": item}

    def test_current_on_active_receiver_reports_track_and_artist(self):
        payload = self._player_payload()
        http = _ScriptedHTTP(
            _devices_page(_device_record()),
            _FakeResponse(200, payload=payload, text=json.dumps(payload)),
        )
        result = SpotifyController(http).perform("current")
        self.assertIn("Bohemian Rhapsody", result)
        self.assertIn("Queen", result)
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[1], ("GET", "/me/player", {}, None))

    def test_current_other_device_id_reports_nothing_and_hides_item(self):
        payload = self._player_payload(device_id="phone-9")
        payload["item"] = {"name": "Phone Track", "artists": [{"name": "Other"}]}
        http = _ScriptedHTTP(
            _devices_page(_device_record()),
            _FakeResponse(200, payload=payload, text=json.dumps(payload)),
        )
        result = SpotifyController(http).perform("current")
        self.assertIn("läuft zurzeit nichts", result)
        self.assertNotIn("Phone Track", result)

    def test_current_paused_null_device_or_null_item_reports_nothing(self):
        payloads = [
            self._player_payload(is_playing=False),
            self._player_payload(device_id=None),
            {"device": {"id": "dev-1"}, "is_playing": True, "item": None},
            self._player_payload(item={"artists": [{"name": "Queen"}]}),
            {"device": None, "is_playing": True, "item": {"name": "x"}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                http = _ScriptedHTTP(
                    _devices_page(_device_record()),
                    _FakeResponse(200, payload=payload, text=json.dumps(payload)),
                )
                result = SpotifyController(http).perform("current")
                self.assertIn("läuft zurzeit nichts", result)

    def test_current_204_and_malformed_json_report_nothing(self):
        for response in (_FakeResponse(204, payload=None, text=""),
                         _FakeResponse(200, payload=None, text="garbage")):
            with self.subTest(status=response.status_code):
                http = _ScriptedHTTP(_devices_page(_device_record()), response)
                result = SpotifyController(http).perform("current")
                self.assertIn("läuft zurzeit nichts", result)


class SpotifyControllerErrorPathsTests(unittest.TestCase):
    class _FailingHTTP:
        def __init__(self, error=None):
            self._error = error
            self.calls = []

        def request(self, method, path, *, params=None, json_body=None):
            self.calls.append((method, path))
            if self._error is not None:
                raise self._error
            raise SpotifyAuthorizationRequired("no access token")

    def test_authorization_required_becomes_german_reauth_text(self):
        result = SpotifyController(self._FailingHTTP()).perform("pause")
        self.assertIn("Spotify muss einmal verbunden werden", result)
        self.assertNotIn("no access token", result)

    def test_api_error_categories_map_to_fixed_german_texts(self):
        cases = (
            (SpotifyAPIError("premium"), "Premium"),
            (SpotifyAPIError("no_device"), "nicht aktiv"),
            (SpotifyAPIError("rate_limited"), "warten"),
            (SpotifyAPIError("network"), "nicht erreichbar"),
        )
        for error, snippet in cases:
            with self.subTest(category=error.category):
                result = SpotifyController(self._FailingHTTP(error=error)).perform("pause")
                self.assertIn(snippet, result)


class _NoRequestHTTP:
    """HTTP fake that explodes on any usage: only pre-HTTP validation paths pass."""

    def request(self, method, path, *, params=None, json_body=None):
        raise AssertionError(f"unexpected HTTP call: {method} {path}")


class SpotifyValidationGermanTextTests(unittest.TestCase):
    def test_volume_validation_returns_german_text(self):
        result = SpotifyController(_NoRequestHTTP()).perform("volume", volume_percent=150)
        self.assertIn("volume_percent", result)


class AuthorizationStatusReportingTests(unittest.TestCase):
    """status() must reflect invalidated tokens, not merely the deadline."""

    def _oauth_with_record(self, record, now):
        store = MagicMock(spec=OAuthStore)
        oauth = SpotifyOAuth(store, clock=lambda: now)
        store.load = MagicMock(return_value=record)
        return oauth

    def _now(self):
        return datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_invalidated_record_reports_reauthorization_required(self):
        now = self._now()
        record = _make_record(access_token=None, access_expires_at=None, refresh_token=None,
                              authorized_at=now, authorization_expires_at=_six_months_from(now))
        status = self._oauth_with_record(record, now).status()
        self.assertFalse(status.authorized)
        self.assertTrue(status.reauthorization_required)

    def test_record_with_refresh_token_is_authorized_before_deadline(self):
        now = self._now()
        record = _make_record(access_token=None, access_expires_at=None, refresh_token="old-refresh",
                              authorized_at=now, authorization_expires_at=_six_months_from(now))
        status = self._oauth_with_record(record, now).status()
        self.assertTrue(status.authorized)
        self.assertFalse(status.reauthorization_required)

    def test_valid_record_is_authorized(self):
        now = self._now()
        record = _make_record(authorized_at=now, authorization_expires_at=_six_months_from(now),
                              access_expires_at=now + timedelta(hours=2))
        status = self._oauth_with_record(record, now).status()
        self.assertTrue(status.authorized)
        self.assertFalse(status.reauthorization_required)


class RefreshFailureClassificationTests(unittest.TestCase):
    """Transient refresh failures must not invalidate the authorization."""

    def _oauth(self, responses, now):
        record = _make_record(
            authorized_at=now,
            authorization_expires_at=_six_months_from(now),
            access_token="old-token",
            access_expires_at=now - timedelta(seconds=10),
            refresh_token="old-refresh",
        )
        store = MagicMock(spec=OAuthStore)
        store.load = MagicMock(return_value=record)
        oauth = SpotifyOAuth(store, request=lambda *a, **k: responses.pop(0), clock=lambda: now)
        return oauth, store

    def _resp(self, status, text=""):
        resp = MagicMock()
        resp.status_code = status
        resp.text = text
        resp.headers = {}
        try:
            resp.json.return_value = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            resp.json.side_effect = ValueError("malformed JSON")
        return resp

    def test_refresh_5xx_keeps_authorization_and_raises_api_error(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, store = self._oauth([self._resp(503, text='{"error": "temporarily unavailable"}')], now)
        with self.assertRaises(SpotifyAPIError):
            oauth.access_token()
        store.invalidate_tokens.assert_not_called()
        store.save.assert_not_called()

    def test_refresh_429_keeps_authorization_and_maps_rate_limited(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, store = self._oauth([self._resp(429, text="")], now)
        with self.assertRaises(SpotifyAPIError) as ctx:
            oauth.access_token()
        self.assertEqual(ctx.exception.category, "rate_limited")
        store.invalidate_tokens.assert_not_called()

    def test_refresh_network_error_keeps_authorization_and_maps_network(self):
        import requests as requests_module
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, store = self._oauth([], now)

        def fail(*args, **kwargs):
            raise requests_module.ConnectionError("down")

        oauth._request = fail
        with self.assertRaises(SpotifyAPIError) as ctx:
            oauth.access_token()
        self.assertEqual(ctx.exception.category, "network")
        store.invalidate_tokens.assert_not_called()

    def test_refresh_invalid_grant_invalidates_and_raises_authorization_required(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, store = self._oauth([self._resp(400, text='{"error": "invalid_grant"}')], now)
        with self.assertRaises(SpotifyAuthorizationRequired):
            oauth.access_token()
        store.invalidate_tokens.assert_called_once()

    def test_refresh_other_400_keeps_authorization(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, store = self._oauth([self._resp(400, text='{"error": "unsupported_grant_type"}')], now)
        with self.assertRaises(SpotifyAPIError):
            oauth.access_token()
        store.invalidate_tokens.assert_not_called()

    def test_refresh_non_dict_json_body_does_not_crash(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        for text, status in (("[]", 200), ("[1, 2]", 400), ("not json", 400)):
            with self.subTest(text=text, status=status):
                oauth, store = self._oauth([self._resp(status, text=text)], now)
                try:
                    oauth.access_token()
                except SpotifyAPIError:
                    pass
                except SpotifyAuthorizationRequired:
                    self.fail("non-dict body must not be classified as permanent auth failure")
                except AttributeError:
                    self.fail("non-dict body must not raise AttributeError")
                store.invalidate_tokens.assert_not_called()

    def test_refresh_invalid_grant_never_leaks_token_in_message(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        oauth, _ = self._oauth([self._resp(400, text='{"error": "invalid_grant"}')], now)
        try:
            oauth.access_token()
        except SpotifyAuthorizationRequired as exc:
            self.assertNotIn("old-refresh", str(exc))
            self.assertNotIn("old-token", str(exc))


class DeadHelperRemovalTests(unittest.TestCase):
    def test_dead_token_helpers_are_removed(self):
        import core.spotify as spotify_module
        self.assertFalse(hasattr(spotify_module, "_access_token_with_lock"))
        self.assertFalse(hasattr(spotify_module, "_refresh_if_needed"))
