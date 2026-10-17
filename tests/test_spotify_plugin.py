"""Tests for the headless Spotify control plugin (Task 5)."""
import importlib
import io
import sys
import unittest
from unittest.mock import patch

from core.spotify import SpotifyAuthorizationRequired

PLUGIN_MODULE = "plugins.spotify_control"


class _FakeController:
    """A real fake controller: records calls, returns German text, can explode."""

    def __init__(self):
        self.calls = []

    def perform(self, action, *, query=None, kind="track", volume_percent=None):
        self.calls.append((action, query, kind, volume_percent))
        if action == "__explode__":
            raise RuntimeError(
                "internal failure: access_token=SECRET_ACCESS "
                "refresh_token=SECRET_REFRESH authorization_code=SECRET_CODE "
                "code_verifier=SECRET_VERIFIER"
            )
        if action == "current":
            return "Auf lacasa läuft gerade 'Titel' von Künstler."
        return "German text unchanged"


def _load_plugin():
    return importlib.import_module(PLUGIN_MODULE)


class PluginSchemaTests(unittest.TestCase):
    def test_plugin_name(self):
        plugin = _load_plugin()
        self.assertEqual(plugin.PLUGIN["name"], "spotify_control")

    def test_action_enum(self):
        plugin = _load_plugin()
        enum = plugin.PLUGIN["parameters"]["properties"]["action"]["enum"]
        self.assertEqual(
            enum, ["play", "pause", "resume", "next", "previous", "volume", "current"]
        )

    def test_kind_enum_and_default(self):
        plugin = _load_plugin()
        kind = plugin.PLUGIN["parameters"]["properties"]["kind"]
        self.assertEqual(kind["enum"], ["track", "album", "artist"])
        self.assertEqual(kind.get("default"), "track")

    def test_volume_bounds(self):
        plugin = _load_plugin()
        vol = plugin.PLUGIN["parameters"]["properties"]["volume_percent"]
        self.assertEqual(vol["type"], "INTEGER")
        self.assertEqual(vol.get("minimum"), 0)
        self.assertEqual(vol.get("maximum"), 100)

    def test_required_is_only_action(self):
        plugin = _load_plugin()
        self.assertEqual(plugin.PLUGIN["parameters"]["required"], ["action"])


class PluginDispatchTests(unittest.TestCase):
    def _run(self, parameters):
        plugin = _load_plugin()
        fake = _FakeController()
        with patch.object(plugin, "get_controller", return_value=fake):
            result = plugin.run(parameters)
        return result, fake

    def test_play_calls_controller_once_with_query_and_kind(self):
        result, fake = self._run(
            {"action": "play", "query": "  Queen ", "kind": "album"}
        )
        self.assertEqual(result, "German text unchanged")
        self.assertEqual(len(fake.calls), 1)
        action, query, kind, volume = fake.calls[0]
        self.assertEqual(action, "play")
        self.assertEqual(query, "  Queen ")
        self.assertEqual(kind, "album")
        self.assertIsNone(volume)

    def test_play_defaults_kind_to_track(self):
        result, fake = self._run({"action": "play", "query": "Bohemian Rhapsody"})
        self.assertEqual(fake.calls[0][2], "track")

    def test_pause_resume_next_previous_current_dispatch(self):
        plugin = _load_plugin()
        fake = _FakeController()
        with patch.object(plugin, "get_controller", return_value=fake):
            for action in ("pause", "resume", "next", "previous", "current"):
                result = plugin.run({"action": action})
                self.assertEqual(result, "German text unchanged" if action != "current"
                                 else "Auf lacasa läuft gerade 'Titel' von Künstler.")
        self.assertEqual([c[0] for c in fake.calls],
                         ["pause", "resume", "next", "previous", "current"])

    def test_volume_passes_integer_percent(self):
        result, fake = self._run({"action": "volume", "volume_percent": 42})
        self.assertEqual(result, "German text unchanged")
        self.assertEqual(fake.calls[0], ("volume", None, "track", 42))

    def test_rejects_play_without_query_before_controller(self):
        plugin = _load_plugin()
        fake = _FakeController()
        with patch.object(plugin, "get_controller", return_value=fake) as gc:
            result = plugin.run({"action": "play"})
        gc.assert_not_called()
        self.assertEqual(len(fake.calls), 0)
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "German text unchanged")

    def test_rejects_play_with_empty_query(self):
        result, fake = self._run({"action": "play", "query": "   "})
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("query", result.lower())

    def test_rejects_invalid_kind(self):
        result, fake = self._run({"action": "play", "query": "x", "kind": "podcast"})
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("kind", result.lower())

    def test_rejects_volume_without_percent(self):
        result, fake = self._run({"action": "volume"})
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("volume_percent", result.lower())

    def test_rejects_volume_out_of_range(self):
        for value in (-1, 101, 1.5):
            result, fake = self._run({"action": "volume", "volume_percent": value})
            self.assertEqual(len(fake.calls), 0, f"action should be rejected for {value}")

    def test_rejects_volume_bool(self):
        result, fake = self._run({"action": "volume", "volume_percent": True})
        self.assertEqual(len(fake.calls), 0)

    def test_rejects_unknown_action(self):
        result, fake = self._run({"action": "bogus"})
        self.assertEqual(len(fake.calls), 0)
        self.assertIn("Aktion", result)

    def test_rejects_non_string_query(self):
        result, fake = self._run({"action": "play", "query": 123})
        self.assertEqual(len(fake.calls), 0)


class PluginSecretSanitizationTests(unittest.TestCase):
    def test_internal_exception_secrets_never_reach_result_or_output(self):
        plugin = _load_plugin()
        fake = _FakeController()
        captured = io.StringIO()
        with patch.object(plugin, "get_controller", return_value=fake):
            with patch("sys.stdout", captured), patch("sys.stderr", captured):
                result = plugin.run({"action": "__explode__"})
        for secret in ("SECRET_ACCESS", "SECRET_REFRESH", "SECRET_CODE",
                       "SECRET_VERIFIER", "access_token", "refresh_token",
                       "authorization_code", "code_verifier"):
            self.assertNotIn(secret, result)
            self.assertNotIn(secret, captured.getvalue())
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main()

class PluginAuthorizationRequiredTests(unittest.TestCase):
    def test_perform_authorization_required_becomes_german_reauth_text(self):
        plugin = _load_plugin()
        fake = _FakeController()
        secret = "SECRET_INTERNAL_REASON"

        def perform_raises(action, **kwargs):
            fake.calls.append((action,))
            raise SpotifyAuthorizationRequired(secret)

        fake.perform = perform_raises
        captured = io.StringIO()
        with patch.object(plugin, "get_controller", return_value=fake):
            with patch("sys.stdout", captured), patch("sys.stderr", captured):
                result = plugin.run({"action": "pause"})
        self.assertIn("Spotify muss einmal verbunden werden", result)
        self.assertNotIn(secret, result)
        self.assertNotIn(secret, captured.getvalue())
        self.assertNotIn("unerwarteter Fehler", result)
