import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, Mock, AsyncMock

from ui_headless import JarvisUI


class HeadlessTests(unittest.IsolatedAsyncioTestCase):
    def make_live(self):
        import main
        from core.turn_provenance import CHANNEL_HTTP_TEXT, create_stable
        from core.user_turn import finalize_turn
        with patch('memory.config_manager.load_api_keys', return_value={'gemini_api_key': 'test-key'}):
            live = main.JarvisLive(JarvisUI())
        # Dispatch tests represent the steady state after a finalized input
        # turn; pre-finalization rejection has its own test in test_turn_finalization.
        live._finalized_turn = finalize_turn(create_stable(
            CHANNEL_HTTP_TEXT, stable_device_id='durable-device',
            auth_session_id='auth-session-test', final=True))
        return live

    def test_confirmation_fails_closed(self):
        from core import confirm
        ui = JarvisUI()
        with self.assertRaises(PermissionError):
            ui.show_confirm('Shutdown', 'Do not execute')
        executed = Mock()
        confirm.bind(ui.show_confirm, ui.hide_confirm)
        result = confirm.request('x', 'Shutdown', 'details', executed)
        confirm.resolve(True)
        executed.assert_not_called()
        self.assertIn('Nothing was done', result)
        confirm.bind(None, None)

    def test_missing_key_exits_without_gui(self):
        for config in ({}, {'gemini_api_key': ''}, {'gemini_api_key': '  '},
                       {'gemini_api_key': 123}, []):
            with self.subTest(config=config):
                with patch('memory.config_manager.load_api_keys', return_value=config):
                    ui = JarvisUI()
                with self.assertRaises(SystemExit) as missing:
                    ui.wait_for_api_key()
                self.assertEqual(missing.exception.code, 78)
        with self.assertRaises(SystemExit) as invalid:
            ui.prompt_reconfig()
        self.assertEqual(invalid.exception.code, 78)

    def test_defaults(self):
        from core.headless import enabled, capture_source
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(enabled())
            self.assertEqual(capture_source(), 'phone')
        with patch.dict(os.environ, {'JARVIS_CAPTURE_SOURCE': 'bogus'}):
            with self.assertRaises(ValueError):
                capture_source()

    def test_nested_authentication_failure_is_not_hidden_by_taskgroup(self):
        from core.headless import error_text
        error = ExceptionGroup('session failed', [ExceptionGroup('recv failed', [RuntimeError('API key not valid')])])
        self.assertIn('API key not valid', error_text(error))

    async def test_capture_phone_does_not_open_local_input(self):
        import main
        live = self.make_live()
        with patch.dict('sys.modules', {'sounddevice': Mock(InputStream=Mock(side_effect=AssertionError('local mic')))}), patch.object(main.audio_devices, 'resolve', side_effect=AssertionError('probe')):
            await asyncio.wait_for(live._listen_audio(), timeout=0.1)

    async def test_allowlist_and_dispatch_reject_writes(self):
        live = self.make_live()
        cfg = live._build_config()
        decls = cfg.tools[0].function_declarations
        self.assertEqual([d.name for d in decls],
                         ['home_assistant', 'spotify_control', 'cannanas'])
        # The Cannanas action enum is the exact closed Release-1 list.
        cannanas_decl = decls[2]
        from core.cannanas_actions import CANNANAS_MODEL_ACTIONS
        canonical = sorted(cannanas_decl.parameters.properties['action'].enum)
        self.assertEqual(canonical, sorted([
            'overview', 'find_zones', 'find_strains', 'find_plants',
            'find_propagation_material', 'list_recent_batches', 'list_plants',
            'count_plants_by_room', 'prepare_plant_create', 'prepare_strain_create',
            'prepare_batch_create', 'prepare_plant_update', 'prepare_batch_update',
            'prepare_diary_create']))
        self.assertEqual(
            decls[1].parameters.properties['action'].enum,
            ['play', 'pause', 'resume', 'next', 'previous', 'volume', 'current'])
        for name, args in [('home_assistant', {'action': 'set', 'entity': 'light.x', 'value': 'on'}), ('shutdown_jarvis', {}), ('save_memory', {'key':'x','value':'y'}), ('cannabis_status', {})]:
            with patch('plugins.home_assistant.run', side_effect=AssertionError('dispatched forbidden action')):
                result = await live._execute_tool(NS(name=name, args=args, id='1'))
            self.assertIn('denied', str(result.response).lower())
        for action in ('play', 'pause', 'resume', 'next', 'previous', 'volume', 'current'):
            with patch('plugins.spotify_control.run', return_value='ok') as run:
                result = await live._execute_tool(NS(name='spotify_control', args={'action': action}, id='1'))
            run.assert_called_once()
            self.assertEqual(result.response['result'], 'ok')
        with patch('plugins.spotify_control.run', side_effect=AssertionError('unknown action executed')):
            result = await live._execute_tool(NS(name='spotify_control', args={'action': 'bogus'}, id='1'))
        self.assertIn('denied', str(result.response).lower())

    async def test_speaking_tail_and_stop_discard_input(self):
        live = self.make_live()
        live.session = NS(send_realtime_input=AsyncMock())
        live.out_queue = asyncio.Queue()
        live.audio_in_queue = asyncio.Queue()
        live.out_queue.put_nowait({'data': b'old', 'mime_type':'audio/pcm;rate=16000'})
        live.audio_in_queue.put_nowait(b'old')
        live.set_speaking(True)
        self.assertFalse(live.audio_allowed())
        self.assertTrue(live.out_queue.empty())
        live.interrupt()
        self.assertTrue(live.audio_in_queue.empty())
        live._interrupted = False
        self.assertFalse(live.audio_allowed())  # tail is still locked
        live._tail_until = 0
        self.assertTrue(live.audio_allowed())

    async def test_sender_rejects_stale_and_sends_end_after_valid_pcm(self):
        import main
        from dashboard.server import DashboardServer
        live = self.make_live()
        live.session = NS(send_realtime_input=AsyncMock())
        live._dashboard = DashboardServer()
        live._dashboard.set_audio_allowed_callback(live.audio_allowed)
        live.out_queue = asyncio.Queue()
        stale = {'data': b'old', 'epoch': -1, 'at': 0}
        live.out_queue.put_nowait(stale)
        live.out_queue.put_nowait({'data': b'\x00\x01', 'mime_type':'audio/pcm;rate=16000', 'epoch':0, 'at': main.time.monotonic()})
        live.out_queue.put_nowait({'audio_stream_end':True, 'epoch':0, 'at': main.time.monotonic()})
        task = asyncio.create_task(live._send_realtime())
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        calls = live.session.send_realtime_input.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].kwargs['audio'].mime_type, 'audio/pcm;rate=16000')
        self.assertEqual(calls[1].kwargs, {'audio_stream_end':True})

    async def test_stop_ends_already_open_input_stream_even_while_gate_is_closed(self):
        live = self.make_live()
        live.session = NS(send_realtime_input=AsyncMock())
        live.out_queue = asyncio.Queue()
        live._input_stream_open = True
        live._turn_active = True
        live.interrupt()
        task = asyncio.create_task(live._send_realtime())
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        live.session.send_realtime_input.assert_awaited_once_with(audio_stream_end=True)

    async def test_interrupted_turn_never_requeues_until_complete(self):
        live = self.make_live()
        live.session = NS()
        live.audio_in_queue = asyncio.Queue()
        live._turn_done_event = asyncio.Event()
        live._interrupted = True
        def response(data, done=False):
            return NS(data=data, server_content=NS(output_transcription=None, input_transcription=None, turn_complete=done), tool_call=None)
        async def receive():
            yield response(b'discard')
            yield response(b'discard-too', True)
            yield response(b'new-turn', True)
            raise asyncio.CancelledError()
        live.session = NS(receive=receive)
        with self.assertRaises(asyncio.CancelledError):
            await live._receive_audio()
        self.assertEqual(live.audio_in_queue.get_nowait(), b'new-turn')
        self.assertTrue(live.audio_in_queue.empty())
        self.assertFalse(live._interrupted)

    async def test_interrupted_tool_request_is_denied_but_acknowledged(self):
        live = self.make_live()
        live._interrupted = True
        send = AsyncMock()
        async def receive():
            yield NS(data=None, server_content=None, tool_call=NS(function_calls=[
                NS(id='tool',name='home_assistant',args={'action':'list'})]))
            self.assertEqual(send.await_count,1,'a missing tool response stalls turn completion')
            raise asyncio.CancelledError()
        live.session = NS(receive=receive,send_tool_response=send)
        with patch('plugins.home_assistant.run', side_effect=AssertionError('interrupted tool executed')):
            with self.assertRaises(asyncio.CancelledError):
                await live._receive_audio()
        self.assertIn('interrupted', str(send.call_args).lower())

    async def test_stop_during_dashboard_send_suppresses_reply_until_complete(self):
        from dashboard.server import DashboardServer
        live = self.make_live()
        live._dashboard = DashboardServer()
        live.audio_in_queue = asyncio.Queue()
        live._turn_done_event = asyncio.Event()
        started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            started.set()
            await release.wait()
            sent.set()

        live.session = NS(send_client_content=send, send_tool_response=AsyncMock())
        live._dashboard._command_queue.put_nowait('first command')
        task = asyncio.create_task(live._process_dashboard_commands())
        try:
            await asyncio.wait_for(started.wait(), 1)
            live.interrupt()
            release.set()
            await asyncio.wait_for(sent.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        def response(data=None, done=False, tool=None):
            return NS(data=data, tool_call=tool, server_content=NS(
                output_transcription=None, input_transcription=None, turn_complete=done))

        async def receive():
            yield response(b'discard')
            yield response(tool=NS(function_calls=[
                NS(id='tool', name='home_assistant', args={'action': 'list'})]))
            yield response(b'discard-too', done=True)
            # A later command must work after the interrupted turn completes.
            live._dashboard._command_queue.put_nowait('next command')
            sent.clear()
            next_task = asyncio.create_task(live._process_dashboard_commands())
            try:
                await asyncio.wait_for(sent.wait(), 1)
            finally:
                next_task.cancel()
                await asyncio.gather(next_task, return_exceptions=True)
            yield response(b'new-turn', done=True)
            raise asyncio.CancelledError()

        live.session.receive = receive
        with patch.object(live, '_execute_tool', new=AsyncMock()) as execute:
            with self.assertRaises(asyncio.CancelledError):
                await live._receive_audio()
        self.assertEqual(live.audio_in_queue.get_nowait(), b'new-turn')
        self.assertTrue(live.audio_in_queue.empty())
        execute.assert_not_awaited()
        replies = live.session.send_tool_response.call_args.kwargs['function_responses']
        self.assertIn('interrupted', replies[0].response['error'].lower())
        self.assertFalse(live._interrupted)
        self.assertFalse(live._turn_active)

    async def test_dashboard_send_finishing_after_reply_does_not_reactivate_turn(self):
        from dashboard.server import DashboardServer
        live = self.make_live()
        live._dashboard = DashboardServer()
        live.audio_in_queue = asyncio.Queue()
        started, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def send(**kwargs):
            started.set()
            await release.wait()
            sent.set()

        async def receive():
            yield NS(data=None, tool_call=None, server_content=NS(
                output_transcription=None, input_transcription=None, turn_complete=True))
            raise asyncio.CancelledError()

        live.session = NS(send_client_content=send, receive=receive)
        live._dashboard._command_queue.put_nowait('command')
        task = asyncio.create_task(live._process_dashboard_commands())
        try:
            await asyncio.wait_for(started.wait(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await live._receive_audio()
            release.set()
            await asyncio.wait_for(sent.wait(), 1)
            self.assertFalse(live._turn_active)
            live.interrupt()
            self.assertFalse(live._interrupted)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_failed_dashboard_send_cleans_only_its_own_pending_turn(self):
        from dashboard.server import DashboardServer
        for failure in (RuntimeError, asyncio.CancelledError):
            for progress in ('none', 'audio', 'tool', 'complete', 'already-active'):
                with self.subTest(failure=failure.__name__, progress=progress):
                    live = self.make_live()
                    live._dashboard = DashboardServer()
                    live.audio_in_queue = asyncio.Queue()
                    live._turn_done_event = asyncio.Event()
                    live._turn_active = progress == 'already-active'
                    started, release = asyncio.Event(), asyncio.Event()

                    async def send(**kwargs):
                        started.set()
                        await release.wait()
                        raise failure('synthetic send failure')

                    live.session = NS(send_client_content=send, send_tool_response=AsyncMock())
                    live._dashboard._command_queue.put_nowait('command')
                    task = asyncio.create_task(live._process_dashboard_commands())
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        if progress in ('audio', 'tool', 'complete'):
                            async def receive():
                                tool = (NS(function_calls=[NS(id='tool', name='shutdown_jarvis', args={})])
                                        if progress == 'tool' else None)
                                content = (None if tool else NS(output_transcription=None,
                                           input_transcription=None, turn_complete=progress == 'complete'))
                                yield NS(data=None if tool else b'answer', tool_call=tool,
                                         server_content=content)
                                raise asyncio.CancelledError()
                            live.session.receive = receive
                            with self.assertRaises(asyncio.CancelledError):
                                await live._receive_audio()
                        live.interrupt()
                        release.set()
                        # Reaching the error handler proves send cleanup finished.
                        with patch('main.print') as log:
                            if failure is RuntimeError:
                                for _ in range(100):
                                    if any('Command error' in str(c) for c in log.call_args_list):
                                        break
                                    await asyncio.sleep(0)
                                else:
                                    self.fail('send failure was not handled')
                            else:
                                with self.assertRaises(asyncio.CancelledError):
                                    await task
                        active = progress in ('audio', 'tool', 'already-active')
                        self.assertEqual(live._turn_active, active)
                        self.assertEqual(live._interrupted, active)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_invalid_key_stops_without_retry_or_private_gui(self):
        import main
        for error in (RuntimeError('API key not valid'), ExceptionGroup('tasks', [RuntimeError('API key not valid')])):
            with self.subTest(error=type(error).__name__):
                live = self.make_live()
                with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                        'JARVIS_PUBLIC_URL':'https://example.test',
                        'JARVIS_CONTROL_SOCKET':str(Path(directory)/'private'/'control.sock')}), patch(
                        'dashboard.server.PORT', 0), patch.object(main, '_get_api_key', return_value='synthetic'), patch.object(
                        main.genai, 'Client', side_effect=error) as client:
                    with self.assertRaises(SystemExit) as caught:
                        async with asyncio.timeout(2):
                            await live.run()
                    self.assertEqual(caught.exception.code, 78)
                    self.assertEqual(client.call_count, 1)
                    self.assertFalse((Path(directory)/'private'/'control.sock').exists())

    async def test_stop_while_idle_does_not_poison_next_turn(self):
        live = self.make_live()
        live.session = NS()
        live._turn_done_event = asyncio.Event()
        live.interrupt()
        self.assertFalse(live._interrupted)

    async def test_headless_selected_output_failure_does_not_silently_route_elsewhere(self):
        import main
        live = self.make_live()
        hardware = Mock()
        hardware.RawOutputStream.side_effect = RuntimeError('selected device unavailable')
        hardware.query_devices.return_value = [{'name':'Pulse analog','max_output_channels':2}]
        with patch.dict(os.environ, {'JARVIS_DUCKING':'0'}), patch.dict('sys.modules',{'sounddevice':hardware}), patch.object(main,'get_output_device',return_value='Pulse analog'):
            with self.assertRaises(RuntimeError):
                await live._play_audio()
        self.assertEqual(hardware.RawOutputStream.call_count,1)

    def test_headless_output_resolver_does_not_probe_input_or_use_prefix_fallback(self):
        from core import headless
        hardware = Mock()
        hardware.query_devices.return_value = [
            {'name':'microphone','max_input_channels':1,'max_output_channels':0},
            {'name':'Pulse analog','max_output_channels':2}]
        self.assertEqual(headless.output_device('Pulse analog', hardware),1)
        hardware.check_input_settings.assert_not_called()
        with self.assertRaises(RuntimeError):
            headless.output_device('Pulse', hardware)

    async def test_first_received_audio_immediately_closes_capture_before_playback(self):
        live = self.make_live()
        live.audio_in_queue = asyncio.Queue()
        live.out_queue = asyncio.Queue()
        live.out_queue.put_nowait({'data':b'stale'})
        async def receive():
            yield NS(data=b'answer', server_content=None, tool_call=None)
            self.assertFalse(live.audio_allowed())
            self.assertTrue(live.out_queue.empty())
            raise asyncio.CancelledError()
        live.session = NS(receive=receive)
        with self.assertRaises(asyncio.CancelledError):
            await live._receive_audio()

    async def test_playback_stop_during_write_discards_rest_and_waits_for_turn_complete(self):
        import main
        live = self.make_live()
        live.session = NS()
        live.audio_in_queue = asyncio.Queue()
        live.out_queue = asyncio.Queue()
        live._turn_done_event = asyncio.Event()
        live._turn_active = True
        live.audio_in_queue.put_nowait(b'\x01\x00'*100)
        started = __import__('threading').Event()
        release = __import__('threading').Event()
        def write(data):
            started.set()
            release.wait(2)
        stream = Mock(latency=.01, write=Mock(side_effect=write))
        hardware = Mock(RawOutputStream=Mock(return_value=stream))
        hardware.query_devices.return_value = [
            {'name': 'Test Pulse output', 'max_output_channels': 2}]
        # This test owns only fake hardware. Never contact the real mixer when
        # the complete suite is run alongside the live service on the target.
        with patch.dict(os.environ, {'JARVIS_DUCKING':'0'}), patch.dict('sys.modules', {'sounddevice': hardware}), patch.object(
                main, 'get_output_device', return_value='Test Pulse output'):
            task = asyncio.create_task(live._play_audio())
            try:
                writing = await asyncio.to_thread(started.wait, 1)
                if task.done():
                    await task  # surface an early resolver/stream exception, not a timeout
                self.assertTrue(writing)
                self.assertEqual(hardware.RawOutputStream.call_args.kwargs['device'], 0)
                live.audio_in_queue.put_nowait(b'discard')
                live.interrupt()
                self.assertTrue(live._interrupted)
                self.assertFalse(live.audio_allowed())
                release.set()
                await asyncio.sleep(.02)
                self.assertEqual(stream.write.call_count,1)
                self.assertTrue(live.audio_in_queue.empty())
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
        stream.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
