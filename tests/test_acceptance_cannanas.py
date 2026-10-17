"""Acceptance suite for the Cannanas JARVIS integration.

One test per design criterion — Mark-LV integration 15–22 plus the restart
fence (27) — see docs/superpowers/specs/2026-10-16-cannanas-jarvis-integration-design.md.

No test here touches the Cannanas API. The only live-dependent checks are
gated behind ``JARVIS_CANNANAS_ACCEPTANCE_LIVE == 'ops-approved'`` (set by the
operator on the target host) and speak exclusively to the locally pinned,
verified adapter process — never to the live API.
"""
import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from core.cannanas_actions import CANNANAS_MODEL_ACTIONS
from core.cannanas_audit import CannanasAudit
from core.cannanas_client import (
    ADAPTER_TOOLS,
    PINNED_ADAPTER_VERSION,
    PINNED_WHEEL_SHA256,
    CannanasClient,
)
from core.cannanas_confirmation import ConfirmationCoordinator
from core.cannanas_host import (
    REFUSAL_SPOKEN,
    SPOKEN_RECORD_LIMIT,
    CannanasHost,
    missing_config_variables,
    read_tool_call,
)
from core.headless import tool_allowed, trusted_headless_registry
from core.turn_provenance import (
    CHANNEL_CONTROL_WS_TEXT,
    CHANNEL_HTTP_TEXT,
    CHANNEL_PHONE_PCM,
    IngressEnvelope,
    auth_session_id_of,
)
from core.user_turn import FinalizedTurn, finalize_turn
from dashboard.access import SessionRegistry
from plugins import cannanas as cannanas_plugin

LIVE_GATE_ENV = 'JARVIS_CANNANAS_ACCEPTANCE_LIVE'
LIVE_GATE_VALUE = 'ops-approved'


def import_test_module(name):
    """Import an existing test module regardless of the discovery invocation."""
    import sys
    tests_dir = str(Path(__file__).resolve().parent)
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    for qualified in (name, f'tests.{name}'):
        try:
            return importlib.import_module(qualified)
        except ModuleNotFoundError:
            continue
    raise AssertionError(f'existing test module not importable: {name}')


HOST_ENV = {
    'JARVIS_CANNANAS_ADAPTER_COMMAND':
        '/opt/cannanas/adapter/venv/bin/cannanas-mcp-adapter',
    'JARVIS_CANNANAS_ADAPTER_ARGS': '',
    'JARVIS_CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
    'CANNANAS_API_KEY': 'dummy-not-real',
    'CANNANAS_CLUB_ID': 'club-fixed',
    'CANNANAS_STATE_DIR': 'SET-IN-TESTS',
    'CANNANAS_AUDIT_FILE': 'SET-IN-TESTS',
}

PREPARE_RESULT = {
    'ok': True,
    'requires_confirmation': True,
    'confirmation_token': 'opaque-token-dummy',
    'payload_hash': 'payload-hash-dummy',
    'preview': {'plant_name': 'Testpflanze', 'status': 'SEED',
                'room': 'Wachraum', 'strain': 'Testsorte'},
    'club_id': 'club-fixed',
}


