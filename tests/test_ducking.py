"""Behavioral mixer/recovery tests; the only fake is the external audio server."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

try:
    from core.ducking import DuckingController, Gain, PulseSinkGain
except ImportError:
    DuckingController = Gain = PulseSinkGain = None


class Mixer:
    def __init__(self, volumes=(65536, 32768), mute=False):
        self.value = (volumes, mute)
        self.app_volume = 42
        self.writes = []
        self.available = True
        self.fail_write = False
        self.before_write = None

    def read(self):
        if not self.available:
            raise RuntimeError('sink absent')
        return Gain(*self.value)

    def write(self, gain):
        if self.before_write:
            self.before_write()
        if self.fail_write:
            raise RuntimeError('audio server unavailable')
        self.value = (gain.volumes, gain.mute)
        self.writes.append(self.value)


class FileMixer:
    """Cross-process audio boundary used by killed-owner/guard CLI tests."""
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        value = json.loads(self.path.read_text())
        return Gain(tuple(value['volumes']), value['mute'])

    def write(self, gain):
        path = self.path.with_suffix('.new')
        path.write_text(json.dumps(dict(volumes=gain.volumes, mute=gain.mute)))
        os.replace(path, self.path)


class DuckingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DuckingController, 'Ducking implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'state.json'
        self.now = 100.0
        self.mixer = Mixer()

    def controller(self, **kw):
        return DuckingController(self.mixer, self.path, clock=lambda: self.now,
                                 ramp_seconds=0, **kw)

    def test_channel_baseline_is_journaled_before_attenuation_and_exactly_restored(self):
        ctl = self.controller()
        def check_journal():
            state = json.loads(self.path.read_text())
            self.assertEqual(state['baseline']['volumes'], [65536, 32768])
        self.mixer.before_write = check_journal
        self.assertTrue(ctl.begin())
        self.assertEqual(self.mixer.value, ((32846, 16423), False))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        ctl.end()
        self.assertEqual(self.mixer.value, ((65536, 32768), False))
        self.assertFalse(self.path.exists())

    def test_retry_failed_begin_never_claims_success_without_attenuation(self):
        ctl = self.controller()
        self.mixer.fail_write = True
        self.assertFalse(ctl.begin())
        self.assertFalse(ctl.begin(), 'owned journal is not proof the mixer write succeeded')
        self.mixer.fail_write = False
        self.assertTrue(ctl.begin())
        self.assertEqual(self.mixer.value[0], (32846,16423))
        ctl.end()
        self.assertEqual(self.mixer.value[0], (65536,32768))

    def test_repeated_and_overlapping_begin_never_rebaseline(self):
        a, b = self.controller(), self.controller()
        self.assertTrue(a.begin())
        self.assertTrue(a.begin())
        self.assertFalse(b.begin())
        b.end()
        self.assertEqual(self.mixer.value[0], (32846, 16423))
        a.end()
        a.end()
        self.assertEqual(self.mixer.value[0], (65536, 32768))

    def test_silent_and_muted_baselines_survive(self):
        for volumes, mute in [((0, 12000), False), ((0, 0), True), ((65536, 9000), True)]:
            with self.subTest(volumes=volumes, mute=mute):
                self.mixer.value = (volumes, mute)
                ctl = self.controller()
                ctl.begin()
                self.assertEqual(self.mixer.value[1], mute)
                ctl.end()
                self.assertEqual(self.mixer.value, (volumes, mute))

    def test_app_volume_and_new_stream_are_independent(self):
        ctl = self.controller()
        ctl.begin()
        self.mixer.app_volume = 17  # app changes or recreates input, not sink envelope
        ctl.heartbeat()
        ctl.end()
        self.assertEqual(self.mixer.app_volume, 17)
        self.assertEqual(self.mixer.value[0], (65536, 32768))

    def test_absent_sink_and_audio_failure_degrade_without_restart(self):
        ctl = self.controller()
        self.mixer.available = False
        self.assertFalse(ctl.begin())
        self.assertTrue(ctl.error)
        self.assertFalse(self.path.exists())
        self.mixer.available = True
        self.mixer.fail_write = True
        self.assertFalse(ctl.begin())
        self.assertTrue(self.path.exists(), 'baseline must remain recoverable after partial failure')
        self.mixer.fail_write = False
        self.now += 20
        self.assertTrue(self.controller().recover())
        self.assertEqual(self.mixer.value[0], (65536, 32768))

    def test_active_lease_not_restored_but_stale_heartbeat_is(self):
        ctl, guard = self.controller(), self.controller()
        ctl.begin()
        self.now += 5
        ctl.heartbeat()
        self.now += 5
        self.assertFalse(guard.recover())
        self.assertEqual(self.mixer.value[0], (32846, 16423))
        self.now += 10
        self.assertTrue(guard.recover())
        self.assertEqual(self.mixer.value[0], (65536, 32768))

    def test_dead_owner_waits_for_buffered_audio_tail(self):
        ctl, guard = self.controller(), self.controller()
        ctl.begin()
        ctl.heartbeat(hold_for=3)
        with patch('core.ducking.owner_alive', return_value=False):
            self.assertFalse(guard.recover())
            self.now += 4
            self.assertTrue(guard.recover())

    def test_normal_end_wins_waiting_heartbeat_without_false_lease_loss(self):
        ctl = self.controller()
        self.assertTrue(ctl.begin())
        restore_started = threading.Event()
        finish_restore = threading.Event()
        heartbeat_waiting = threading.Event()
        original_lock = ctl._lock

        def block_restore():
            restore_started.set()
            self.assertTrue(finish_restore.wait(2), 'test did not release blocked restore')

        @contextmanager
        def observed_lock():
            if threading.current_thread().name == 'waiting-heartbeat':
                heartbeat_waiting.set()
            with original_lock():
                yield

        self.mixer.before_write = block_restore
        ctl._lock = observed_lock
        ended, heartbeated = [], []
        end_thread = threading.Thread(target=lambda: ended.append(ctl.end()), name='normal-end')
        heartbeat_thread = threading.Thread(
            target=lambda: heartbeated.append(ctl.heartbeat()), name='waiting-heartbeat')
        end_thread.start()
        self.assertTrue(restore_started.wait(2), 'end did not enter restore while holding the lock')
        ctl.error = 'stale warning'
        heartbeat_thread.start()
        self.assertTrue(heartbeat_waiting.wait(2), 'heartbeat did not observe the old token and wait')
        finish_restore.set()
        end_thread.join(2)
        heartbeat_thread.join(2)

        self.assertFalse(end_thread.is_alive())
        self.assertFalse(heartbeat_thread.is_alive())
        self.assertEqual(ended, [True])
        self.assertEqual(heartbeated, [True])
        self.assertIsNone(ctl.error)
        self.assertIsNone(ctl.token)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.mixer.value, ((65536, 32768), False))

    def test_resumed_owner_reports_lease_loss_after_guard_recovery(self):
        old, guard = self.controller(), self.controller()
        self.assertTrue(old.begin())
        self.now += 20
        self.assertTrue(guard.recover())
        writes = list(self.mixer.writes)
        self.assertFalse(old.heartbeat())
        self.assertIn('lease', (old.error or '').lower())
        self.assertFalse(self.path.exists(), 'lost owner must not silently reacquire')
        self.assertEqual(self.mixer.writes, writes)
        old.end()
        self.assertEqual(self.mixer.value, ((65536, 32768), False))

    def test_old_owner_cannot_restore_or_heartbeat_restarted_generation(self):
        old, guard, new = self.controller(), self.controller(), self.controller()
        self.assertTrue(old.begin())
        self.now += 20
        self.assertTrue(guard.recover())
        self.assertTrue(new.begin())
        journal = self.path.read_bytes()
        self.assertFalse(old.heartbeat())
        self.assertIn('lease', (old.error or '').lower())
        old.end()
        self.assertEqual(self.path.read_bytes(), journal, 'old generation changed the valid new lease')
        self.assertEqual(self.mixer.value[0], (32846, 16423))
        new.end()
        self.assertEqual(self.mixer.value[0], (65536, 32768))

    def test_malformed_journal_is_not_overwritten_or_used_as_gain(self):
        self.path.parent.mkdir(mode=0o700)
        for content in ('{', '{}', '{"baseline":{"volumes":[-1],"mute":false}}'):
            self.path.write_text(content)
            ctl = self.controller()
            self.assertFalse(ctl.recover())
            self.assertFalse(ctl.begin())
            self.assertTrue(ctl.error)
            self.assertEqual(self.mixer.writes, [])

    def test_unavailable_server_during_restore_keeps_journal_for_retry(self):
        ctl = self.controller()
        ctl.begin()
        self.mixer.fail_write = True
        ctl.end()
        self.assertTrue(self.path.exists())
        self.mixer.fail_write = False
        self.assertTrue(self.controller().recover(force=True))
        self.assertFalse(self.path.exists())

    def test_partial_ramp_crash_has_original_baseline(self):
        ctl = self.controller()
        ctl.ramp_seconds = .02
        writes = []
        def die():
            writes.append(1)
            if len(writes) == 2:
                raise RuntimeError('partial ramp')
        self.mixer.before_write = die
        self.assertFalse(ctl.begin())
        self.mixer.before_write = None
        self.now += 20
        self.assertTrue(self.controller().recover())
        self.assertEqual(self.mixer.value[0], (65536, 32768))


class PulseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(PulseSinkGain, 'Pulse adapter missing')

    def test_json_channels_and_only_named_sink_commands_with_timeout(self):
        calls = []
        sinks = [{'name':'analog', 'mute':False, 'volume':{'mono':{'value':123}}},
                 {'name':'lacasa_spotify','mute':True, 'volume':{
                     'front-left':{'value':65536},'front-right':{'value':12000}}}]
        def run(args, **kw):
            calls.append((args, kw))
            return subprocess.CompletedProcess(args, 0, json.dumps(sinks), '')
        with patch('core.ducking.subprocess.run', side_effect=run):
            adapter = PulseSinkGain('lacasa_spotify')
            self.assertEqual(adapter.read(), Gain((65536, 12000), True))
            adapter.write(Gain((0, 32846), True))
        self.assertEqual(calls[0][0], ['pactl','--format=json','list','sinks'])
        self.assertEqual(calls[1][0], ['pactl','set-sink-volume','lacasa_spotify','0','32846'])
        self.assertEqual(calls[2][0], ['pactl','set-sink-mute','lacasa_spotify','1'])
        self.assertTrue(all(0 < kw['timeout'] <= 2 for _,kw in calls))
        self.assertFalse(any(any(x in cmd for x in ('pause','seek','restart','sink-input')) for cmd,_ in calls))

    def test_missing_sink_bad_json_and_command_failure_are_explicit(self):
        for result in ('[]', 'invalid'):
            with patch('core.ducking.subprocess.run', return_value=subprocess.CompletedProcess([],0,result,'')):
                with self.assertRaises((RuntimeError, ValueError)):
                    PulseSinkGain('lacasa_spotify').read()
        with patch('core.ducking.subprocess.run', side_effect=subprocess.TimeoutExpired('pactl',1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                PulseSinkGain('lacasa_spotify').read()


class GuardProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'runtime' / 'gain.json'
        self.audio = self.root / 'audio.json'
        self.mixer = FileMixer(self.audio)
        self.mixer.write(Gain((65536, 32768), False))
        # Real subprocess/argv/CLI boundary, simulated external Pulse server.
        pactl = self.root / 'pactl'
        pactl.write_text('''#!PYTHON
import json, os, pathlib, sys
p = pathlib.Path(os.environ['FAKE_PULSE_FILE'])
v = json.loads(p.read_text())
a = sys.argv[1:]
if a == ['--format=json','list','sinks']:
    print(json.dumps([{'name':'lacasa_spotify','mute':v['mute'],
                      'volume':{str(i):{'value':x} for i,x in enumerate(v['volumes'])}}]))
elif a[:2] == ['set-sink-volume','lacasa_spotify']:
    v['volumes'] = [int(x) for x in a[2:]]
    p.write_text(json.dumps(v))
elif a[:2] == ['set-sink-mute','lacasa_spotify']:
    v['mute'] = a[2] == '1'
    p.write_text(json.dumps(v))
else:
    sys.exit('forbidden Pulse operation: '+repr(a))
'''.replace('PYTHON', sys.executable, 1))
        pactl.chmod(0o700)
        self.env = {**os.environ, 'PATH':str(self.root)+os.pathsep+os.environ['PATH'],
                    'FAKE_PULSE_FILE':str(self.audio),
                    'PYTHONPATH':str(Path(__file__).parent)+os.pathsep+str(Path.cwd())}

    def owner(self):
        code = '''
import sys,time
from core.ducking import DuckingController
from test_ducking import FileMixer
c=DuckingController(FileMixer(sys.argv[1]),sys.argv[2],ramp_seconds=0)
assert c.begin(), c.error
print('owned',flush=True)
while True:
    c.heartbeat()
    time.sleep(.1)
'''
        proc = subprocess.Popen([sys.executable,'-c',code,str(self.audio),str(self.state)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=3)
        self.addCleanup(cleanup)
        import select
        ready, _, _ = select.select([proc.stdout], [], [], 3)
        self.assertTrue(ready, 'owner failed to become ready')
        self.assertEqual(proc.stdout.readline().strip(), 'owned')
        self.assertEqual(self.mixer.read().volumes, (32846,16423))
        return proc

    def cli(self, command='guard', *args):
        return subprocess.run([sys.executable,'-m','core.ducking',command,'--state',str(self.state),*args],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def test_sigkill_owner_recovered_by_real_guard_cli(self):
        owner = self.owner()
        result = self.cli('guard','--once')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.mixer.read().volumes,(32846,16423))
        owner.kill()
        owner.wait(timeout=3)
        result = self.cli('guard','--once')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('restored',result.stdout)
        self.assertEqual(self.mixer.read().volumes,(65536,32768))
        self.assertFalse(self.state.exists())
        self.assertEqual(self.cli('restore').returncode,0)

    def test_guard_and_restart_share_lock_no_new_generation_rollback(self):
        owner = self.owner()
        owner.kill()
        owner.wait(timeout=3)
        guard = subprocess.Popen([sys.executable,'-m','core.ducking','guard','--state',str(self.state),'--once'],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        new = self.owner()
        out, err = guard.communicate(timeout=5)
        self.assertEqual(guard.returncode,0,err)
        self.assertEqual(self.mixer.read().volumes,(32846,16423))
        self.assertEqual(json.loads(self.state.read_text())['pid'],new.pid)
        new.kill()
        new.wait(timeout=3)
        self.assertEqual(self.cli('guard','--once').returncode,0)
        self.assertEqual(self.mixer.read().volumes,(65536,32768))

    def test_cli_reports_malformed_state_and_unavailable_pactl_nonzero(self):
        self.state.parent.mkdir(mode=0o700)
        self.state.write_text('{}')
        result = self.cli('guard','--once')
        self.assertEqual(result.returncode,1)
        self.assertIn('degraded', result.stderr)
        self.state.unlink()
        owner = self.owner()
        owner.kill()
        owner.wait(timeout=3)
        # Do not fall through to a host's real pactl when running on the audio
        # server: the test must never modify the live Spotify envelope.
        (self.root/'pactl').write_text('#!'+sys.executable+'\nraise SystemExit(1)\n')
        result = self.cli('restore')
        self.assertEqual(result.returncode,1)
        self.assertTrue(self.state.exists())


if __name__ == '__main__':
    unittest.main()
