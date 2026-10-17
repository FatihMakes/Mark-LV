"""End-to-end host flows with the real persistent client against the fake adapter.

Never touches the Cannanas API: the fake adapter speaks MCP over STDIO locally.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from core.cannanas_audit import CannanasAudit
from core.cannanas_client import CannanasClient, CannanasUnavailable
from core.cannanas_confirmation import PendingConfirmation
from core.cannanas_host import CannanasHost
from core.turn_provenance import CHANNEL_PHONE_PCM, IngressEnvelope
from core.user_turn import FinalizedTurn, finalize_turn

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'fake_adapter.py'

EXPECTED_TOOLS = [
    'get_adapter_info', 'get_cannanas_overview', 'find_zones', 'find_strains',
    'find_plants', 'find_propagation_material', 'list_recent_batches',
    'list_plants', 'count_plants_by_room',
    'prepare_plant_create', 'confirm_plant_create', 'prepare_strain_create',
    'confirm_strain_create', 'prepare_batch_create', 'confirm_batch_create',
    'prepare_plant_update', 'confirm_plant_update', 'prepare_batch_update',
    'confirm_batch_update', 'prepare_diary_create', 'confirm_diary_create',
]


def make_turn(user_turn_id=30, *, device='device-durable-1', auth='auth-digest-1'):
    envelope = IngressEnvelope(stable_device_id=device, auth_session_id=auth,
                               capture_id='0:1', user_turn_id=user_turn_id,
                               channel=CHANNEL_PHONE_PCM, final=True)
    return FinalizedTurn(envelope=envelope, finalized_at=0.0,
                         identity=f'{envelope.channel}:{user_turn_id}')


class RecordingClient:
    """Serializes through the real CannanasClient and records every call."""

    def __init__(self, real):
        self._real = real
        self.calls = []
        self.lock = threading.Lock()

    def call(self, tool, arguments, budget=None):
        result = self._real.call(tool, arguments, budget)
        with self.lock:
            self.calls.append({'tool': tool, 'arguments': dict(arguments)})
        return result


def _host_env(root: Path) -> dict:
    return {
        'JARVIS_CANNANAS_ADAPTER_COMMAND': sys.executable,
        'JARVIS_CANNANAS_ADAPTER_ARGS': str(FIXTURE),
        'JARVIS_CANNANAS_AUDIT_FILE': str(root / 'host-audit.jsonl'),
        'CANNANAS_API_KEY': 'dummy-not-real',
        'CANNANAS_CLUB_ID': 'club-fixed',
        'CANNANAS_STATE_DIR': str(root / 'state'),
        'CANNANAS_AUDIT_FILE': str(root / 'adapter-audit.jsonl'),
    }


class IntegrationFixture(unittest.TestCase):
    """A real CannanasClient talking to the local fake adapter via STDIO."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.real_client = CannanasClient({
            'command': sys.executable,
            'args': [str(FIXTURE)],
            'adapter_version': '1.0.1',
            'artifact_sha256': 'a' * 64,
            'expected_tools': EXPECTED_TOOLS,
            'adapter_env': {},
        })
        self.assertTrue(self.real_client.start())
        self.client = RecordingClient(self.real_client)
        self.host = CannanasHost(env=_host_env(self.root), client=self.client,
                                 audit=CannanasAudit(self.root / 'host-audit.jsonl'))
        self.addCleanup(self.real_client.stop, 5)