class FakeClient:
    """Recording stub of the persistent MCP client."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}
        self.lock = threading.Lock()

    def call(self, tool, arguments, budget=None):
        with self.lock:
            self.calls.append({'tool': tool, 'arguments': dict(arguments)})
        return dict(self.results.get(tool, {'ok': True}))


def make_envelope(channel, user_turn_id, *, stable_device_id='device-durable-1',
                  auth_session_id='auth-digest-1',
                  capture_id='2:1', final=True) -> IngressEnvelope:
    return IngressEnvelope(stable_device_id=stable_device_id,
                           auth_session_id=auth_session_id,
                           capture_id=capture_id, user_turn_id=user_turn_id,
                           channel=channel, final=final)


def make_turn(channel=CHANNEL_PHONE_PCM, user_turn_id=40,
              **envelope_kwargs) -> FinalizedTurn:
    envelope = make_envelope(channel, user_turn_id, **envelope_kwargs)
    return FinalizedTurn(envelope=envelope, finalized_at=0.0,
                         identity=f'{envelope.channel}:{envelope.user_turn_id}')


class AcceptanceFixture(unittest.TestCase):
    """One host with a recording client; every fixture result is dummy-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = FakeClient()
        self.host = self.make_host()
        self.turn = make_turn()

    def make_host(self, client=None, coordinator=None):
        env = dict(HOST_ENV)
        env['JARVIS_CANNANAS_AUDIT_FILE'] = str(self.root / 'host-audit.jsonl')
        env['CANNANAS_STATE_DIR'] = str(self.root / 'state')
        env['CANNANAS_AUDIT_FILE'] = str(self.root / 'adapter-audit.jsonl')
        return CannanasHost(env=env, client=self.client if client is None else client,
                            audit=CannanasAudit(env['JARVIS_CANNANAS_AUDIT_FILE']),
                            coordinator=coordinator)

    def run_plugin(self, parameters, *, host=None, turn=None):
        return cannanas_plugin.run(
            parameters, player=None,
            session_memory=turn if turn is not None else self.turn,
            host=host if host is not None else self.host)

    def prepare_pending(self, *, turn=None):
        turn = turn or self.turn
        self.client.results['prepare_plant_create'] = dict(PREPARE_RESULT)
        result = self.run_plugin(
            {'action': 'prepare_plant_create', 'plant_name': 'Testpflanze',
             'room': 'Wachraum', 'status': 'SEED', 'query': 'Testsorte'},
            turn=turn)
        self.assertTrue(result.get('ok'), result)
        pending = self.host.coordinator.current(turn.stable_device_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.state, 'pending')
        return pending


# ── Criterion 15: only approved read-only trust set in the registry ────────

class TestCriterion15ClosedRegistry(AcceptanceFixture):
    def test_registry_contains_only_approved_read_only_trust_set(self):
        registry = trusted_headless_registry()
        declaration = {d['name']: d for d in registry.get_tool_declarations()}
        self.assertEqual(set(declaration),
                         {'home_assistant', 'spotify_control', 'cannanas'})
        # Home Assistant is read-only with exactly the three approved actions;
        # there is no write/switch parameter left on the declaration.
        ha = declaration['home_assistant']['parameters']['properties']['action']
        self.assertEqual(sorted(ha['enum']), ['list', 'query', 'temperature'])
        self.assertNotIn('value', declaration['home_assistant']['parameters']['properties'])
        self.assertIn('Read-only', declaration['home_assistant']['description'])
        # Spotify keeps exactly its seven approved actions.
        spotify = declaration['spotify_control']['parameters']['properties']['action']
        self.assertEqual(sorted(spotify['enum']),
                         ['current', 'next', 'pause', 'play', 'previous',
                          'resume', 'volume'])
        # Cannanas is the closed enum, model-facing only, with no host
        # operation (confirm_*/adapter bookkeeping) anywhere in it.
        enum = declaration['cannanas']['parameters']['properties']['action']['enum']
        self.assertEqual(sorted(enum), sorted(CANNANAS_MODEL_ACTIONS))
        self.assertNotIn('behavior', declaration['cannanas'])
        for forbidden in ('confirm_plant_create', 'confirm_diary_create',
                          'get_adapter_info'):
            self.assertNotIn(forbidden, enum)


# ── Criterion 16: denial before any dispatch ───────────────────────────────

