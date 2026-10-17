"""Regression checks: local credentials must never be picked up by git add."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SecretIgnoreTests(unittest.TestCase):
    def test_credentials_are_ignored(self):
        paths = [
            "config/api_keys.json",
            "config/certs/private.key",
            "config/whatsapp_web/session.json",
            "plugins/token-test.json",
            "plugins/client_secret-test.json",
            ".env",
            "config/spotify_oauth.json",
            "config/device_credentials.json",
            "config/device_qr/example.url",
            "config/device_qr/example.png",
        ]
        for path in paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", path],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode, 0, f"Not ignored: {path}"
                )

    def test_local_config_is_not_tracked(self):
        result = subprocess.run(
            ["git", "ls-files", "--", "config/api_keys.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, "")

    def test_spotify_oauth_is_ignored_and_untracked(self):
        # Explicit ignore check
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "config/spotify_oauth.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode, 0, "config/spotify_oauth.json should be ignored"
        )
        # Untracked check
        result = subprocess.run(
            ["git", "ls-files", "--", "config/spotify_oauth.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout, "")