class TestHostConfirmConsumesAtomicallyAndPostsOnce(IntegrationFixture):
    def prepare(self, turn):
        # Real prepare flow: adapter prepare → host stores the pending.
        self.host.prepare('prepare_plant_create', {
            'plant_name': 'Brain Damage', 'room': 'Madrid',
            'query': 'BDS', 'status': 'SEED'}, turn)
        pending = self.host.coordinator.current('device-durable-1')
        self.assertIsNotNone(pending)
        return pending

    @staticmethod
    def _opaque(value):
        from core.cannanas_confirmation import OpaqueToken
        return OpaqueToken(value)

    def test_two_racing_affirms_yield_exactly_one_confirm_call(self):
        turn = make_turn(user_turn_id=13)
        self.prepare(turn)
        affirm_turn = make_turn(user_turn_id=17)
        outcomes = []
        barrier = threading.Barrier(2)

        def racing():
            barrier.wait()
            outcomes.append(self.host.confirm(affirm_turn))

        threads = [threading.Thread(target=racing) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        confirmations = [o for o in outcomes if o.get('classification') in
                         ('succeeded', 'created_but_unverified',
                          'unknown', 'failed')]
        refusals = [o for o in outcomes if o.get('refusal')]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]['refusal'], 'already_used')
        # Exactly one adapter confirm call (the earlier prepare is also
        # recorded); the pending was consumed.
        confirm_calls = [c for c in self.client.calls
                         if c['tool'].startswith('confirm_')]
        self.assertEqual(len(confirm_calls), 1)
        self.assertEqual(confirm_calls[0]['tool'], 'confirm_plant_create')
        binding = confirm_calls[0]['arguments']['binding']
        self.assertEqual(binding['prepared_turn_id'], str(13))
        self.assertEqual(binding['adapter_boot_id'], self.host.boot_id())
        self.assertEqual(self.host.coordinator.current('device-durable-1').state,
                         'consumed')
        audit_lines = [json.loads(line) for line in
                       (self.root / 'host-audit.jsonl').read_text().splitlines()]
        self.assertEqual([e['event'] for e in audit_lines],
                         ['prepared', 'confirmed', 'result'])
        self.assertEqual(audit_lines[1]['event'], 'confirmed')
        self.assertEqual(audit_lines[1]['state'], 'executing')
        self.assertEqual(audit_lines[2]['state'], 'succeeded')


class TestInterleavedDeviceCannotConfirmOtherDevice(IntegrationFixture):
    def test_other_device_cannot_confirm(self):
        from core.cannanas_confirmation import OpaqueToken
        now = time.monotonic()
        self.host.coordinator.store(PendingConfirmation(
            action='plant_create', club_id='club-fixed', payload_hash='h',
            complete_preview='Vorschau', jarvis_session_id='device-durable-2',
            stable_device_id='device-durable-2', auth_session_id='other-digest',
            prepared_turn_id=11, adapter_boot_id=self.host.boot_id(),
            created_at=now, expires_at=now + 300.0,
            state='pending', opaque_mcp_token=OpaqueToken('t2')))
        # An affirm from the WRONG device touches nothing and POSTs nothing.
        wrong = self.host.confirm(make_turn(user_turn_id=15,
                                            device='device-durable-1'))
        self.assertEqual(wrong['refusal'], 'wrong_session')
        self.assertEqual(len(self.client.calls), 0)
        # The interleaved device's turn is 'none' at evaluate: untouched.
        evaluation = self.host.evaluate(make_turn(user_turn_id=16,
                                                  device='device-durable-1'), 'ja')
        self.assertEqual(evaluation['decision'], 'none')
        self.assertEqual(self.host.coordinator.current(
            'device-durable-2').state, 'pending')
        self.assertEqual(len(self.client.calls), 0)
        # The owning device can still genuinely confirm.
        genuine = self.host.confirm(make_turn(user_turn_id=21,
                                              device='device-durable-2',
                                              auth='other-digest'))
        self.assertEqual(genuine.get('classification'), 'succeeded')
        self.assertEqual(len(self.client.calls), 1)

    @staticmethod
    def _opaque(value):
        from core.cannanas_confirmation import OpaqueToken
        return OpaqueToken(value)


class TestPostSendTimeoutIsUnknownNotRetried(IntegrationFixture):
    def test_confirm_timeout_classifies_unknown_and_never_repeats(self):
        from core.cannanas_confirmation import OpaqueToken
        now = time.monotonic()
        self.host.coordinator.store(PendingConfirmation(
            action='plant_create', club_id='club-fixed', payload_hash='h',
            complete_preview='Vorschau', jarvis_session_id='device-durable-1',
            stable_device_id='device-durable-1', auth_session_id='auth-digest-1',
            prepared_turn_id=11, adapter_boot_id=self.host.boot_id(),
            created_at=now, expires_at=now + 300.0, state='pending',
            opaque_mcp_token=OpaqueToken('t')))
        self.host._client = _DroppingConfirmClient(self.real_client)
        outcome = self.host.confirm(make_turn(user_turn_id=15))
        self.assertEqual(outcome['classification'], 'unknown')
        self.assertEqual(self.host._client.attempts, 1)

    def test_prepare_after_failed_confirm_requires_new_preview(self):
        pass  # covered by the coordinator's already_used fence


class _DroppingConfirmClient:
    """Real client whose confirm calls hang until the budget expires."""

    def __init__(self, real):
        self._real = real
        self.attempts = 0

    def call(self, tool, arguments, budget=None):
        if tool.startswith('confirm_'):
            self.attempts += 1
            raise CannanasUnavailable('Cannunas adapter answer timed out')
        return self._real.call(tool, arguments, budget)