class TestCriterion16DeniedBeforeDispatch(AcceptanceFixture):
    def test_cannabis_status_and_unapproved_paths_are_denied_before_dispatch(self):
        # The standalone status action is not trusted anywhere.
        self.assertFalse(tool_allowed('cannabis_status', {'action': 'status'}))
        registry = trusted_headless_registry()
        self.assertFalse(registry.has('cannabis_status'))
        # Excluded Home Assistant / Spotify write actions are denied.
        for name, args in (
                ('home_assistant', {'action': 'turn_on'}),
                ('home_assistant', {'action': 'set_temperature', 'value': 30}),
                ('spotify_control', {'action': 'playlist_create'}),
                ('spotify_control', {'action': 'seek'})):
            self.assertFalse(tool_allowed(name, args), (name, args))
        # Cannanas: host operations, HTTP/MCP paths and arbitrary actions are
        # denied before any dispatch — with or without extra parameters.
        for action in ('confirm_plant_create', 'cancel_pending', 'confirm_pending',
                       'mcp__find_plants', 'http.get', 'prepare_destructive_purge',
                       ''):
            self.assertFalse(tool_allowed('cannanas', {'action': action}), action)
            self.assertFalse(tool_allowed('cannanas', {'action': action, 'path': '/'}),
                             action)
        # Everything outside the trusted set stays out by default.
        for name in ('shell', 'mcp', 'http_fetch', 'cannabis_status'):
            self.assertFalse(tool_allowed(name, {}))
        # The plugin itself refuses an unknown action without touching the host.
        class GuardedHost:
            def call_read(self, *a, **k):
                raise AssertionError('unknown action reached the host')

            def prepare(self, *a, **k):
                raise AssertionError('unknown action reached the host')

        result = self.run_plugin({'action': 'raw_mcp_call'}, host=GuardedHost())
        self.assertFalse(result['ok'])
        self.assertIn('nicht freigegeben', result['summary'])


# ── Criterion 17: bounded response with truncation and next page ───────────

class TestCriterion17BoundedRecords(AcceptanceFixture):
    def test_26_record_response_returns_at_most_25_structured_and_5_spoken(self):
        records = [{'name': f'Pflanze {i}', 'status': 'SEED'}
                   for i in range(26)]
        records.append({'bearer_token': 'leak-me-not', 'name': 'Geheimnis'})
        self.client.results['find_plants'] = {
            'ok': True, 'plants': records, 'exact': True, 'truncated': True,
            'total': 27}
        result = self.run_plugin({'action': 'find_plants', 'query': 'Pflanze'})
        self.assertEqual(len(result['records']), 25)
        self.assertEqual(result['spoken_records'], SPOKEN_RECORD_LIMIT)
        self.assertEqual(result['total_records'], 27)
        self.assertTrue(result['truncated'])
        # Exactly the first five names are spoken; the rest stays structured.
        summary = result['summary']
        self.assertIn(', '.join(f'Pflanze {i}' for i in range(5)), summary)
        self.assertNotIn('Pflanze 5', summary)
        self.assertIn('Liste gekürzt', summary)
        # Secret-bearing keys are stripped from every structured record.
        rendered = json.dumps(result)
        self.assertNotIn('leak-me-not', rendered)
        for record in result['records']:
            self.assertNotIn('bearer_token', record)

    def test_truncation_and_next_page_semantics(self):
        # Next-page behaviour is a fresh bounded tool call: the count parameter
        # is clamped into [1, 25]; nothing drains unbounded.
        self.assertEqual(read_tool_call('find_plants',
                                        {'count': 50, 'query': 'x'})[1]['limit'], 25)
        self.assertEqual(read_tool_call('find_plants', {'count': 0})[1]['limit'], 1)
        # 26 records with truncation=True mark the list as continuing and
        # non-exact totals are qualified ("mindestens").
        records = [{'name': f'Pflanze {i}'} for i in range(26)]
        self.client.results['list_plants'] = {
            'ok': True, 'plants': records, 'exact': False, 'truncated': True}
        result = self.run_plugin({'action': 'list_plants'})
        self.assertTrue(result['truncated'])
        self.assertFalse(result['exact'])
        self.assertIn('mindestens', result['summary'])
        # and the next-page request itself stays inside the bound.
        self.client.calls.clear()
        self.run_plugin({'action': 'list_plants', 'count': 999})
        self.assertEqual(self.client.calls[0]['arguments']['limit'], 25)


