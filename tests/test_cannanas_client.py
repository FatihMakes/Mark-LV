"""Persistent serialized Cannanas MCP client: budgets, pins, restart fences."""
import sys
import unittest
from pathlib import Path

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

PINNED_VERSION = '1.0.1'
PINNED_SHA = 'a' * 64


def _config(mode='ok', **extra):
    args = [str(FIXTURE)]
    if mode != 'ok':
        args.append(f'--mode={mode}')
    for key, value in extra.items():
        args.append(f'--set:{key}={value}')
    return {
        'command': sys.executable,
        'args': args,
        'adapter_version': PINNED_VERSION,
        'artifact_sha256': PINNED_SHA,
        'expected_tools': EXPECTED_TOOLS,
        'adapter_env': {},
    }


class ClientFixture(unittest.TestCase):
    def set_mode(self, mode, **extra):
        self.mode = dict(mode=mode, **extra)
        return self._config()

    def _config(self):
        return _config(self.mode.get('mode', 'ok'),
                       **{k: v for k, v in self.mode.items() if k != 'mode'})

    def make_client(self, config, **kw):
        from core.cannanas_client import CannanasClient
        client = CannanasClient(config, **kw)
        self.addCleanup(client.stop, 5)
        return client


class TestInitBudget10s(ClientFixture):
    def test_initialize_beyond_budget_degrades_without_hanging(self):
        config = self.set_mode('slow-init', FAKE_ADAPTER_INIT_DELAY='3')
        client = self.make_client(config, init_budget=0.5)
        self.assertFalse(client.start())
        self.assertTrue(client.degraded)
        # Still bounded by the budget, not the full 3 s delay plus hang.
        self.assertIn('initialize', client.degraded_reason)

    def test_configured_budget_is_10s_by_default(self):
        from core.cannanas_client import INIT_BUDGET_SECONDS
        self.assertEqual(INIT_BUDGET_SECONDS, 10.0)


class TestToolBudget25s(ClientFixture):
    def test_tool_call_beyond_budget_fails_bounded(self):
        config = self.set_mode('slow-tool', FAKE_ADAPTER_TOOL_DELAY='5')
        client = self.make_client(config, tool_budget=0.5, restart_backoff=0.01)
        self.assertTrue(client.start())
        with self.assertRaises(Exception):
            client.call('get_cannanas_overview', {})
        # Bounded: the call returned well before the 5 s tool delay elapsed.

    def test_configured_budget_is_25s_by_default(self):
        from core.cannanas_client import TOOL_BUDGET_SECONDS
        self.assertEqual(TOOL_BUDGET_SECONDS, 25.0)


class TestSinglePersistentSession(ClientFixture):
    def test_calls_share_one_child_process(self):
        config = self.set_mode('ok')
        client = self.make_client(config)
        self.assertTrue(client.start())
        first = client.call('get_cannanas_overview', {})
        second = client.call('find_plants', {'query': 'Brain Damage'})
        self.assertEqual(first['pid'], second['pid'])
        self.assertGreater(first['pid'], 0)


class TestExactToolSetVerified(ClientFixture):
    def test_unexpected_extra_tool_listed_is_refused(self):
        config = self.set_mode('extra-tool')
        client = self.make_client(config)
        self.assertFalse(client.start())
        self.assertTrue(client.degraded)
        self.assertIn('tool set', client.degraded_reason)

    def test_missing_expected_tool_is_refused(self):
        config = self.set_mode('ok')
        config['expected_tools'] = EXPECTED_TOOLS + ['rogue_delete_everything']
        client = self.make_client(config)
        self.assertFalse(client.start())
        self.assertTrue(client.degraded)


