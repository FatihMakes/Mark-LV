"""Exercise JarvisLive's real playback loop through its hardware boundary."""
import asyncio
import tempfile
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_ducking import Mixer
from core.ducking import DuckingController
from ui_headless import JarvisUI


class Stream:
    latency = .02

    def __init__(self):
        self.writes = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.closed = False
        self.opened = False
        self.on_write = None
        self.fail = False

    def start(self):
        self.opened = True

    def write(self, data):
        self.writes.append(data)
        if self.on_write:
            self.on_write()
        self.started.set()
        self.release.wait(4)
        if self.fail:
            raise RuntimeError('hardware write failed')

    def stop(self):
        pass

    def close(self):
        self.closed = True
        self.opened = False


class PlaybackDuckingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import main
        with patch('memory.config_manager.load_api_keys', return_value={'gemini_api_key':'test'}):
            self.live = main.JarvisLive(JarvisUI())
        self.live.session = NS()
        self.live.audio_in_queue = asyncio.Queue()
        self.live.out_queue = asyncio.Queue()
        self.live._turn_done_event = asyncio.Event()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mixer = Mixer()
        self.ctl = DuckingController(self.mixer, Path(self.tmp.name)/'state'/'gain.json',
                                     ramp_seconds=0, lease_seconds=1)
        self.live._ducking = self.ctl
        self.stream = Stream()
        hardware = NS(RawOutputStream=lambda **kw: self.stream,
                      query_devices=lambda: [{'name':'analog-test','max_output_channels':2}])
        self.patches = [patch.dict('sys.modules',{'sounddevice':hardware}),
                        patch.object(main, 'get_output_device', return_value='analog-test')]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.task = asyncio.create_task(self.live._play_audio())
        await asyncio.sleep(.01)

    async def asyncTearDown(self):
        self.stream.release.set()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def first_write(self, duration=.2):
        self.live._turn_active = True
        self.live.audio_in_queue.put_nowait(b'\x01\x00' * int(duration*24000))
        self.assertTrue(await asyncio.to_thread(self.stream.started.wait, 1))
        if self.task.done():
            await self.task

    async def wait_restored(self):
        deadline = time.monotonic()+2
        # A worker can finish mixer writes before the event loop observes its
        # completion. Wait for the public playback phase as well as raw gain.
        while (self.mixer.value[0] != (65536,32768) or self.live._playback_active) and time.monotonic()<deadline:
            await asyncio.sleep(.02)
        self.assertEqual(self.mixer.value[0], (65536,32768))
        self.assertFalse(self.live._playback_active)

    async def test_run_cancellation_is_not_swallowed_as_reconnect(self):
        import main
        live = self.live
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'JARVIS_PUBLIC_URL': 'https://example.test',
                'JARVIS_CONTROL_SOCKET': str(Path(directory)/'private'/'control.sock')}), patch(
                'dashboard.server.PORT', 0), patch.object(main, '_get_api_key', return_value='test'), patch.object(
                main.genai, 'Client', side_effect=asyncio.CancelledError()):
            task = asyncio.create_task(live.run())
            try:
                await asyncio.wait_for(asyncio.shield(task), 2)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                task.cancel()
            swallowed = not task.done()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertFalse(swallowed, 'run converted cancellation into a reconnect')

    async def test_stop_during_gain_acquire_discards_batch_even_if_model_completes(self):
        started, release = threading.Event(), threading.Event()
        def pause_ramp():
            started.set()
            release.wait(2)
        self.mixer.before_write = pause_ramp
        self.live._turn_active = True
        self.live.audio_in_queue.put_nowait(b'\x01\x00'*1200)
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        self.live.interrupt()
        self.live._turn_active = False
        self.live._interrupted = False
        self.live._turn_done_event.set()
        release.set()
        await asyncio.sleep(.4)
        self.assertEqual(self.stream.writes, [], 'Stop must fence an already-dequeued batch')
        await self.wait_restored()

    async def test_invalid_envelope_config_does_not_leak_open_hardware(self):
        await self.asyncTearDown()
        import os
        self.live._ducking = None
        self.stream.closed = False
        # Invalid configuration must be rejected before a stream is opened, or
        # the stream must still be closed by the error path.
        with patch.dict(os.environ, {'JARVIS_DUCKING':'1', 'JARVIS_DUCK_DB':'bad'}):
            self.task = asyncio.create_task(self.live._play_audio())
            with self.assertRaises(ValueError):
                await self.task
        self.assertFalse(self.stream.opened)

    async def test_text_tool_or_idle_turn_never_ducks(self):
        self.live._turn_done_event.set()
        await asyncio.sleep(.15)
        self.assertEqual(self.mixer.writes, [])
        self.assertFalse(self.ctl.state_path.exists())

    async def test_gain_precedes_first_output_and_release_waits_pcm_latency_tail(self):
        observed = []
        self.stream.on_write = lambda: observed.append(self.mixer.value[0])
        start = time.monotonic()
        await self.first_write()
        self.assertEqual(observed, [(32846,16423)])
        self.live._turn_active = False
        self.live._turn_done_event.set()
        await asyncio.sleep(.15)
        self.assertEqual(self.mixer.value[0], (32846,16423))
        self.assertFalse(self.live.audio_allowed())
        await self.wait_restored()
        self.assertGreaterEqual(time.monotonic()-start, .47)
        self.assertTrue(self.live.audio_allowed(), repr((self.live._playback_active,
                        self.live._is_speaking, self.live._tail_until-time.monotonic(),
                        self.ctl.state_path.exists())))

    async def test_long_gap_and_block_refresh_lease_without_unducking(self):
        self.stream.release.clear()
        await self.first_write(.05)
        guard = DuckingController(self.mixer, self.ctl.state_path, ramp_seconds=0)
        await asyncio.sleep(1.2)
        self.assertFalse(await asyncio.to_thread(guard.recover), 'blocking write lost lease')
        with patch('core.ducking.owner_alive', return_value=False):
            self.assertFalse(await asyncio.to_thread(guard.recover), 'crash recovery ignored physical tail')
        self.assertEqual(self.mixer.value[0], (32846,16423))
        self.stream.release.set()
        await asyncio.sleep(1.2)
        self.assertFalse(await asyncio.to_thread(guard.recover), 'inter-chunk gap lost lease')
        self.assertEqual(self.mixer.value[0], (32846,16423))
        self.live._turn_done_event.set()
        await self.wait_restored()

    async def test_stop_while_write_blocked_keeps_mic_and_gain_until_tail_finishes(self):
        self.stream.release.clear()
        await self.first_write(.2)
        self.live.interrupt()
        self.live._turn_active = False
        self.live._interrupted = False  # model reports turn_complete immediately
        self.live._turn_done_event.set()
        await asyncio.sleep(.3)
        self.assertFalse(self.live.audio_allowed(), 'in-flight write must keep mic closed')
        self.assertEqual(self.mixer.value[0], (32846,16423))
        self.stream.release.set()
        await asyncio.sleep(.04)
        self.assertFalse(self.live.audio_allowed(), 'tail is measured from write completion too')
        await self.wait_restored()
        self.assertTrue(self.live.audio_allowed())

    async def model_complete(self):
        # Exercise the real receiver's completion state, then send no more events.
        async def receive():
            yield NS(data=None, tool_call=None, server_content=NS(
                output_transcription=None, input_transcription=None, turn_complete=True))
            raise asyncio.CancelledError()
        self.live.session.receive = receive
        with self.assertRaises(asyncio.CancelledError):
            await self.live._receive_audio()
        self.assertFalse(self.live._turn_active)
        self.assertTrue(self.live._turn_done_event.is_set())

    async def test_stop_after_model_complete_during_blocked_write_restores_and_opens_mic(self):
        self.stream.release.clear()
        await self.first_write(.2)
        await self.model_complete()
        self.live.audio_in_queue.put_nowait(b'discard')
        self.live.interrupt()
        self.assertFalse(self.live._interrupted, 'completed model needs no suppression')
        await asyncio.sleep(.3)
        self.assertTrue(self.live._playback_active)
        self.assertFalse(self.live.audio_allowed(), 'physical write still owns the microphone gate')
        self.assertEqual(self.mixer.value, ((32846,16423), False))
        self.assertTrue(self.ctl.state_path.exists())
        self.stream.release.set()
        await asyncio.sleep(.04)
        self.assertFalse(self.live.audio_allowed(), 'Stop must retain the physical output tail')
        self.assertEqual(self.mixer.value, ((32846,16423), False))
        await self.wait_restored()
        self.assertEqual(self.mixer.value, ((65536,32768), False))
        self.assertFalse(self.ctl.state_path.exists())
        self.assertTrue(self.live.audio_allowed())
        self.assertEqual(len(self.stream.writes), 1)
        self.assertFalse(self.task.done())

    async def test_stop_after_model_complete_during_tail_restores_and_opens_mic(self):
        await self.first_write(.2)
        await self.model_complete()
        async with asyncio.timeout(1):
            while self.live._is_speaking:
                await asyncio.sleep(.005)
        self.assertTrue(self.live._tail_active())
        self.assertTrue(self.live._playback_active)
        self.live.interrupt()
        self.assertFalse(self.live.audio_allowed())
        self.assertEqual(self.mixer.value, ((32846,16423), False))
        self.assertTrue(self.ctl.state_path.exists())
        await self.wait_restored()
        self.assertEqual(self.mixer.value, ((65536,32768), False))
        self.assertFalse(self.ctl.state_path.exists())
        self.assertTrue(self.live.audio_allowed())
        self.assertFalse(self.task.done())

    async def test_cancellation_waits_for_inflight_write_and_restores_before_close(self):
        self.stream.release.clear()
        await self.first_write(.05)
        self.task.cancel()
        await asyncio.sleep(.05)
        self.assertFalse(self.task.done())
        self.assertEqual(self.mixer.value[0], (32846,16423))
        self.assertFalse(self.live.audio_allowed())
        self.stream.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertTrue(self.stream.closed)
        self.assertEqual(self.mixer.value[0], (65536,32768))
        self.assertFalse(self.ctl.state_path.exists())

    async def test_hardware_exception_releases_and_propagates(self):
        self.stream.fail = True
        await self.first_write(.05)
        with self.assertRaisesRegex(RuntimeError, 'hardware write failed'):
            await self.task
        self.assertEqual(self.mixer.value[0], (65536,32768))
        self.assertTrue(self.stream.closed)

    async def assert_remote_lease_error(self):
        from fastapi.testclient import TestClient
        server = self.live._dashboard
        assert server is not None
        deadline = time.monotonic() + 1
        def errors():
            return [m for m in server._history
                    if m.get('text', '').startswith('ERR: Spotify ducking degraded')
                    and 'lease' in m['text'].lower()]
        while not errors() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(errors(), 'lost envelope must appear in remote ERR history')
        # Read history through the actual authenticated remote route, not just
        # an in-memory logging spy. Credentials exist only inside this test app.
        with TestClient(server.app) as client:
            token = client.post('/login', json={'pin': self.remote_key}).json()['token']
            with client.websocket_connect(f'/ws?token={token}') as ws:
                self.assertEqual(ws.receive_json()['type'], 'status')
                history = [ws.receive_json() for _ in server._history[-50:]]
        self.assertTrue(any(m.get('text', '').startswith('ERR: Spotify ducking degraded')
                            and 'lease' in m['text'].lower() for m in history))

    async def test_recovered_lease_is_reported_by_background_heartbeat_and_speech_continues(self):
        from dashboard.server import DashboardServer
        self.live._dashboard = DashboardServer()
        self.remote_key = self.live._dashboard.new_key()
        now = [100.0]
        self.ctl.clock = lambda: now[0]
        await self.first_write(.05)
        now[0] += 20
        guard = DuckingController(self.mixer, self.ctl.state_path,
                                 clock=lambda: now[0], ramp_seconds=0)
        self.assertTrue(await asyncio.to_thread(guard.recover))
        self.assertEqual(self.mixer.value, ((65536,32768), False))
        await self.assert_remote_lease_error()
        self.stream.started.clear()
        self.live.audio_in_queue.put_nowait(b'\x02\x00'*1200)
        self.assertTrue(await asyncio.to_thread(self.stream.started.wait, 1))
        self.assertEqual(len(self.stream.writes), 2, 'lease loss must not disable speech')
        self.assertEqual(self.mixer.value, ((65536,32768), False))
        self.assertFalse(self.ctl.state_path.exists(), 'resumption must not reacquire behind the guard')
        await self.model_complete()
        await self.wait_restored()
        self.assertTrue(self.live.audio_allowed())
        self.assertFalse(self.task.done())

    async def test_prewrite_lost_lease_reports_remote_error_without_stealing_new_generation(self):
        from dashboard.server import DashboardServer
        self.live._dashboard = DashboardServer()
        self.remote_key = self.live._dashboard.new_key()
        now = [100.0]
        self.ctl.clock = lambda: now[0]
        await self.first_write(.05)
        # Isolate immediate pre-write reporting: no periodic heartbeat can rescue
        # the assertion. Production controller, journal and playback stay real.
        heartbeat_tasks = [t for t in asyncio.all_tasks()
                           if t.get_coro().__qualname__ == 'JarvisLive._play_audio.<locals>.keep_lease']
        self.assertEqual(len(heartbeat_tasks), 1)
        heartbeat_tasks[0].cancel()
        await asyncio.gather(*heartbeat_tasks, return_exceptions=True)
        now[0] += 20
        guard = DuckingController(self.mixer, self.ctl.state_path,
                                 clock=lambda: now[0], ramp_seconds=0)
        self.assertTrue(await asyncio.to_thread(guard.recover))
        new = DuckingController(self.mixer, self.ctl.state_path,
                               clock=lambda: now[0], ramp_seconds=0)
        self.addCleanup(new.end)
        self.assertTrue(await asyncio.to_thread(new.begin))
        journal = self.ctl.state_path.read_bytes()
        self.stream.started.clear()
        self.live.audio_in_queue.put_nowait(b'\x02\x00'*1200)
        self.assertTrue(await asyncio.to_thread(self.stream.started.wait, 1))
        self.assertEqual(len(self.stream.writes), 2)
        await self.assert_remote_lease_error()
        self.assertEqual(self.ctl.state_path.read_bytes(), journal)
        self.assertEqual(self.mixer.value, ((32846,16423), False))
        await self.model_complete()
        async with asyncio.timeout(2):
            while self.live._playback_active:
                await asyncio.sleep(.01)
        self.assertTrue(self.live.audio_allowed())
        self.assertEqual(self.ctl.state_path.read_bytes(), journal)
        self.assertEqual(self.mixer.value, ((32846,16423), False), 'old phase must not restore new owner')
        self.assertTrue(await asyncio.to_thread(new.end))
        self.assertEqual(self.mixer.value, ((65536,32768), False))
        self.assertFalse(self.task.done())

    async def test_failed_ducking_does_not_prevent_speech_and_reports_degraded(self):
        from dashboard.server import DashboardServer
        self.live._dashboard = DashboardServer()
        self.mixer.available = False
        await self.first_write(.05)
        self.assertEqual(len(self.stream.writes),1)
        self.assertTrue(self.ctl.error)
        await asyncio.sleep(0)
        self.assertTrue(any('Spotify ducking degraded' in m.get('text','')
                            for m in self.live._dashboard._history),
                        'phone-only operator cannot see a console-only failure')
        self.live._turn_done_event.set()
        await asyncio.sleep(.4)
        self.assertFalse(self.task.done())


class ServiceSignalTests(unittest.TestCase):
    def test_sigterm_cancels_run_and_finishes_cleanup_in_real_child(self):
        import main
        import os
        import signal
        import subprocess
        import sys
        import select
        self.assertTrue(callable(getattr(main, '_run_service', None)), 'graceful SIGTERM wrapper missing')
        code = '''
import asyncio, main
class Service:
    async def run(self):
        print('ready',flush=True)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(.05)
            print('cleaned',flush=True)
asyncio.run(main._run_service(Service()))
'''
        proc = subprocess.Popen([sys.executable,'-c',code], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=os.environ.copy())
        try:
            ready, _, _ = select.select([proc.stdout], [], [], 4)
            self.assertTrue(ready)
            self.assertEqual(proc.stdout.readline().strip(),'ready')
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=4)
            self.assertEqual(proc.returncode,0,err)
            self.assertIn('cleaned',out)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
