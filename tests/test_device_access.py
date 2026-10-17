"""One-hour in-memory bearer sessions and durable dashboard login tests."""
import asyncio
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from core.device_credentials import DeviceCredentialConfigError, DeviceCredentialStore
from dashboard.access import AttemptLimiter, SessionRegistry
from dashboard.server import DashboardServer


async def _control_request(path: Path, payload: bytes) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(payload)
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), 2)
        return json.loads(reply)
    finally:
        writer.close()
        await writer.wait_closed()


class DeviceControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = DeviceCredentialStore(self.root / "devices.json")
        self.server = DashboardServer(
            public_url="https://example.test",
            host="127.0.0.1",
            device_store=self.store,
            qr_output_dir=self.root / "qr",
        )
        self.path = self.root / "control" / "control.sock"
        self.task = asyncio.create_task(self.server.serve_control(self.path))
        await asyncio.wait_for(self.server._control_ready.wait(), 2)

    async def _cleanup(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.temp.cleanup()

    async def test_json_control_create_list_revoke_round_trip_is_secret_free(self):
        printed = Mock()
        with patch("builtins.print", printed):
            created = await _control_request(self.path, json.dumps({
                "command": "device-create", "label": "LaCasa iPhone 1",
            }).encode() + b"\n")
            device_id = created["device"]["device_id"]

            async def cli(*args):
                return await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "core.headless", *args, "--socket", str(self.path)],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False,
                )

            list_cli = await cli("device-list")
            revoke_cli = await cli("device-revoke", device_id)
            listed = json.loads(list_cli.stdout)
            revoked = json.loads(revoke_cli.stdout)

        url_payload = Path(created["url_path"]).read_text(encoding="utf-8")
        secret = url_payload.rsplit(".", 1)[1]
        observed = "\n".join((json.dumps(created), json.dumps(listed), json.dumps(revoked),
                                list_cli.stdout, list_cli.stderr, revoke_cli.stdout, revoke_cli.stderr,
                                " ".join(str(call) for call in printed.call_args_list),
                                Path(created["url_path"]).name, Path(created["qr_path"]).name))
        self.assertEqual(set(created), {"ok", "device", "url_path", "qr_path"})
        self.assertTrue(created["ok"])
        self.assertEqual(set(created["device"]), {"device_id", "label", "created_at", "revoked_at"})
        self.assertEqual(listed["devices"], [created["device"]])
        self.assertEqual(revoked["device"]["device_id"], device_id)
        self.assertIsNotNone(revoked["device"]["revoked_at"])
        self.assertEqual(list_cli.returncode, 0)
        self.assertEqual(revoke_cli.returncode, 0)
        self.assertFalse(secret in observed, "durable secret leaked outside its private artifact")
        self.assertFalse(url_payload in observed, "durable URL leaked outside its private artifact")

    async def test_control_rejects_oversize_bad_utf8_bad_json_and_unknown_command(self):
        payloads = (
            b"x" * 4096 + b"\n",
            b'{"command":"device-list","label":"\xff"}\n',
            b"not json\n",
            b'{"command":"not-registered"}\n',
        )
        for payload in payloads:
            with self.subTest(payload_kind=len(payload)):
                reply = await _control_request(self.path, payload)
                self.assertFalse(reply["ok"])
                self.assertLessEqual(len(json.dumps(reply).encode() + b"\n"), 65536)
                self.assertNotIn(payload.decode("utf-8", "replace"), json.dumps(reply))

    async def test_registered_extension_command_uses_same_bounded_protocol(self):
        async def status_handler(request):
            self.assertEqual(request, {"command": "synthetic-status"})
            return {"ok": True, "state": "ready"}

        self.server.register_control_command("synthetic-status", status_handler)
        reply = await _control_request(self.path, b'{"command":"synthetic-status"}\n')

        self.assertEqual(reply, {"ok": True, "state": "ready"})
        with self.assertRaises(ValueError):
            self.server.register_control_command("pair", status_handler)
        with self.assertRaises(ValueError):
            self.server.register_control_command("device-list", status_handler)

    async def test_device_create_artifact_failure_revokes_new_record_and_cleans_partial_files(self):
        with patch("qrcode.QRCode.make_image", side_effect=RuntimeError("synthetic artifact failure")):
            reply = await _control_request(
                self.path,
                b'{"command":"device-create","label":"Broken QR"}\n',
            )

        devices = self.store.list_devices()
        self.assertFalse(reply["ok"])
        self.assertEqual(len(devices), 1)
        self.assertIsNotNone(devices[0].revoked_at)
        self.assertFalse(any((self.root / "qr").iterdir()), "artifact failure left partial output")

    async def test_device_create_does_not_delete_preexisting_artifact(self):
        created = self.store.create("Existing device")
        output = self.root / "qr"
        output.mkdir(mode=0o700, exist_ok=True)
        preexisting = output / f"{created.summary.device_id}.url"
        preexisting.write_text("operator-owned sentinel", encoding="utf-8")
        preexisting.chmod(0o600)

        with patch.object(self.store, "create", return_value=created):
            reply = await _control_request(
                self.path,
                b'{"command":"device-create","label":"Replacement"}\n',
            )

        self.assertFalse(reply["ok"])
        self.assertEqual(preexisting.read_text(encoding="utf-8"), "operator-owned sentinel")
        self.assertIsNotNone(self.store.list_devices()[0].revoked_at)

    async def _assert_rollback_failure_stops_control_service(self, rollback_error):
        created = self.store.create("Rollback failure")
        output = self.root / "qr"
        output.mkdir(mode=0o700, exist_ok=True)
        preexisting = output / f"{created.summary.device_id}.url"
        preexisting.write_text("operator-owned sentinel", encoding="utf-8")
        preexisting.chmod(0o600)

        with patch.object(self.store, "create", return_value=created), \
                patch.object(self.server, "revoke_device", side_effect=rollback_error):
            with self.assertRaises(json.JSONDecodeError):
                await _control_request(
                    self.path,
                    b'{"command":"device-create","label":"Broken rollback"}\n',
                )
            with self.assertRaises(DeviceCredentialConfigError):
                await asyncio.wait_for(self.task, 2)

        self.assertIsNone(self.store.list_devices()[0].revoked_at)
        self.assertEqual(preexisting.read_text(encoding="utf-8"), "operator-owned sentinel")

    async def test_device_create_config_rollback_failure_stops_control_service(self):
        await self._assert_rollback_failure_stops_control_service(
            DeviceCredentialConfigError("synthetic rollback failure")
        )

    async def test_device_create_runtime_rollback_failure_stops_control_service(self):
        await self._assert_rollback_failure_stops_control_service(
            RuntimeError("synthetic rollback failure")
        )

    async def test_device_create_value_rollback_failure_stops_control_service(self):
        await self._assert_rollback_failure_stops_control_service(
            ValueError("synthetic rollback failure")
        )