# ── Criterion 18: STOP/interruption cannot write ───────────────────────────

class TestCriterion18StopCannotWrite(AcceptanceFixture):
    def test_interrupted_or_stopped_turn_never_produces_a_write(self):
        pending = self.prepare_pending()
        # (a) An interrupted capture is never final: it cannot finalize a user
        # turn, so it can never drive the confirm flow.
        interrupted = make_envelope(CHANNEL_PHONE_PCM, user_turn_id=41, final=False)
        with self.assertRaises(ValueError):
            finalize_turn(interrupted)
        # (b) A STOP utterance on a finalized turn cancels the pending with
        # zero adapter POSTs.
        self.client.calls.clear()
        evaluation = self.host.evaluate(self.turn, 'stop')
        self.assertEqual(evaluation['decision'], 'deny')
        self.assertTrue(evaluation['handled'])
        self.assertEqual(self.client.calls, [])
        # The cancelled pending stays a replay fence.
        executing, refusal = self.host.coordinator.consume(
            'device-durable-1', self.turn,
            {'action': pending.action, 'club_id': pending.club_id,
             'payload_hash': pending.payload_hash,
             'adapter_boot_id': pending.adapter_boot_id})
        self.assertIsNone(executing)
        self.assertEqual(refusal, 'cancelled')
        # (c) No finalized turn (input still in flight): the host evaluates
        # 'none' and nothing is written.
        result = self.host.evaluate(None, 'ja')
        self.assertEqual(result['decision'], 'none')
        self.assertFalse(result['handled'])
        self.assertEqual(self.client.calls, [])


# ── Criterion 19: existing Spotify/HA phone suite remains green ────────────

class TestCriterion19ExistingSuitesRemainGreen(AcceptanceFixture):
    def test_existing_spotify_and_phone_paths_keep_their_registry_contract(self):
        # The structural half of the criterion: the established trusted set is
        # still dispatchable for approved actions and for nothing else.
        registry = trusted_headless_registry()
        self.assertTrue(tool_allowed('home_assistant', {'action': 'query'}))
        self.assertTrue(tool_allowed('spotify_control',
                                     {'action': 'play', 'type': 'track'}))
        self.assertTrue(registry.has('home_assistant'))
        self.assertTrue(registry.has('spotify_control'))
        # The suite-level half: every established module still imports and
        # collects (it runs green in the same full unittest run).
        for module in ('test_headless', 'test_device_access',
                       'test_playback_ducking', 'test_spotify_plugin',
                       'test_spotify'):
            spec = import_test_module(module)
            self.assertTrue(hasattr(spec, 'unittest'), module)


# ── Criterion 20: provenance survives dispatch on all three channels ───────

class TestCriterion20ProvenanceThroughDispatch(AcceptanceFixture):
    def test_all_three_channels_preserve_the_four_provenance_values(self):
        from core.cannanas_host import prepare_binding_for
        for channel in (CHANNEL_HTTP_TEXT, CHANNEL_CONTROL_WS_TEXT,
                        CHANNEL_PHONE_PCM):
            device, auth, turn_id = f'device-{channel}', f'auth-{channel}', 50
            finalized = finalize_turn(make_envelope(
                channel, turn_id, stable_device_id=device,
                auth_session_id=auth, capture_id='9:7'))
            binding = prepare_binding_for(finalized)
            self.assertIsNotNone(binding, channel)
            self.assertEqual(binding['jarvis_session_id'],
                             binding['stable_device_id'])
            self.assertEqual(binding['stable_device_id'], device)
            self.assertEqual(binding['auth_session_id'], auth)
            self.assertEqual(binding['prepared_turn_id'], str(turn_id))
            self.assertEqual(binding['adapter_boot_id'], self.host.boot_id())
            self.assertEqual(finalized.envelope.capture_id, '9:7')
            self.assertEqual(finalized.channel, channel)
            # A prepare executed from that channel's finalized turn stores the
            # identical provenance in the pending confirmation.
            self.client.results['prepare_plant_create'] = dict(PREPARE_RESULT)
            self.run_plugin({'action': 'prepare_plant_create',
                             'plant_name': 'Testpflanze'}, turn=finalized)
            pending = self.host.coordinator.current(device)
            self.assertIsNotNone(pending, channel)
            self.assertEqual(pending.stable_device_id, device)
            self.assertEqual(pending.auth_session_id, auth)
            self.assertEqual(pending.prepared_turn_id, turn_id)


