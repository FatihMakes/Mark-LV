"""Final review regressions: real local servers and route -> relay -> sender.

Only model I/O is replaced; no credentials, audio hardware or services are used.
"""
import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from fastapi.testclient import TestClient
from dashboard.server import DashboardServer
from ui_headless import JarvisUI


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


class LocalServiceFixture:
    def setUp(self):
        import main
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.control = self.root / 'private' / 'control.sock'
        cfg = self.root / 'synthetic.json'
        cfg.write_text(json.dumps({'gemini_api_key': 'synthetic-no-network'}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.patches = [
            patch.dict(os.environ, {'JARVIS_HEADLESS': 'true', 'JARVIS_CAPTURE_SOURCE': 'phone',
                'JARVIS_BIND': '127.0.0.1', 'JARVIS_PUBLIC_URL': 'https://example.test',
                'JARVIS_CONTROL_SOCKET': str(self.control)}),
            patch('dashboard.server.PORT', self.port),
            patch.object(main, 'API_CONFIG_PATH', cfg),
            patch('memory.config_manager.CONFIG_FILE', cfg),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.live = main.JarvisLive(JarvisUI())


class EssentialServerTests(LocalServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.before = asyncio.all_tasks()
        self.connecting = asyncio.Event()
        self.disconnected = asyncio.Event()
        fixture = self
        class PendingModel:
            async def __aenter__(self):
                fixture.connecting.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    fixture.disconnected.set()
            async def __aexit__(self, *args):
                pass
        self.model = patch.object(self.main.genai, 'Client', return_value=NS(
            aio=NS(live=NS(connect=lambda **kw: PendingModel()))))
        self.model.start()
        self.addCleanup(self.model.stop)

    async def asyncTearDown(self):
        # Also reaps the detached tasks on the RED baseline.
        tasks = asyncio.all_tasks() - self.before - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def assert_startup_fails(self, text):
        try:
            await asyncio.wait_for(self.live.run(), .8)
        except TimeoutError:
            self.fail('essential entry point failed but headless service stayed alive')
        except Exception as exc:
            self.assertIn(text, self.main.headless.error_text(exc))
        else:
            self.fail('headless service unexpectedly returned success')
        self.assertFalse(self.connecting.is_set(), 'model started before essential readiness')
        self.assertFalse(self.control.exists())
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', self.port)), 0)

    async def test_control_directory_permission_failure_is_owned(self):
        self.control.parent.mkdir(mode=0o755)
        self.control.parent.chmod(0o755)
        await self.assert_startup_fails('mode 0700')

    async def test_control_socket_conflict_fails_without_unlinking_other_owner(self):
        self.control.parent.mkdir(mode=0o700)
        with socket.socket(socket.AF_UNIX) as owner:
            owner.bind(str(self.control))
            owner.listen()
            try:
                await asyncio.wait_for(self.live.run(), .8)
            except TimeoutError:
                self.fail('active control conflict did not fail the headless service')
            except Exception as exc:
                self.assertIn('already active', self.main.headless.error_text(exc))
            self.assertTrue(self.control.exists())
            self.assertFalse(self.connecting.is_set())

    async def test_http_bind_failure_is_retryable_and_cleans_control(self):
        with socket.socket() as owner:
            owner.bind(('127.0.0.1', self.port))
            owner.listen()
            try:
                await asyncio.wait_for(self.live.run(), .8)
            except TimeoutError:
                self.fail('HTTP bind failure did not fail the headless service')
            except SystemExit as exc:
                self.assertNotEqual(exc.code, 78)
            except Exception as exc:
                self.assertIn('address already in use', self.main.headless.error_text(exc).lower())
            self.assertFalse(self.connecting.is_set())
            self.assertFalse(self.control.exists())

    async def start_ready(self):
        task = asyncio.create_task(self.live.run())
        self.addAsyncCleanup(self.stop_task, task)
        await asyncio.wait_for(self.connecting.wait(), 2)
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(b'GET /login HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        await writer.drain()
        self.assertIn(b'200 OK', await reader.read())
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_unix_connection(str(self.control))
        writer.write(b'pair\n')
        await writer.drain()
        self.assertIn('https://example.test/auto-login?key=', (await reader.readline()).decode())
        writer.close()
        await writer.wait_closed()
        return task

    async def stop_task(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_model_starts_only_after_real_http_and_control_readiness_then_cleans_up(self):
        task = await self.start_ready()
        await self.stop_task(task)
        self.assertTrue(self.disconnected.is_set())
        self.assertFalse(self.control.exists())
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', self.port)), 0)

    async def test_later_http_exit_fails_service_and_cleans_other_entrypoint(self):
        task = await self.start_ready()
        self.live._dashboard._http_server.should_exit = True
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(task, 2)
        self.assertIn('HTTP', self.main.headless.error_text(caught.exception))
        self.assertTrue(self.disconnected.is_set())
        self.assertFalse(self.control.exists())

    async def test_http_coroutine_failure_after_bind_cleans_all_resources(self):
        import uvicorn
        original = uvicorn.Server.startup
        async def fail_after_bind(server, sockets=None):
            await original(server, sockets=sockets)
            raise RuntimeError('synthetic HTTP coroutine startup failure')
        with patch.object(uvicorn.Server, 'startup', new=fail_after_bind):
            await self.assert_startup_fails('synthetic HTTP coroutine startup failure')
        await asyncio.sleep(0)
        self.assertFalse(any('LifespanOn.main' in str(t.get_coro()) for t in
                             asyncio.all_tasks() - self.before))

    async def test_later_control_listener_close_fails_service_and_cleans_http(self):
        task = await self.start_ready()
        self.live._dashboard._control_server.close()
        with self.assertRaises(Exception) as caught:
            await asyncio.wait_for(task, 2)
        self.assertIn('pairing control', self.main.headless.error_text(caught.exception))
        self.assertTrue(self.disconnected.is_set())
        self.assertFalse(self.control.exists())

    async def test_missing_or_invalid_config_has_distinct_exit_status(self):
        for value in ({}, {'gemini_api_key': ''}, {'gemini_api_key': 123}, None):
            with self.subTest(value=value):
                path = self.main.API_CONFIG_PATH
                path.write_text('invalid json' if value is None else json.dumps(value))
                try:
                    async with asyncio.timeout(.4):
                        await self.live.run()
                except SystemExit as exc:
                    self.assertEqual(exc.code, 78)
                except TimeoutError:
                    self.fail('invalid local config retried instead of EX_CONFIG')
                self.assertFalse(self.connecting.is_set())

    async def test_invalid_bind_configuration_exits_config_not_retryable_failure(self):
        with patch.dict(os.environ, {'JARVIS_BIND': '0.0.0.0'}):
            with self.assertRaises(SystemExit) as caught:
                async with asyncio.timeout(.5):
                    await self.live.run()
        self.assertEqual(caught.exception.code, 78)


class Phone:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.messages = []
    async def accept(self): pass
    async def send_json(self, msg): self.messages.append(msg)
    async def close(self, **kwargs):
        self.incoming.put_nowait({'type': 'websocket.disconnect'})
    async def receive(self): return await self.incoming.get()
    def pcm(self, data): self.incoming.put_nowait({'type':'websocket.receive', 'bytes': data})
    def end(self): self.incoming.put_nowait({'type':'websocket.receive', 'text':'{"type":"audio_stream_end"}'})
    def disconnect(self): self.incoming.put_nowait({'type':'websocket.disconnect'})


class PhoneClosureTests(LocalServiceFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = self.live._dashboard = DashboardServer()
        self.server.set_audio_allowed_callback(self.live.audio_allowed)
        self.server._tokens.add('synthetic')
        self.route = next(r.endpoint for r in self.server.app.routes if r.path == '/ws/phone-audio')
        self.live.out_queue = asyncio.Queue(maxsize=200)
        self.sent = []
        async def send(**kwargs):
            self.sent.append('end' if kwargs.get('audio_stream_end') else kwargs['audio'].data)
        self.live.session = NS(send_realtime_input=send)
        self.tasks = [asyncio.create_task(self.live._relay_phone_audio()),
                      asyncio.create_task(self.live._send_realtime())]

    async def asyncTearDown(self):
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def phone(self):
        phone = Phone()
        task = asyncio.create_task(self.route(phone, token='synthetic'))
        self.tasks.append(task)
        await eventually(lambda: phone.messages)
        self.assertEqual(phone.messages[0], {'type':'audio_ready'})
        return phone, task

    async def test_disconnect_after_forwarded_pcm_closes_then_reconnect_is_clean(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        phone.disconnect()
        await task
        await eventually(lambda: 'end' in self.sent)
        self.assertFalse(self.live._input_stream_open)
        self.assertIsNone(self.server._audio_owner)
        replacement, task = await self.phone()
        replacement.pcm(b'\x02\x00')
        replacement.end()
        await task
        await eventually(lambda: len(self.sent) == 4)
        self.assertEqual(self.sent, [b'\x01\x00', 'end', b'\x02\x00', 'end'])
        self.assertFalse(self.live._input_stream_open)

    async def test_revocation_closes_forwarded_input_even_before_route_finally(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        self.server.new_key()
        await asyncio.gather(*self.server._revocation_tasks)
        await task
        await eventually(lambda: 'end' in self.sent)
        self.assertEqual(self.sent, [b'\x01\x00', 'end'])
        self.assertFalse(self.live._input_stream_open)

    async def test_abrupt_disconnect_discards_queued_pcm_but_preserves_closure(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        # Route can queue PCM without relay/sender getting a scheduling turn.
        for _ in range(20): phone.pcm(b'\x09\x00')
        phone.disconnect()
        await task
        await eventually(lambda: 'end' in self.sent)
        self.assertEqual(self.sent, [b'\x01\x00', 'end'])

    async def test_explicit_end_preserves_all_accepted_fifo_pcm(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        phone.pcm(b'\x02\x00')
        phone.end()
        await task
        await eventually(lambda: len(self.sent) == 3)
        self.assertEqual(self.sent, [b'\x01\x00', b'\x02\x00', 'end'])

    async def test_explicit_end_survives_sender_backpressure_and_frame_expiry(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        # Stall the real sender after the first actual upstream send. An end
        # remains a control obligation even once the PCM freshness window ends.
        self.tasks[1].cancel()
        await asyncio.gather(self.tasks[1], return_exceptions=True)
        phone.end()
        await task
        await eventually(lambda: not self.live.out_queue.empty())
        self.live.out_queue._queue[0]['at'] = time.monotonic() - 2
        sender = asyncio.create_task(self.live._send_realtime())
        self.tasks.append(sender)
        await eventually(lambda: 'end' in self.sent)
        self.assertFalse(self.live._input_stream_open)

    async def test_delayed_explicit_old_capture_end_cannot_end_new_capture(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        old_capture = self.server._capture_serial
        phone.end()
        await task
        await eventually(lambda: len(self.sent) == 2)
        replacement, task = await self.phone()
        replacement.pcm(b'\x02\x00')
        await eventually(lambda: len(self.sent) == 3)
        self.server._phone_audio_queue.put_nowait({'audio_stream_end': True,
            'epoch': self.server._audio_epoch, 'capture': old_capture, 'at': time.monotonic()})
        await asyncio.sleep(.03)
        self.assertEqual(self.sent, [b'\x01\x00', 'end', b'\x02\x00'])
        self.assertTrue(self.live._input_stream_open)
        replacement.end()
        await task
        await eventually(lambda: len(self.sent) == 4)

    async def test_delayed_old_epoch_close_does_not_end_new_capture(self):
        phone, task = await self.phone()
        phone.pcm(b'\x01\x00')
        await eventually(lambda: self.sent)
        old_epoch = self.server._audio_epoch
        phone.disconnect()
        await task
        await eventually(lambda: 'end' in self.sent)
        replacement, task = await self.phone()
        replacement.pcm(b'\x02\x00')
        await eventually(lambda: len(self.sent) == 3)
        self.server._phone_audio_queue.put_nowait(
            {'audio_stream_end': True, 'control': True, 'epoch': old_epoch})
        await asyncio.sleep(.03)
        self.assertEqual(self.sent, [b'\x01\x00', 'end', b'\x02\x00'])
        self.assertTrue(self.live._input_stream_open)
        replacement.end()
        await task
        await eventually(lambda: len(self.sent) == 4)


class ProcessExitTests(LocalServiceFixture, unittest.TestCase):
    def child(self):
        import subprocess
        import sys
        script = '''
import os
from pathlib import Path
import memory.config_manager as config
config.CONFIG_FILE = Path(os.environ['TEST_CONFIG_PATH'])
import main
import dashboard.server as dashboard
main.API_CONFIG_PATH = config.CONFIG_FILE
dashboard.PORT = int(os.environ['TEST_HTTP_PORT'])
def offline_client(**kwargs):
    raise RuntimeError('API key not valid')
main.genai.Client = offline_client
main.main()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, 'TEST_CONFIG_PATH': str(self.main.API_CONFIG_PATH),
                 'TEST_HTTP_PORT': str(self.port)}, capture_output=True, text=True, timeout=10)
        self.assertFalse(self.control.exists(), result.stderr)
        return result

    def test_real_process_auth_and_missing_credentials_exit_config(self):
        invalid = self.child()
        self.assertEqual(invalid.returncode, 78, invalid.stderr)
        self.main.API_CONFIG_PATH.write_text('{}')
        missing = self.child()
        self.assertEqual(missing.returncode, 78, missing.stderr)

    def test_real_process_listener_failures_are_retryable_by_unit_policy(self):
        import configparser
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(Path(__file__).resolve().parents[1]/'deploy/jarvis.service')
        self.assertEqual(unit['Service']['Restart'], 'on-failure')
        prevented = set(unit['Service']['RestartPreventExitStatus'].split())
        self.assertIn('78', prevented)
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', self.port))
            occupied.listen()
            failed = self.child()
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertNotIn(str(failed.returncode), prevented)
        self.control.parent.mkdir(mode=0o700, exist_ok=True)
        self.control.parent.chmod(0o755)
        failed = self.child()
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertNotIn(str(failed.returncode), prevented)


class ServedHelpTests(unittest.TestCase):
    def test_served_login_and_expired_help_use_secure_headless_instructions(self):
        client = TestClient(DashboardServer().app)
        for url in ('/login', '/auto-login?key=expired'):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                text = response.text.lower()
                for unsafe in ('chrome://flags', 'insecure origins', 'desktop app', 'remote control</strong>'):
                    self.assertNotIn(unsafe, text)
                for useful in ('https', 'safari', 'microphone', 'python -m core.headless pair'):
                    self.assertIn(useful, text)


if __name__ == '__main__':
    unittest.main()