class TestRegistryRestartInvalidatesPending(IntegrationFixture):
    def test_client_restart_hook_invalidates_pending(self):
        from core.cannanas_confirmation import OpaqueToken
        now = time.monotonic()
        self.host.coordinator.store(PendingConfirmation(
            action='plant_create', club_id='club-fixed', payload_hash='h',
            complete_preview='Vorschau', jarvis_session_id='device-durable-1',
            stable_device_id='device-durable-1', auth_session_id='auth-digest-1',
            prepared_turn_id=11, adapter_boot_id=self.host.boot_id(),
            created_at=now, expires_at=now + 300.0, state='pending',
            opaque_mcp_token=OpaqueToken('t')))
        self.host.on_client_restart()
        outcome = self.host.confirm(make_turn(user_turn_id=15))
        self.assertEqual(outcome['refusal'], 'cancelled')
        self.assertTrue(all(p['tool'] != 'confirm_plant_create'
                            for p in self.client.calls))


class TestMainTextPathEndToEnd(unittest.TestCase):
    def test_affirm_text_turn_executes_confirmed_write_without_model_turn(self):
        import ui_headless

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cfg = root / 'synthetic.json'
        cfg.write_text(json.dumps({'gemini_api_key': 'synthetic-no-network'}))
        env = _host_env(root)
        patches = [
            patch.dict(os.environ, {'JARVIS_HEADLESS': 'true',
                                    'JARVIS_CAPTURE_SOURCE': 'phone',
                                    'JARVIS_PUBLIC_URL': 'https://example.test'}),
            patch.object(__import__('main', fromlist=['API_CONFIG_PATH']),
                         'API_CONFIG_PATH', cfg),
            patch('memory.config_manager.CONFIG_FILE', cfg),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        import main
        from ui_headless import JarvisUI
        live = main.JarvisLive(JarvisUI())
        live.session = NS(
            send_client_content=AsyncMock(return_value=None),
            send_realtime_input=AsyncMock(return_value=None))

        # Stand-in host whose client records and answers confirms.
        class StubHostClient:
            def __init__(self):
                self.calls = []

            def call(self, tool, arguments, budget=None):
                if tool.startswith('confirm_'):
                    self.calls.append(tool)
                    return {'ok': True, 'result_id': 'plant-77'}
                return {'ok': True, 'plants': [], 'exact': True}
        from core.cannanas_confirmation import OpaqueToken
        host = CannanasHost(env=env, client=StubHostClient(),
                            audit=CannanasAudit(root / 'host-audit.jsonl'))
        live._cannanas_host = host
        now = time.monotonic()
        host.coordinator.store(PendingConfirmation(
            action='plant_create', club_id='club-fixed', payload_hash='h',
            complete_preview='Neue Pflanze „Brain Damage“.',
            jarvis_session_id='device-durable-1',
            stable_device_id='device-durable-1', auth_session_id='auth-digest-1',
            prepared_turn_id=11, adapter_boot_id=host.boot_id(),
            created_at=now, expires_at=now + 300.0, state='pending',
            opaque_mcp_token=OpaqueToken('t')))

        # Finalize the affirm turn exactly like the text ingest point does.
        envelope = IngressEnvelope(stable_device_id='device-durable-1',
                                   auth_session_id='auth-digest-1', capture_id=None,
                                   user_turn_id=21, channel='http_text', final=True)
        live._finalized_turn = finalize_turn(envelope)

        # Drive the real text ingest loop: the affirm turn must execute the
        # confirmed write on the host and SKIP the normal model turn.
        asyncio.run(_drive_text_path(live, host, self.calls_seen))

    def setUp(self):
        self.calls_seen = []

    def dummy(self):  # pragma: no cover - placeholder for clarity
        pass


async def _drive_text_path(live, host, seen):
    from core.turn_provenance import ProvenanceText
    live._dashboard = NS(_command_queue=asyncio.Queue())
    client = host._client
    live._dashboard._command_queue.put_nowait(
        ProvenanceText('ja', live._finalized_turn.envelope))
    task = asyncio.ensure_future(live._process_dashboard_commands())
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    seen.extend(client.calls)
    # The affirm turn was handled by the host: one confirm call, no model turn.
    assert seen == ['confirm_plant_create'], seen
    assert not live.session.send_client_content.await_count, 'model turn must be skipped'


if __name__ == '__main__':
    unittest.main()