# ── Criterion 21: four durable credentials stay isolated ───────────────────

class TestCriterion21FourDevices(AcceptanceFixture):
    def test_four_durable_credentials_remain_isolated(self):
        registry = SessionRegistry()
        for index in range(4):
            device = f'device-durable-{index + 1}'
            session = registry.issue(device)
            self.assertEqual(session.device_id, device)
            turn = finalize_turn(make_envelope(
                CHANNEL_PHONE_PCM, 60 + index, stable_device_id=device,
                auth_session_id=auth_session_id_of(session.session_key),
                capture_id=f'1:{index}'))
            self.prepare_pending(turn=turn)
            pending = self.host.coordinator.current(device)
            self.assertEqual(pending.stable_device_id, device)
            self.assertEqual(pending.auth_session_id,
                             auth_session_id_of(session.session_key))
            self.assertEqual(pending.action, 'plant_create')
        # All four pendings coexist, keyed by durable device, never merged.
        devices = [f'device-durable-{i}' for i in range(1, 5)]
        self.assertEqual(len([d for d in devices
                              if self.host.coordinator.current(d) is not None]),
                         4)

    def test_interleaved_turns_cannot_confirm_another_devices_preview(self):
        turn_a = make_turn(user_turn_id=70, stable_device_id='device-a')
        turn_b = make_turn(user_turn_id=71, stable_device_id='device-b')
        self.prepare_pending(turn=turn_a)
        # device-b has no pending of its own: its confirm attempt is refused
        # with the bounded German line and zero POSTs.
        self.client.calls.clear()
        outcome = self.host.confirm(turn_b)
        self.assertEqual(outcome['refusal'], 'wrong_session')
        self.assertEqual(outcome['classification'], 'refused')
        self.assertEqual(outcome['spoken'],
                         REFUSAL_SPOKEN['wrong_session'])
        self.assertNotIn('opaque-token-dummy', outcome['spoken'])
        self.assertNotIn('payload-hash-dummy', outcome['spoken'])
        self.assertEqual(self.client.calls, [])
        # device-a still owns its untouched pending afterwards.
        pending = self.host.coordinator.current('device-a')
        self.assertEqual(pending.state, 'pending')

    def test_bearer_refresh_retains_the_stable_device_identity(self):
        registry = SessionRegistry()
        first = registry.issue('device-durable-1')
        refreshed = registry.issue('device-durable-1')
        self.assertNotEqual(first.token, refreshed.token)
        self.assertNotEqual(first.session_key, refreshed.session_key)
        self.assertEqual(refreshed.device_id, 'device-durable-1')
        # The post-refresh envelope keeps the durable identity while the
        # pseudonymous auth-session digest rotates with the session key.
        envelope = make_envelope(
            CHANNEL_PHONE_PCM, 80,
            stable_device_id=refreshed.device_id,
            auth_session_id=auth_session_id_of(refreshed.session_key),
            capture_id=None)
        self.assertEqual(envelope.stable_device_id, 'device-durable-1')
        self.assertNotEqual(envelope.auth_session_id,
                            auth_session_id_of(first.session_key))

    def test_revocation_invalidates_pending_approval(self):
        registry = SessionRegistry()
        session = registry.issue('device-durable-1')
        turn = finalize_turn(make_envelope(
            CHANNEL_PHONE_PCM, 90,
            auth_session_id=auth_session_id_of(session.session_key)))
        self.prepare_pending(turn=turn)
        # Revocation removes the device's bearer sessions; nothing with this
        # bearer can authenticate any more.
        revoked = registry.revoke_device('device-durable-1')
        self.assertIn(session.token, revoked)
        self.assertIsNone(registry.authenticate(session.token))
        # A re-paired device gets a fresh session digest — the stored pending
        # can never be confirmed with it (bounded refusal, zero POSTs).
        repaired = registry.issue('device-durable-1')
        self.assertNotEqual(auth_session_id_of(repaired.session_key),
                            auth_session_id_of(session.session_key))
        fresh_turn = finalize_turn(make_envelope(
            CHANNEL_PHONE_PCM, 91,
            auth_session_id=auth_session_id_of(repaired.session_key),
            capture_id='1:2'))
        self.client.calls.clear()
        outcome = self.host.confirm(fresh_turn)
        self.assertEqual(outcome['refusal'], 'wrong_auth_session')
        self.assertEqual(outcome['classification'], 'refused')
        self.assertEqual(self.client.calls, [])


