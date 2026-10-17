"""Turn finalization + tool-dispatch provenance, and the closed action gate."""
import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from ui_headless import JarvisUI

from core.cannanas_actions import (
    CANNANAS_MODEL_ACTIONS,
    CANNANAS_PREPARE_ACTIONS,
    CANNANAS_READ_ACTIONS,
)
from core.headless import tool_allowed
from core.turn_provenance import (
    CHANNEL_HTTP_TEXT,
    CHANNEL_PHONE_PCM,
    create_stable,
    final_variant,
)
from core.user_turn import FinalizedTurn, finalize_turn, turn_registry


class LiveFixture(unittest.TestCase):
    """Minimal JarvisLive without model connection, mirroring test_final_lifecycle."""

    def setUp(self):
        import main
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = root / 'synthetic.json'
        cfg.write_text(json.dumps({'gemini_api_key': 'synthetic-no-network'}))
        self.patches = [
            patch.dict(os.environ, {'JARVIS_HEADLESS': 'true', 'JARVIS_CAPTURE_SOURCE': 'phone',
                                    'JARVIS_PUBLIC_URL': 'https://example.test',
                                    'JARVIS_CONTROL_SOCKET': str(root / 'control.sock'),
                                    'JARVIS_DEVICE_CREDENTIALS': str(root / 'devices.json'),
                                    'JARVIS_DEVICE_QR_DIR': str(root / 'qr')}),
            patch.object(main, 'API_CONFIG_PATH', cfg),
            patch('memory.config_manager.CONFIG_FILE', cfg),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.live = main.JarvisLive(JarvisUI())
        turn_registry.clear()
        envelope = create_stable(CHANNEL_HTTP_TEXT, stable_device_id='device-durable',
                                 auth_session_id='auth-session-a', final=True)
        self.finalized = finalize_turn(envelope)


class RecordingRegistry:
    def __init__(self, has=True):
        self.received = {}
        self._has = has

    def has(self, name):
        return self._has

    def run(self, name, parameters, player=None, session_memory=None):
        self.received['name'] = name
        self.received['session_memory'] = session_memory
        return 'ok'

    def scheduling(self, name):
        return None


class TestFinalizedTurnIdentityStable(unittest.TestCase):
    def test_identity_is_frozen_per_turn_and_turns_increase(self):
        a = create_stable(CHANNEL_HTTP_TEXT, stable_device_id='dev', auth_session_id='s',
                          final=True)
        turn_a = finalize_turn(a)
        b = create_stable(CHANNEL_HTTP_TEXT, stable_device_id='dev', auth_session_id='s',
                          final=True)
        turn_b = finalize_turn(b)
        self.assertGreater(turn_b.user_turn_id, turn_a.user_turn_id)
        self.assertEqual(turn_a.identity, f'{CHANNEL_HTTP_TEXT}:{a.user_turn_id}')
        # Identity is stable through the turn: re-reading never changes it.
        self.assertEqual(turn_a.identity, turn_a.identity)
        self.assertIsInstance(turn_a, FinalizedTurn)
        self.assertEqual(turn_a.user_turn_id, a.user_turn_id)
        self.assertEqual(turn_b.user_turn_id, b.user_turn_id)


class TestToolCallBeforeFinalizationRejected(LiveFixture):
    def test_tool_call_without_finalized_turn_is_rejected(self):
        self.live._finalized_turn = None
        registries = RecordingRegistry(), RecordingRegistry()
        self.live._plugin_registry = registries[0]
        self.live._action_registry = registries[1]
        fc = NS(name='cannanas', args={'action': 'overview'}, id='fc-1')
        response = asyncio.run(self.live._execute_tool(fc))
        self.assertIn('no finalized user turn', response.response['error'])
        self.assertNotIn('session_memory', registries[0].received)


class TestDispatchReplacesSessionMemoryNoneForHeadless(LiveFixture):
    def test_registry_receives_finalized_turn_context(self):
        finalized = self.finalized
        self.live._finalized_turn = finalized
        plugin_registry, action_registry = RecordingRegistry(), RecordingRegistry(has=False)
        self.live._plugin_registry = plugin_registry
        self.live._action_registry = action_registry
        fc = NS(name='cannanas', args={'action': 'overview'}, id='fc-2')
        response = asyncio.run(self.live._execute_tool(fc))
        self.assertEqual(plugin_registry.received['name'], 'cannanas')
        self.assertIs(plugin_registry.received['session_memory'], finalized)
        self.assertEqual(response.response['result'], 'ok')


class TestResponseGenerationIsNotUserTurnIdentity(unittest.TestCase):
    def test_non_final_envelopes_never_finalize_and_registry_stays_put(self):
        pending = create_stable(CHANNEL_HTTP_TEXT, stable_device_id='dev',
                                auth_session_id='s', final=False)
        with self.assertRaises(ValueError):
            finalize_turn(pending)
        turn = turn_registry.current(CHANNEL_HTTP_TEXT)
        other = create_stable(CHANNEL_HTTP_TEXT, stable_device_id='dev',
                              auth_session_id='s', final=False)
        with self.assertRaises(ValueError):
            finalize_turn(other)
        # A response generation stamps nothing finalized: identity unchanged.
        if turn is not None:
            self.assertIs(turn_registry.current(CHANNEL_HTTP_TEXT), turn)

    def test_phone_envelope_finalizes_only_at_stream_end(self):
        pending_pcm = create_stable(CHANNEL_PHONE_PCM, stable_device_id='dev',
                                    auth_session_id='s', capture_id='0:1', final=False)
        with self.assertRaises(ValueError):
            finalize_turn(pending_pcm)
        end = finalize_turn(final_variant(pending_pcm))
        self.assertEqual(end.channel, CHANNEL_PHONE_PCM)
        self.assertEqual(end.user_turn_id, pending_pcm.user_turn_id)


class TestToolAllowedRejectsUnknownCannanasAction(unittest.TestCase):
    def test_only_closed_enum_actions_pass_the_gate(self):
        for action in CANNANAS_READ_ACTIONS | CANNANAS_PREPARE_ACTIONS:
            self.assertTrue(tool_allowed('cannanas', {'action': action}))
        for action in ('delete_plant', 'confirm_pending', 'cancel_pending',
                       'unknown_mcp_tool', 'http_get', ''):
            self.assertFalse(tool_allowed('cannanas', {'action': action}))
        self.assertFalse(tool_allowed('cannanas', {}))
        self.assertFalse(tool_allowed('cannabis_status', {'action': 'status'}))
        # Existing allowlist untouched.
        self.assertTrue(tool_allowed('home_assistant', {'action': 'temperature'}))
        self.assertTrue(tool_allowed('spotify_control', {'action': 'play'}))


class TestConfirmPendingNotInModelEnum(unittest.TestCase):
    def test_confirm_and_cancel_are_never_model_actions(self):
        self.assertNotIn('confirm_pending', CANNANAS_MODEL_ACTIONS)
        self.assertNotIn('cancel_pending', CANNANAS_MODEL_ACTIONS)
        self.assertEqual(CANNANAS_MODEL_ACTIONS, CANNANAS_READ_ACTIONS | CANNANAS_PREPARE_ACTIONS)
        # The enum is a frozen set of exactly the Release-1 vocabulary.
        self.assertEqual(len(CANNANAS_MODEL_ACTIONS), 14)
        self.assertTrue(all(a.startswith(('prepare_')) or a.startswith(('find_', 'list_',
                          'count_', 'overview')) for a in CANNANAS_MODEL_ACTIONS))


if __name__ == '__main__':
    unittest.main()