class DeviceCliTests(unittest.TestCase):
    def _run_main(self, argv, *, pair_url=None, reply=None):
        from core import headless
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["headless", *argv]), \
                patch.object(headless, "request_pair", return_value=pair_url), \
                patch.object(headless, "request_control", return_value=reply), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            headless.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_device_cli_commands_emit_safe_json_and_preserve_pair_cli(self):
        safe = {"ok": True, "device": {"device_id": "id", "label": "phone", "created_at": "now", "revoked_at": None}}
        for argv, reply in ((["device-create", "--label", "phone"], safe),
                            (["device-list"], {"ok": True, "devices": [safe["device"]]}),
                            (["device-revoke", "id"], safe)):
            with self.subTest(argv=argv):
                stdout, stderr = self._run_main(argv, reply=reply)
                self.assertEqual(json.loads(stdout), reply)
                self.assertEqual(stderr, "")
        pair_url = "https://example.test/auto-login?key=PAIRING"
        stdout, stderr = self._run_main(["pair"], pair_url=pair_url)
        self.assertEqual(stdout, pair_url + "\n")
        self.assertEqual(stderr, "")

    def test_phone_bearer_has_no_device_admin_http_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            store = DeviceCredentialStore(root / "devices.json")
            device = store.create("phone")
            server = DashboardServer(public_url="https://example.test", host="127.0.0.1", device_store=store)
            client = TestClient(server.app)
            token = client.post("/api/device-login", json={
                "device_id": device.summary.device_id, "secret": device.secret,
            }).json()["token"]
            routes = {route.path for route in server.app.routes}
            headers = {"Authorization": f"Bearer {token}"}
            for path in ("/api/revoke-devices", "/api/devices", "/api/device-create",
                         "/api/device-list", "/api/device-revoke"):
                with self.subTest(path=path):
                    self.assertNotIn(path, routes)
                    self.assertEqual(client.post(path, headers=headers).status_code, 404)