# ── Criterion 22: pre-finalization assent cannot confirm ───────────────────

class TestCriterion22PreFinalizationCannotConfirm(AcceptanceFixture):
    def test_same_turn_tool_call_or_affirmative_cannot_confirm_a_write(self):
        pending = self.prepare_pending()
        self.assertEqual(pending.prepared_turn_id, self.turn.user_turn_id)
        # (a) A same-turn affirm — the very turn that prepared — is refused by
        # the turn fence with zero POSTs.
        self.client.calls.clear()
        outcome = self.host.evaluate(self.turn, 'ja')
        self.assertEqual(outcome['decision'], 'affirm')
        self.assertEqual(outcome['refusal'], 'wrong_turn')
        self.assertEqual(outcome['classification'], 'refused')
        self.assertIn('nicht nach der Vorschau', outcome['spoken'])
        self.assertEqual(self.client.calls, [])
        # (b) An older turn id fails through the same fence.
        executing, refusal = self.host.coordinator.consume(
            'device-durable-1', make_turn(user_turn_id=39),
            {'action': pending.action, 'club_id': pending.club_id,
             'payload_hash': pending.payload_hash,
             'adapter_boot_id': pending.adapter_boot_id})
        self.assertIsNone(executing)
        self.assertEqual(refusal, 'wrong_turn')
        # (c) A mid-stream envelope (tool call arriving before final
        # transcription) can never finalize a user turn at all.
        with self.assertRaises(ValueError):
            finalize_turn(make_envelope(CHANNEL_PHONE_PCM, 41, final=False))

    def test_the_next_finalized_turn_really_confirms(self):
        self.prepare_pending()
        self.client.calls.clear()
        later = make_turn(user_turn_id=41)
        self.client.results['confirm_plant_create'] = {
            'ok': True, 'result_id': 'plant-42'}
        outcome = self.host.evaluate(later, 'ja')
        self.assertEqual(outcome['classification'], 'succeeded')
        self.assertEqual(outcome['result_id'], 'plant-42')
        self.assertEqual([c['tool'] for c in self.client.calls],
                         ['confirm_plant_create'])


# ── Criterion 27: restart fences in-memory pending state ───────────────────

