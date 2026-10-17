"""Persistent device credential storage tests using synthetic credentials only."""
import base64
import contextlib
import hashlib
import hmac
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import core.device_credentials as credentials
from core.device_credentials import DeviceCredentialConfigError, DeviceCredentialStore


class DeviceCredentialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.path = self.root / "devices.json"
        self.store = DeviceCredentialStore(self.path, clock=lambda: 1_728_000_000.0)

    @staticmethod
    def _decode(value):
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def test_four_credentials_persist_without_raw_secrets(self):
        created = [self.store.create(f"LaCasa iPhone {number}") for number in range(1, 5)]

        raw = self.path.read_bytes()
        serialized = json.loads(raw)
        self.assertEqual(serialized["version"], 1)
        self.assertEqual(len(serialized["devices"]), 4)
        self.assertEqual(len({entry.summary.device_id for entry in created}), 4)
        self.assertEqual(len({entry["salt"] for entry in serialized["devices"]}), 4)
        self.assertEqual(len({entry["verifier"] for entry in serialized["devices"]}), 4)
        for entry in created:
            self.assertEqual(len(self._decode(entry.secret)), 32)
            self.assertNotIn(entry.secret.encode(), raw)
            self.assertNotIn(entry.secret, repr(entry))
            self.assertNotIn(entry.secret, repr(entry.summary))
            self.assertNotIn("salt", entry.summary.__dict__)
            self.assertNotIn("verifier", entry.summary.__dict__)

    def test_restart_store_verifies_each_device_independently(self):
        created = [self.store.create(f"LaCasa iPhone {number}") for number in range(1, 5)]
        restarted = DeviceCredentialStore(self.path, clock=lambda: 1_728_000_001.0)

        self.assertEqual(
            [restarted.verify(entry.summary.device_id, entry.secret) for entry in created],
            [entry.summary for entry in created],
        )
        self.assertIsNone(restarted.verify(created[0].summary.device_id, created[1].secret))

    def test_scrypt_uses_independent_salts_and_constant_time_compare(self):
        real_scrypt = hashlib.scrypt
        real_compare = hmac.compare_digest
        scrypt_calls = []
        compare_calls = []

        def checked_scrypt(*args, **kwargs):
            scrypt_calls.append((args, kwargs))
            self.assertEqual(kwargs["n"], 2**14)
            self.assertEqual(kwargs["r"], 8)
            self.assertEqual(kwargs["p"], 1)
            self.assertEqual(kwargs["dklen"], 32)
            return real_scrypt(*args, **kwargs)

        def checked_compare(left, right):
            compare_calls.append((left, right))
            return real_compare(left, right)

        with patch("core.device_credentials.hashlib.scrypt", side_effect=checked_scrypt), patch(
            "core.device_credentials.hmac.compare_digest", side_effect=checked_compare
        ):
            first = self.store.create("LaCasa iPhone 1")
            second = self.store.create("LaCasa iPhone 2")
            self.assertEqual(self.store.verify(first.summary.device_id, first.secret), first.summary)

        self.assertGreaterEqual(len(scrypt_calls), 3)
        self.assertEqual(len(self._decode(first.secret)), 32)
        self.assertNotEqual(scrypt_calls[0][1]["salt"], scrypt_calls[1][1]["salt"])
        self.assertEqual(len(compare_calls), 1)

    def test_revoke_is_independent_idempotent_and_persistent(self):
        first = self.store.create("LaCasa iPhone 1")
        second = self.store.create("LaCasa iPhone 2")

        revoked = self.store.revoke(first.summary.device_id)
        self.assertIsNotNone(revoked)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertEqual(self.store.revoke(first.summary.device_id), revoked)
        self.assertIsNone(self.store.verify(first.summary.device_id, first.secret))
        self.assertEqual(self.store.verify(second.summary.device_id, second.secret), second.summary)

        restarted = DeviceCredentialStore(self.path, clock=lambda: 1_728_000_001.0)
        self.assertEqual(restarted.revoke(first.summary.device_id), revoked)
        self.assertIsNone(restarted.verify(first.summary.device_id, first.secret))
        self.assertEqual(restarted.verify(second.summary.device_id, second.secret), second.summary)
        self.assertIsNone(restarted.revoke("A" * 22))

    def test_unknown_well_formed_device_still_runs_dummy_scrypt(self):
        created = self.store.create("LaCasa iPhone 1")
        unknown = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode()
        calls = []
        real_scrypt = hashlib.scrypt
        real_compare = hmac.compare_digest

        def counted_scrypt(*args, **kwargs):
            calls.append(kwargs)
            return real_scrypt(*args, **kwargs)

        with patch("core.device_credentials.hashlib.scrypt", side_effect=counted_scrypt), patch(
            "core.device_credentials.hmac.compare_digest", wraps=real_compare
        ) as compare:
            self.assertIsNone(self.store.verify(unknown, created.secret))

        self.assertEqual(len(calls), 1)
        compare.assert_called_once()

    def test_invalid_label_or_credential_encoding_is_rejected_without_write(self):
        for label in ("", "   ", "x" * 81, "bad\x00label", "bad\x7flabel", "bad\x85label"):
            with self.subTest(label=repr(label)):
                with self.assertRaises(ValueError):
                    self.store.create(label)
                self.assertFalse(self.path.exists())

        created = self.store.create("LaCasa iPhone 1")
        before = self.path.read_bytes()
        for device_id, secret in (("not-base64!", created.secret), (created.summary.device_id, "not-base64!"), ("short", "short")):
            with self.subTest(device_id=device_id):
                self.assertIsNone(self.store.verify(device_id, secret))
                self.assertEqual(self.path.read_bytes(), before)

    def test_store_rejects_symlink_wrong_mode_wrong_owner_and_corruption(self):
        created = self.store.create("LaCasa iPhone 1")
        safe_bytes = self.path.read_bytes()
        target = self.root / "link-target.json"
        target.write_bytes(safe_bytes)
        self.path.unlink()
        self.path.symlink_to(target)
        with self.assertRaises(DeviceCredentialConfigError):
            self.store.list_devices()
        self.assertEqual(target.read_bytes(), safe_bytes)

        self.path.unlink()
        self.path.write_bytes(safe_bytes)
        self.path.chmod(0o640)
        with self.assertRaises(DeviceCredentialConfigError):
            self.store.list_devices()

        self.path.chmod(0o600)
        with patch("core.device_credentials.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(DeviceCredentialConfigError):
                self.store.list_devices()

        self.path.write_bytes(b"{")
        with self.assertRaises(DeviceCredentialConfigError):
            self.store.list_devices()
        self.assertFalse(created.secret in self.path.read_text(errors="ignore"), "secret leaked into corrupt store")

    def test_store_write_accepts_exact_limit_and_rejects_one_byte_over_without_replacement(self):
        exact = b"x" * 1_048_576
        with patch.object(credentials.json, "dumps", return_value=exact.decode("ascii")):
            self.store._write([])
        self.assertEqual(self.path.read_bytes(), exact)

        with patch.object(credentials.json, "dumps", return_value="x" * (1_048_576 + 1)):
            with self.assertRaises(DeviceCredentialConfigError):
                self.store._write([])

        self.assertEqual(self.path.read_bytes(), exact)

    def test_store_rejects_symlinked_ancestor_without_writing_target_directory(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        nested = target / "nested"
        nested.mkdir(mode=0o700)
        ancestor = self.root / "ancestor"
        ancestor.symlink_to(target, target_is_directory=True)
        store = DeviceCredentialStore(ancestor / "nested" / "devices.json")

        with self.assertRaises(DeviceCredentialConfigError):
            store.create("LaCasa iPhone 1")

        self.assertFalse((nested / "devices.json").exists())

    def test_atomic_write_never_exposes_partial_json(self):
        failures = []
        observed_update = threading.Event()
        stop = threading.Event()

        self.store.create("LaCasa iPhone 0")

        def read_while_writing():
            while not stop.is_set():
                try:
                    document = json.loads(self.path.read_bytes())
                    if len(document["devices"]) > 1:
                        observed_update.set()
                except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    failures.append(type(exc).__name__)
                    return

        reader = threading.Thread(target=read_while_writing)
        reader.start()
        try:
            for number in range(32):
                self.store.create(f"LaCasa iPhone {number + 1}")
        finally:
            stop.set()
            reader.join()
        self.assertEqual(failures, [], "reader observed a partial credential store")
        self.assertTrue(observed_update.is_set(), "reader did not observe any atomic replacement")
        self.assertEqual(len(json.loads(self.path.read_bytes())["devices"]), 33)

    def test_concurrent_creates_and_revoke_preserve_every_record(self):
        start = threading.Barrier(9)
        created = []
        created_lock = threading.Lock()

        def create(number):
            start.wait()
            result = self.store.create(f"LaCasa iPhone {number}")
            with created_lock:
                created.append(result)

        workers = [threading.Thread(target=create, args=(number,)) for number in range(1, 9)]
        for worker in workers:
            worker.start()
        start.wait()
        for worker in workers:
            worker.join()
        revoked_ids = {item.summary.device_id for item in created[:4]}
        revoke_workers = [threading.Thread(target=self.store.revoke, args=(device_id,)) for device_id in revoked_ids]
        for worker in revoke_workers:
            worker.start()
        for worker in revoke_workers:
            worker.join()

        reopened = DeviceCredentialStore(self.path)
        devices = reopened.list_devices()
        self.assertEqual({item.device_id for item in devices}, {item.summary.device_id for item in created})
        self.assertEqual({item.device_id for item in devices if item.revoked_at is not None}, revoked_ids)

    def test_qr_uses_fragment_only_and_private_artifacts(self):
        from core.device_credentials import write_device_artifacts
        import qrcode

        created = self.store.create("LaCasa iPhone 1")
        output_dir = self.root / "qr"
        public_url = "https://lacasa-server.tail7a9c2.ts.net"
        expected = f"{public_url}/login#device={created.summary.device_id}.{created.secret}"
        with patch.object(qrcode.QRCode, "add_data", autospec=True, side_effect=qrcode.QRCode.add_data) as add_data:
            url_path, png_path = write_device_artifacts(created, public_url, output_dir)

        payload = add_data.call_args.args[1]
        self.assertTrue(hmac.compare_digest(payload, expected), "QR payload differs from fragment credential URL")
        self.assertTrue(hmac.compare_digest(url_path.read_text(), expected), "URL artifact payload differs")
        self.assertNotIn("?device=", payload)
        self.assertEqual(url_path.name, f"{created.summary.device_id}.url")
        self.assertEqual(png_path.name, f"{created.summary.device_id}.png")
        self.assertEqual(url_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(png_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output_dir.stat().st_mode & 0o777, 0o700)

    def test_qr_rejects_symlink_directory_or_existing_output_without_touching_target(self):
        from core.device_credentials import write_device_artifacts

        created = self.store.create("LaCasa iPhone 1")
        target_dir = self.root / "target"
        target_dir.mkdir(mode=0o700)
        marker = target_dir / "marker"
        marker.write_text("unchanged")
        output_dir = self.root / "qr"
        output_dir.symlink_to(target_dir, target_is_directory=True)
        with self.assertRaises(DeviceCredentialConfigError):
            write_device_artifacts(created, "https://example.test", output_dir)
        self.assertEqual(marker.read_text(), "unchanged")

        output_dir.unlink()
        output_dir.mkdir(mode=0o700)
        existing = output_dir / f"{created.summary.device_id}.url"
        existing.write_text("unchanged")
        existing.chmod(0o600)
        with self.assertRaises(DeviceCredentialConfigError):
            write_device_artifacts(created, "https://example.test", output_dir)
        self.assertEqual(existing.read_text(), "unchanged")

    def test_qr_failure_leaves_no_secret_in_exception_or_log(self):
        from core.device_credentials import write_device_artifacts
        import qrcode

        created = self.store.create("LaCasa iPhone 1")
        output_dir = self.root / "qr"
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger().addHandler(handler)
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with patch.object(qrcode.QRCode, "make", side_effect=RuntimeError("synthetic QR failure")), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), self.assertRaises(RuntimeError) as caught:
                write_device_artifacts(created, "https://example.test", output_dir)
        finally:
            logging.getLogger().removeHandler(handler)
        observed = str(caught.exception) + log_stream.getvalue() + stdout.getvalue() + stderr.getvalue()
        self.assertFalse(created.secret in observed, "secret leaked through an artifact failure")
        self.assertFalse(any(output_dir.glob("*")), "artifact failure left a partial secret artifact")

    def test_private_artifact_post_open_check_failure_removes_untracked_file(self):
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with patch("core.device_credentials._checked_regular", side_effect=RuntimeError("synthetic check failure")):
                with self.assertRaises(RuntimeError):
                    credentials._create_private_file(directory_fd, "untracked.url")
        finally:
            os.close(directory_fd)

        self.assertFalse((self.root / "untracked.url").exists())

    def test_artifact_cleanup_failure_is_not_suppressed(self):
        from core.device_credentials import write_device_artifacts
        import qrcode

        created = self.store.create("LaCasa iPhone 1")
        output_dir = self.root / "qr"
        with patch.object(qrcode.QRCode, "make", side_effect=RuntimeError("synthetic QR failure")), patch(
            "core.device_credentials.os.unlink", side_effect=OSError("synthetic cleanup failure")
        ):
            with self.assertRaises(DeviceCredentialConfigError):
                write_device_artifacts(created, "https://example.test", output_dir)