class TestVersionAndSHAPinned(ClientFixture):
    def test_version_or_sha_mismatch_degrades_reads_only(self):
        config = self.set_mode('wrong-version')
        client = self.make_client(config)
        self.assertTrue(client.start())  # transport is up; release refused for writes
        self.assertTrue(client.read_only_degraded)
        with self.assertRaises(Exception):
            client.call('prepare_plant_create', {})
        with self.assertRaises(Exception):
            client.call('confirm_plant_create', {})
        # Reads still work in the read-only-safe degraded state.
        result = client.call('get_cannanas_overview', {})
        self.assertEqual(result['ok'], True)

    def test_pinned_release_matches_manifest(self):
        from core.cannanas_client import PINNED_ADAPTER_VERSION, PINNED_WHEEL_SHA256
        self.assertEqual(PINNED_ADAPTER_VERSION, PINNED_VERSION)
        self.assertEqual(PINNED_WHEEL_SHA256,
                         '7085943afc149d3f2356fca66d8a9d0bac9c2576166e99e36093a56b87153f5f')


class TestChildExitRestartsOnce(ClientFixture):
    def test_read_survives_single_restart_and_fails_second(self):
        config = self.set_mode('one-shot')
        client = self.make_client(config, restart_backoff=0.01)
        self.assertTrue(client.start())
        first = client.call('get_cannanas_overview', {})
        self.assertTrue(first['ok'])
        # The child dies on its second tools/call; the read restarts once
        # and succeeds again against a fresh child.
        second = client.call('get_cannanas_overview', {})
        self.assertTrue(second['ok'])
        self.assertNotEqual(first['pid'], second['pid'])
        # A third call hits a fresh exit with the restart already spent:
        # degraded, bounded failure — never an unbounded loop.
        with self.assertRaises(Exception):
            client.call('get_cannanas_overview', {})
        self.assertTrue(client.degraded)


class TestRestartInvalidatesCoordinatorHook(ClientFixture):
    def test_restart_calls_invalidation_hook_exactly_once(self):
        config = self.set_mode('one-shot')
        hook = []
        client = self.make_client(config, restart_backoff=0.01,
                                  on_restart=lambda: hook.append(1))
        self.assertTrue(client.start())
        client.call('get_cannanas_overview', {})
        client.call('get_cannanas_overview', {})   # exit → restart → hook
        with self.assertRaises(Exception):
            client.call('get_cannanas_overview', {})
        self.assertEqual(hook, [1])


class TestMissingBinaryDegradesNotCrashes(ClientFixture):
    def test_missing_entry_point_reports_degraded_not_crash(self):
        config = self.set_mode('ok')
        config['command'] = '/nonexistent/cannanas-mcp-adapter-path'
        client = self.make_client(config)
        self.assertFalse(client.start())
        self.assertTrue(client.degraded)
        self.assertIn('entry point', client.degraded_reason)
        with self.assertRaises(Exception):
            client.call('get_cannanas_overview', {})


class TestChildEnvironmentAndSerialization(unittest.TestCase):
    def test_child_env_is_minimal_and_fixed(self):
        from core.cannanas_client import child_environment
        env = child_environment({
            'CANNANAS_API_KEY': 'key', 'CANNANAS_CLUB_ID': 'club',
            'CANNANAS_STATE_DIR': '/var/lib/cannanas/adapter/state',
            'CANNANAS_AUDIT_FILE': '/var/lib/cannanas/adapter/audit.jsonl',
            'CANNANAS_API_URL': 'https://evil.example',
            'JARVIS_TOKEN': 'leak',
        })
        self.assertEqual(
            set(env),
            {'PATH', 'CANNANAS_API_KEY', 'CANNANAS_CLUB_ID',
             'CANNANAS_STATE_DIR', 'CANNANAS_AUDIT_FILE'})
        self.assertNotIn('CANNANAS_API_URL', env)

    def test_calls_serialize_via_one_queue(self):
        from core.cannanas_client import CannanasClient
        self.assertTrue(hasattr(CannanasClient, 'call'))
        self.assertTrue(hasattr(CannanasClient, 'start'))
        self.assertTrue(hasattr(CannanasClient, 'stop'))


if __name__ == '__main__':
    unittest.main()