class TestCriterion27RestartFencesPending(AcceptanceFixture):
    def test_jarvis_restart_loses_the_pending_and_requires_a_fresh_preview(self):
        self.prepare_pending()
        # A JARVIS restart owns a fresh in-memory coordinator: the pending
        # preview no longer exists and any confirm attempt is refused before
        # the adapter.
        self.client.calls.clear()
        restarted_host = self.make_host(coordinator=ConfirmationCoordinator())
        outcome = restarted_host.confirm(make_turn(user_turn_id=41))
        self.assertEqual(outcome['refusal'], 'wrong_session')
        self.assertEqual(outcome['classification'], 'refused')
        self.assertEqual(self.client.calls, [])
        # A fresh preview is required: the restarted host stores a new pending
        # and a later finalized turn then confirms exactly once.
        next_turn = make_turn(user_turn_id=42)
        self.client.results['prepare_plant_create'] = dict(PREPARE_RESULT)
        result = self.run_plugin(
            {'action': 'prepare_plant_create', 'plant_name': 'X'},
            host=restarted_host, turn=next_turn)
        self.assertTrue(result.get('ok'), result)
        later = make_turn(user_turn_id=43)
        self.client.results['confirm_plant_create'] = {
            'ok': True, 'result_id': 'plant-7'}
        outcome = restarted_host.evaluate(later, 'ja')
        self.assertEqual(outcome['classification'], 'succeeded')
        self.assertEqual(self.client.calls[-1]['tool'], 'confirm_plant_create')
        self.assertEqual(sorted(c['tool'] for c in self.client.calls),
                         ['confirm_plant_create', 'prepare_plant_create'])

    def test_adapter_restart_invalidates_every_pending_preview(self):
        pending = self.prepare_pending()
        # The adapter child restarts: the explicit fence turns every pending
        # unconsumable and the old confirmation token loses its meaning.
        self.host.coordinator.invalidate_all_on_adapter_restart()
        cancelled = self.host.coordinator.current('device-durable-1')
        self.assertEqual(cancelled.state, 'cancelled')
        self.client.calls.clear()
        executing, refusal = self.host.coordinator.consume(
            'device-durable-1', make_turn(user_turn_id=41),
            {'action': pending.action, 'club_id': pending.club_id,
             'payload_hash': pending.payload_hash,
             'adapter_boot_id': pending.adapter_boot_id})
        self.assertIsNone(executing)
        self.assertEqual(refusal, 'cancelled')
        self.assertEqual(self.client.calls, [])
        # A fresh prepare is required and yields a consumable pending again.
        self.client.results['prepare_plant_create'] = dict(PREPARE_RESULT)
        self.run_plugin({'action': 'prepare_plant_create',
                         'plant_name': 'Testpflanze'})
        refreshed = self.host.coordinator.current('device-durable-1')
        self.assertEqual(refreshed.state, 'pending')
        self.assertIsNot(refreshed, pending)


# ── Live-dependent checks: operator-approved, skipped by default ──────────

@unittest.skipUnless(
    os.getenv(LIVE_GATE_ENV) == LIVE_GATE_VALUE,
    f'live dependent: set {LIVE_GATE_ENV}={LIVE_GATE_VALUE} on the target host '
    'only after explicit operator approval; tests never target the live API')
class LiveOperatorGate(unittest.TestCase):
    """Operator-approved boot check of the deployment's pinned adapter.

    Talks only to the locally pinned, SHA-verified adapter process and to the
    one informational tool call; the Cannanas API is never contacted.
    """

    def test_pinned_adapter_boots_with_the_release_1_tool_set(self):
        from core.cannanas_client import child_environment
        from core.cannanas_host import ADAPTER_ENV_VARS

        missing = missing_config_variables()
        self.assertEqual(
            missing, (),
            'live gate requires a fully configured Cannanas host '
            f'(missing, names only: {", ".join(missing)})')
        client = CannanasClient({
            'command': os.environ['JARVIS_CANNANAS_ADAPTER_COMMAND'],
            'args': (os.environ.get('JARVIS_CANNANAS_ADAPTER_ARGS') or '').split(),
            'adapter_version': PINNED_ADAPTER_VERSION,
            'artifact_sha256': PINNED_WHEEL_SHA256,
            'expected_tools': ADAPTER_TOOLS(),
            'adapter_env': child_environment(
                {key: os.environ.get(key, '') for key in ADAPTER_ENV_VARS}),
        })
        self.assertTrue(client.start(),
                        'pinned adapter failed to boot or verify its release')
        try:
            result = client.call('get_adapter_info', {})
            self.assertTrue(result.get('ok'))
            # Informational only; nothing secret-bearing is echoed.
            self.assertNotIn('Bearer', json.dumps(str(result.get('version') or '')))
        finally:
            client.stop()


if __name__ == '__main__':
    unittest.main()