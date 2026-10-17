"""The cannanas plugin: closed enum, reads, prepare previews, degraded readiness."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from core.cannanas_actions import CANNANAS_MODEL_ACTIONS
from core.cannanas_client import CannanasUnavailable
from core.cannanas_confirmation import ConfirmationCoordinator
from core.cannanas_host import CannanasHost
from core.cannanas_audit import CannanasAudit
from core.headless import tool_allowed, trusted_headless_registry
from core.turn_provenance import CHANNEL_PHONE_PCM, IngressEnvelope
from core.user_turn import FinalizedTurn
from plugins import cannanas as cannanas_plugin

HOST_ENV = {
    'JARVIS_CANNANAS_ADAPTER_COMMAND': '/opt/cannanas/adapter/venv/bin/cannanas-mcp-adapter',
    'JARVIS_CANNANAS_ADAPTER_ARGS': '',
    'JARVIS_CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
    'CANNANAS_API_KEY': 'dummy-not-real',
    'CANNANAS_CLUB_ID': 'club-fixed',
    'CANNANAS_STATE_DIR': 'SET-IN-TESTS',
    'CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
}


class FakeClient:
    """Recording stub of the persistent MCP client."""

    def __init__(self, results=None, fail_on=None):
        self.calls = []
        self.results = results or {}
        self.fail_on = fail_on or {}
        self.lock = threading.Lock()

    def call(self, tool, arguments, budget=None):
        with self.lock:
            self.calls.append({'tool': tool, 'arguments': dict(arguments)})
        if tool in self.fail_on:
            raise self.fail_on[tool]
        return dict(self.results.get(tool, {'ok': True}))


def make_turn(user_turn_id=20, *, device='device-durable-1', auth='auth-digest-1'):
    envelope = IngressEnvelope(stable_device_id=device, auth_session_id=auth,
                               capture_id='1:1', user_turn_id=user_turn_id,
                               channel=CHANNEL_PHONE_PCM, final=True)
    return FinalizedTurn(envelope=envelope, finalized_at=0.0,
                         identity=f'{envelope.channel}:{user_turn_id}')


def make_host(tmpdir, *, client=None, env=None):
    audit_file = tmpdir / 'host-audit.jsonl'
    full_env = dict(HOST_ENV)
    full_env['JARVIS_CANNANAS_AUDIT_FILE'] = str(audit_file)
    full_env['CANNANAS_STATE_DIR'] = str(tmpdir / 'state')
    full_env['CANNANAS_AUDIT_FILE'] = str(tmpdir / 'adapter-audit.jsonl')
    if env:
        full_env.update(env)
    host = CannanasHost(env=full_env, client=client,
                        audit=CannanasAudit(audit_file))
    return host


class PluginFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = FakeClient()
        self.host = make_host(self.root, client=self.client)
        self.turn = make_turn()

    def run_plugin(self, parameters, *, host=None, session_memory='unset'):
        return cannanas_plugin.run(
            parameters, session_memory=self.turn if session_memory == 'unset'
            else session_memory,
            host=host if host is not None else self.host)


class TestEnumClosedAtRegistry(PluginFixture):
    def test_registry_declares_exact_closed_enum_without_host_ops(self):
        registry = trusted_headless_registry()
        self.assertTrue(registry.has('cannanas'))
        declaration = registry.get_tool_declarations()
        by_name = {d['name']: d for d in declaration}
        self.assertEqual(sorted(by_name['cannanas']['parameters']['properties']['action']['enum']),
                         sorted(CANNANAS_MODEL_ACTIONS))
        self.assertNotIn('behavior', by_name['cannanas'])
        # belt and braces: the gate refuses host operations separately
        for action in ('confirm_pending', 'cancel_pending', 'confirm_plant_create'):
            self.assertFalse(tool_allowed('cannanas', {'action': action}))


class TestReadActionCallsAdapterAndProjects(PluginFixture):
    def test_read_maps_to_adapter_tool_and_returns_bounded_german_payload(self):
        self.client.results['count_plants_by_room'] = {
            'ok': True, 'room': 'Madrid', 'plant_count': 26, 'exact': True}
        result = self.run_plugin({'action': 'count_plants_by_room', 'room': 'Madrid'})
        self.assertEqual(self.client.calls[0]['tool'], 'count_plants_by_room')
        self.assertEqual(self.client.calls[0]['arguments'], {'room': 'Madrid'})
        self.assertEqual(result['summary'], 'Pflanzen im Raum Madrid: 26.')
        self.assertTrue(result['exact'])

    def test_read_limit_and_truncation_semantics(self):
        records = [{'name': f'Pflanze {i}', 'status': 'SEED'} for i in range(26)]
        self.client.results['find_plants'] = {
            'ok': True, 'plants': records, 'exact': True, 'truncated': True,
            'total': 26}
        result = self.run_plugin({'action': 'find_plants', 'query': 'Pflanze'})
        self.assertLessEqual(len(result['records']), 25)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['spoken_records'], 5)
        self.assertIn('Erste', result['summary'])

    def test_read_failure_is_one_bounded_german_notice(self):
        client = FakeClient(fail_on={'get_cannanas_overview':
                                     CannanasUnavailable('call: closedresourceerror')})
        host = make_host(self.root, client=client)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', False))
        self.assertNotIn('closedresourceerror', result['summary'])
        self.assertIn('Cannanas', result['summary'])


class TestPrepareStoresPendingAndReturnsPreview(PluginFixture):
    def test_prepare_response_maps_to_adapter_and_stores_pending(self):
        self.client.results['prepare_plant_create'] = {
            'ok': True,
            'requires_confirmation': True,
            'confirmation_token': 'opaque-token-1',
            'payload_hash': 'payload-hash-1',
            'preview': {'plant_name': 'Brain Damage', 'status': 'SEED',
                        'room': 'Madrid', 'strain': 'Brain Damage Strain'},
            'club_id': 'club-fixed',
        }
        result = self.run_plugin({
            'action': 'prepare_plant_create', 'plant_name': 'Brain Damage',
            'room': 'Madrid', 'status': 'SEED',
            'query': 'Brain Damage Strain'})
        tool, args = self.client.calls[0]['tool'], self.client.calls[0]['arguments']
        self.assertEqual(tool, 'prepare_plant_create')
        self.assertEqual(args['plant_name'], 'Brain Damage')
        binding = args['binding']
        self.assertEqual(binding['stable_device_id'], 'device-durable-1')
        self.assertEqual(binding['prepared_turn_id'], '20')
        self.assertEqual(binding['adapter_boot_id'], self.host.boot_id())
        pending = self.host.coordinator.current('device-durable-1')
        self.assertIsNotNone(pending)
        self.assertEqual(pending.payload_hash, 'payload-hash-1')
        self.assertEqual(pending.state, 'pending')
        self.assertNotEqual(pending.expires_at - pending.created_at, 0)
        # The model result never contains the token or club id.
        rendered = json.dumps(result)
        self.assertNotIn('opaque-token-1', rendered)
        self.assertNotIn('club-fixed', rendered)
        self.assertNotIn('payload-hash-1', rendered)
        self.assertIn('bitte bestätigen', result['summary'].lower())
        self.assertIsInstance(result['preview'], dict)


class TestPrepareZeroPOSTs(PluginFixture):
    def test_prepare_never_touches_a_confirm_tool(self):
        class GuardedClient(FakeClient):
            def call(self, tool, arguments, budget=None):
                if tool.startswith('confirm_'):
                    raise AssertionError(f'POST attempted during prepare: {tool}')
                return super().call(tool, arguments, budget)
        client = GuardedClient(results={'prepare_plant_create': {
            'ok': True, 'confirmation_token': 't', 'payload_hash': 'h',
            'preview': {'plant_name': 'X'}, 'club_id': 'club-fixed'}})
        host = make_host(self.root, client=client)
        result = self.run_plugin({'action': 'prepare_plant_create',
                                  'plant_name': 'X', 'room': 'Madrid',
                                  'status': 'SEED'}, host=host)
        self.assertTrue(result['requires_confirmation'])
        self.assertEqual([c['tool'] for c in client.calls], ['prepare_plant_create'])


class TestUnknownActionRefused(PluginFixture):
    def test_unknown_action_gets_fixed_german_refusal(self):
        result = self.run_plugin({'action': 'delete_plant'})
        self.assertFalse(result.get('ok', True))
        self.assertEqual(len(self.client.calls), 0)
        result = self.run_plugin({'action': 'confirm_pending'})
        self.assertFalse(result.get('ok', True))
        self.assertEqual(len(self.client.calls), 0)


class TestMissingConfigFailsBeforeNetwork(PluginFixture):
    def test_missing_configuration_names_only_the_variable(self):
        env = dict(HOST_ENV)
        env.pop('CANNANAS_API_KEY')
        host = CannanasHost(env=env, client=None)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', True))
        self.assertIn('CANNANAS_API_KEY', result['summary'])
        # Nothing else about configuration leaks, and no client was created.
        self.assertFalse(host.available())
        self.assertIsNone(host.client_if_started())

    def test_missing_host_audit_var_degrades_everything(self):
        env = dict(HOST_ENV)
        env.pop('JARVIS_CANNANAS_AUDIT_FILE')
        audit = CannanasAudit(self.root / 'unused.jsonl')
        host = CannanasHost(env=env, client=self.client, audit=audit)
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertIn('JARVIS_CANNANAS_AUDIT_FILE', result['summary'])


class TestDegradedCannanasKeepsJARVISAlive(PluginFixture):
    def test_degraded_host_returns_german_notice_not_exception(self):
        client = FakeClient(fail_on={'get_cannanas_overview':
                                     CannanasUnavailable('call: x')})
        host = make_host(self.root, client=client)
        host.mark_degraded('client: failure in tests')
        result = self.run_plugin({'action': 'overview'}, host=host)
        self.assertFalse(result.get('ok', True))
        self.assertIn('Cannanas', result['summary'])
        # The plugin never raises; JARVIS keeps running.
        self.assertTrue(True)


class TestCannabisStatusStillDenied(PluginFixture):
    def test_cannabis_status_denied_before_dispatch(self):
        self.assertFalse(tool_allowed('cannabis_status', {'action': 'status'}))
        registry = trusted_headless_registry()
        self.assertFalse(registry.has('cannabis_status'))


if __name__ == '__main__':
    unittest.main()