class SessionRegistryTests(unittest.TestCase):
    def test_session_record_is_valid_before_and_invalid_at_absolute_expiry(self):
        now = [1_000.0]
        registry = SessionRegistry(ttl_seconds=3600, clock=lambda: now[0])
        record = registry.issue("device-a")

        now[0] = 4_599.999
        self.assertEqual(registry.authenticate(record.token), record)
        now[0] = 4_600.0
        self.assertIsNone(registry.authenticate(record.token))
        self.assertIsNone(registry.authenticate(record.token))

    def test_session_issue_generates_fresh_bearer_and_encryption_key(self):
        registry = SessionRegistry(clock=lambda: 1_000.0)
        first = registry.issue("device-a")
        second = registry.issue("device-a")

        self.assertNotEqual(first.token, second.token)
        self.assertNotEqual(first.session_key, second.session_key)
        self.assertEqual(first.device_id, "device-a")
        self.assertEqual(second.device_id, "device-a")
        self.assertEqual(first.expires_at - first.created_at, 3600)
        self.assertNotIn("device-a", vars(registry))

    def test_registry_revokes_only_matching_device_or_transient_sessions(self):
        registry = SessionRegistry(clock=lambda: 1_000.0)
        first = registry.issue("device-a")
        second = registry.issue("device-a")
        other = registry.issue("device-b")
        transient = registry.issue(None)

        self.assertEqual(registry.revoke_device("device-a"), {first.token, second.token})
        self.assertIsNone(registry.authenticate(first.token))
        self.assertIsNone(registry.authenticate(second.token))
        self.assertEqual(registry.authenticate(other.token), other)
        self.assertEqual(registry.authenticate(transient.token), transient)
        self.assertEqual(registry.revoke_transient(), {transient.token})
        self.assertEqual(registry.authenticate(other.token), other)
        self.assertIsNone(registry.authenticate(transient.token))

    def test_rate_limit_has_independent_ten_per_minute_buckets(self):
        now = [100.0]
        limiter = AttemptLimiter(clock=lambda: now[0])

        for _ in range(10):
            self.assertTrue(limiter.allow("transient-login"))
        self.assertFalse(limiter.allow("transient-login"))
        for _ in range(10):
            self.assertTrue(limiter.allow("durable-login"))
        self.assertFalse(limiter.allow("durable-login"))
        now[0] += 60
        self.assertTrue(limiter.allow("transient-login"))
        self.assertTrue(limiter.allow("durable-login"))


class DurableDeviceLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o700)
        self.path = root / "devices.json"
        self.now = [1_000.0]
        self.monotonic = [100.0]
        self.store = DeviceCredentialStore(self.path, clock=lambda: self.now[0])
        self.device = self.store.create("LaCasa iPhone")
        self.server = self._server(self.store)
        self.client = TestClient(self.server.app)

    def _server(self, store):
        return DashboardServer(
            public_url="https://example.test",
            host="127.0.0.1",
            device_store=store,
            bearer_ttl_seconds=3600,
            wall_clock=lambda: self.now[0],
            monotonic_clock=lambda: self.monotonic[0],
        )

    def _login(self, client=None, device=None):
        client = client or self.client
        device = device or self.device
        return client.post(
            "/api/device-login",
            json={"device_id": device.summary.device_id, "secret": device.secret},
        )

    def test_device_login_rejects_malformed_unknown_wrong_and_revoked_identically(self):
        malformed = self.client.post("/api/device-login", content=b"[")
        wrong_type = self.client.post("/api/device-login", json={"device_id": 1, "secret": 2})
        wrong_secret = ("A" if self.device.secret[0] != "A" else "B") + self.device.secret[1:]
        wrong = self.client.post("/api/device-login", json={
            "device_id": self.device.summary.device_id,
            "secret": wrong_secret,
        })
        unknown = self.client.post("/api/device-login", json={
            "device_id": "A" * len(self.device.summary.device_id),
            "secret": self.device.secret,
        })
        revoked_device = self.store.create("LaCasa iPad")
        self.store.revoke(revoked_device.summary.device_id)
        revoked = self._login(device=revoked_device)

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual({wrong.status_code, unknown.status_code, revoked.status_code}, {401})
        self.assertEqual(wrong.content, unknown.content)
        self.assertEqual(wrong.content, revoked.content)

    def test_device_login_rejects_malformed_credential_encodings_before_verification(self):
        class StoreWithoutMalformedVerifier:
            def __init__(self):
                self.calls = []

            def list_devices(self):
                return []

            def verify(self, device_id, secret):
                self.calls.append((device_id, secret))
                return None

        store = StoreWithoutMalformedVerifier()
        client = TestClient(self._server(store).app)
        valid_id = self.device.summary.device_id
        valid_secret = self.device.secret
        malformed = {
            "invalid base64url device_id": {"device_id": "!" * len(valid_id), "secret": valid_secret},
            "invalid base64url secret": {"device_id": valid_id, "secret": "!" * len(valid_secret)},
            "wrong-length device_id": {"device_id": valid_id[:-1], "secret": valid_secret},
            "wrong-length secret": {"device_id": valid_id, "secret": valid_secret[:-1]},
            "non-ASCII device_id": {"device_id": "é" * len(valid_id), "secret": valid_secret},
            "non-ASCII secret": {"device_id": valid_id, "secret": "é" * len(valid_secret)},
        }

        for description, body in malformed.items():
            with self.subTest(description=description):
                response = client.post("/api/device-login", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json(), {"ok": False})
                self.assertEqual(store.calls, [])

    def test_corrupt_credential_store_is_rejected_at_dashboard_construction(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        self.path.chmod(0o600)

        with self.assertRaises(DeviceCredentialConfigError):
            self._server(DeviceCredentialStore(self.path, clock=lambda: self.now[0]))

    def test_device_login_returns_fresh_session_material_without_secret_disclosure(self):
        first = self._login()
        second = self._login()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(set(first.json()), {"ok", "token", "key", "expires_in"})
        self.assertTrue(first.json()["ok"])
        self.assertEqual(first.json()["expires_in"], 3600)
        self.assertNotEqual(first.json()["token"], second.json()["token"])
        self.assertNotEqual(first.json()["key"], second.json()["key"])
        self.assertNotIn(self.device.secret, first.text)
        self.assertNotIn(self.device.secret, second.text)

    def test_durable_login_survives_new_dashboard_server_with_same_store(self):
        initial = self._login().json()
        replacement = self._server(DeviceCredentialStore(self.path, clock=lambda: self.now[0]))
        replacement_client = TestClient(replacement.app)
        resumed = self._login(replacement_client)

        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(replacement_client.get(
            "/api/status", headers={"Authorization": f"Bearer {initial['token']}"}
        ).status_code, 401)

    def test_http_bearer_expires_at_one_hour_without_sliding(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        headers = {"Authorization": f"Bearer {response.json()['token']}"}
        self.now[0] += 3_599.999
        self.assertEqual(self.client.get("/api/status", headers=headers).status_code, 200)
        self.now[0] += 0.001

        self.assertEqual(self.client.get("/api/status", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/api/command", headers=headers, json={"text": "x"}).status_code, 401)
        self.assertEqual(self.client.post("/api/interrupt", headers=headers).status_code, 401)

    def test_durable_login_rate_limit_does_not_consume_transient_pin_budget(self):
        for _ in range(10):
            self.assertEqual(self.client.post("/login", json={"pin": "WRONG"}).status_code, 401)
        self.assertEqual(self.client.post("/login", json={"pin": "WRONG"}).status_code, 429)

        self.assertEqual(self._login().status_code, 200)

    def test_no_raw_server_secret_or_session_material_is_written_to_store(self):
        before = self.path.read_bytes()
        response = self._login()
        after = self.path.read_bytes()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(after, before)
        self.assertNotIn(self.device.secret.encode(), after)
        self.assertNotIn(response.json()["token"].encode(), after)
        self.assertNotIn(response.json()["key"].encode(), after)
        self.assertNotIn(b"session_key", after)
        self.assertEqual(json.loads(after)["version"], 1)

    def test_transient_pair_does_not_change_store_or_durable_bearers(self):
        durable = self._login().json()
        before = self.path.read_bytes()
        key = self.server.new_key()
        transient = self.client.post("/login", json={"pin": key}).json()

        self.server.new_key()

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.client.get(
            "/api/status", headers={"Authorization": f"Bearer {durable['token']}"}
        ).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/status", headers={"Authorization": f"Bearer {transient['token']}"}
        ).status_code, 401)

    def test_revocation_wins_a_concurrent_relogin_and_cannot_resurrect_device(self):
        active = self._login().json()

        summary = asyncio.run(self.server.revoke_device(self.device.summary.device_id))
        relogin = self._login()

        self.assertEqual(summary.device_id, self.device.summary.device_id)
        self.assertIsNone(self.server._sessions.authenticate(active["token"]))
        self.assertIsNone(self.store.verify(self.device.summary.device_id, self.device.secret))
        self.assertEqual(relogin.status_code, 401)


if __name__ == "__main__":
    unittest.main()
