import asyncio
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from dashboard.server import DashboardServer
from core.headless import request_control, request_pair, control_path
from core.spotify import AuthorizationStart, AuthorizationStatus, SpotifyConfigError
from datetime import datetime, timezone


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.server = DashboardServer(public_url='https://lacasa-server.tail7a9c2.ts.net', host='127.0.0.1')
        self.client = TestClient(self.server.app)
        self.key = self.server.new_key()
        self.token = self.client.post('/login', json={'pin':self.key}).json()['token']
        self.headers = {'Authorization':f'Bearer {self.token}'}

    def test_auth_status_interrupt_and_command(self):
        cb = Mock()
        self.server.set_interrupt_callback(cb)
        for route, method in [('/api/status','get'),('/api/interrupt','post'),('/api/command','post')]:
            self.assertEqual(getattr(self.client,method)(route, json={} if method=='post' else None).status_code if method=='post' else self.client.get(route).status_code,401)
        self.assertEqual(self.client.get('/api/status',headers=self.headers).json()['audio_allowed'],False)
        self.server.set_audio_allowed_callback(lambda:True)
        self.assertTrue(self.client.get('/api/status',headers=self.headers).json()['audio_allowed'])
        self.assertEqual(self.client.post('/api/interrupt',headers=self.headers).status_code,200)
        cb.assert_called_once()
        self.assertEqual(self.client.post('/api/command',headers=self.headers,json={'text':'hello'}).status_code,200)
        self.assertEqual(self.server._command_queue.get_nowait(),'hello')

    def test_invalid_expired_and_rotation_revokes_every_credential(self):
        self.assertEqual(self.client.post('/login',json={'pin':'WRONG'}).status_code,401)
        expired=self.server.new_key(expiry_secs=-1)
        self.assertEqual(self.client.post('/login',json={'pin':expired}).status_code,401)
        key=self.server.new_key()
        html=self.client.get('/auto-login',params={'key':key}).text
        import re
        device=re.search("jarvis_device_token','([^']+)",html).group(1)
        self.assertEqual(self.client.post('/api/device-login',json={'device_token':device}).status_code,200)
        self.server.new_key()
        self.assertEqual(self.client.get('/api/status',headers=self.headers).status_code,401)
        self.assertEqual(self.client.post('/api/device-login',json={'device_token':device}).status_code,401)
        self.assertEqual(self.client.post('/login',json={'pin':key}).status_code,401)
        self.assertEqual(self.server.get_url(),'https://lacasa-server.tail7a9c2.ts.net')

    def test_websocket_current_status_and_auth(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/ws?token=bad'):pass
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/ws/phone-audio?token=bad'):pass
        with self.client.websocket_connect(f'/ws?token={self.token}') as ws:
            self.assertEqual(ws.receive_json()['type'],'status')
            ws.send_json({'type':'command','text':'hi'})
        self.assertEqual(self.server._command_queue.get_nowait(),'hi')

    def test_single_source_gate_end_order_and_stale_frame(self):
        self.server.set_audio_allowed_callback(lambda:True)
        url=f'/ws/phone-audio?token={self.token}'
        with self.client.websocket_connect(url) as ws:
            self.assertEqual(ws.receive_json()['type'],'audio_ready')
            with self.assertRaises(WebSocketDisconnect) as second:
                with self.client.websocket_connect(url):pass
            self.assertEqual(second.exception.code,4009)
            ws.send_bytes(b'\x01\x02')
            ws.send_json({'type':'audio_stream_end'})
            self.assertEqual(ws.receive_json()['type'],'audio_ended')
        pcm=self.server._phone_audio_queue.get_nowait()
        end=self.server._phone_audio_queue.get_nowait()
        self.assertEqual(pcm['mime_type'],'audio/pcm;rate=16000')
        self.assertTrue(end['audio_stream_end'])
        self.assertTrue(self.server.audio_message_valid(pcm))
        self.server.invalidate_audio()
        self.assertFalse(self.server.audio_message_valid(pcm))
        self.server.set_audio_allowed_callback(lambda:False)
        with self.assertRaises(WebSocketDisconnect) as busy:
            with self.client.websocket_connect(url):pass
        self.assertEqual(busy.exception.code,4009)

    def test_gate_rechecked_on_existing_stream(self):
        allowed=[True]
        self.server.set_audio_allowed_callback(lambda:allowed[0])
        with self.client.websocket_connect(f'/ws/phone-audio?token={self.token}') as ws:
            ws.receive_json()
            allowed[0]=False
            ws.send_bytes(b'\x00\x01')
            with self.assertRaises(WebSocketDisconnect):
                ws.receive_json()
        self.assertTrue(self.server._phone_audio_queue.empty())

    def test_rotation_closes_real_control_and_audio_websockets(self):
        self.server.set_audio_allowed_callback(lambda:True)
        with TestClient(self.server.app) as client:
            with client.websocket_connect(f'/ws?token={self.token}') as control:
                control.receive_json()
                with client.websocket_connect(f'/ws/phone-audio?token={self.token}') as audio:
                    audio.receive_json()
                    client.portal.call(self.server.new_key)
                    # Already-sent history may precede the close control frame.
                    with self.assertRaises(WebSocketDisconnect):
                        for _ in range(4):
                            control.receive_json()
                    with self.assertRaises(WebSocketDisconnect):
                        audio.receive_json()
        self.assertEqual(self.client.get('/api/status',headers=self.headers).status_code,401)


class WebSocketHandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_audio_handshake_failure_releases_owner_and_allows_reconnect(self):
        for stage in ('accept', 'send'):
            for failure in (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                with self.subTest(stage=stage, failure=failure.__name__):
                    server = DashboardServer()
                    server._tokens.add('test-token')
                    server.set_audio_allowed_callback(lambda: True)
                    route = next(r.endpoint for r in server.app.routes
                                 if r.path == '/ws/phone-audio')
                    stale = {'data': b'\x00\x01', 'epoch': server._audio_epoch,
                             'at': time.monotonic()}
                    server._phone_audio_queue.put_nowait(stale)
                    socket = HandshakeSocket(stage, failure)
                    try:
                        await route(socket, token='test-token')
                    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                        pass
                    self.assertIsNone(server._audio_owner)
                    self.assertNotIn(socket, server._audio_clients)
                    self.assertTrue(server._phone_audio_queue.empty())
                    self.assertFalse(server.audio_message_valid(stale))
                    replacement = HandshakeSocket()
                    await route(replacement, token='test-token')
                    self.assertEqual(replacement.messages,
                                     [{'type': 'audio_ready'}, {'type': 'audio_ended'}])
                    self.assertIsNone(server._audio_owner)
                    self.assertFalse(server._audio_clients)

    async def test_control_handshake_failure_does_not_leave_registered_client(self):
        for stage in ('accept', 'send'):
            for failure in (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                with self.subTest(stage=stage, failure=failure.__name__):
                    server = DashboardServer()
                    server._tokens.add('test-token')
                    route = next(r.endpoint for r in server.app.routes if r.path == '/ws')
                    socket = HandshakeSocket(stage, failure)
                    try:
                        await route(socket, token='test-token')
                    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
                        pass
                    self.assertNotIn(socket, server._clients)
                    replacement = HandshakeSocket()
                    await route(replacement, token='test-token')
                    self.assertEqual(replacement.messages[0]['type'], 'status')
                    self.assertFalse(server._clients)


class HandshakeSocket:
    """Transport fault injection; production route/state/cleanup stay real."""
    def __init__(self, stage=None, failure=None):
        self.stage, self.failure = stage, failure
        self.messages = []

    def fail_at(self, stage):
        if self.stage == stage:
            raise self.failure(1006) if self.failure is WebSocketDisconnect else self.failure()

    async def accept(self):
        self.fail_at('accept')

    async def send_json(self, message):
        self.fail_at('send')
        self.messages.append(message)

    async def receive(self):
        return {'type': 'websocket.receive', 'text': '{"type":"audio_stream_end"}'}

    async def receive_json(self):
        raise WebSocketDisconnect(1000)

    async def close(self, **kwargs):
        pass


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_cli_targets_running_instance_and_rotates_live_sockets(self):
        from core.headless import request_pair
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private'/'control.sock'
            server=DashboardServer(public_url='https://lacasa-server.tail7a9c2.ts.net')
            ws=Mock()
            async def close(**kwargs):pass
            ws.close=Mock(side_effect=close)
            server._clients.add(ws)
            task=asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():break
                    await asyncio.sleep(.01)
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
                url=await asyncio.to_thread(request_pair,path)
                self.assertTrue(url.startswith(server.get_url()+'/auto-login?key='))
                key=url.split('key=')[1]
                self.assertIn(key,server._pending_keys)
                self.assertEqual(ws.close.call_count,1)
                await asyncio.to_thread(request_pair,path)
                self.assertNotIn(key,server._pending_keys)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)


class BrowserVoiceTests(unittest.TestCase):
    def test_half_duplex_capture_auto_resumes_once_and_cancellations_stay_idle(self):
        import subprocess
        html = (Path(__file__).resolve().parents[1]/'dashboard/static/app.html').read_text()
        script = r'''
const vm = require('vm'), assert = require('assert');
let html = require('fs').readFileSync(0, 'utf8');
assert(!html.includes('chrome://flags'));
const elements = new Map();
function element(id) {
 if(!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',value:'',disabled:false,children:[],
 classList:{add(){},remove(){}},addEventListener(){},appendChild(child){this.children.push(child);},
 style:{},querySelector(){return element('child');}});
 return elements.get(id);
}
let sockets = [], permissions = [], stopped = 0, requests = [], mediaCalls = 0;
class WS {
 constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}
 send(value){this.sent.push(value);}
 close(){this.readyState=3;}
}
class AC {
 constructor(){this.state='running';this.sampleRate=16000;this.destination={};}
 resume(){return Promise.resolve();} close(){this.closed=true;return Promise.resolve();}
 createMediaStreamSource(){return {connect(){},disconnect(){}};}
 createScriptProcessor(){return {connect(){},disconnect(){}};}
}
const context = vm.createContext({console,WebSocket:WS,AudioContext:AC,Blob,URL,
 Float32Array,Int16Array,Uint8Array,Math,JSON,Promise,Date,
 document:{getElementById:element,createElement:()=>element(Math.random())},
 sessionStorage:{getItem:k=>k==='jarvis_token'?'token':null},
 location:{protocol:'https:',host:'private.example',origin:'https://private.example',replace(){}},
 navigator:{mediaDevices:{getUserMedia:()=>{mediaCalls++;return new Promise((resolve,reject)=>permissions.push({resolve,reject}));}}},
 fetch:async (url,opts)=>{requests.push(url);return {ok:true,json:async()=>({type:'status',state:'active',audio_allowed:true})};},
 setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},
 addEventListener(){},window:{addEventListener(){}},btoa:s=>Buffer.from(s).toString('base64')});
vm.runInContext([...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(x=>x[1]).join('\n'),context);
const control=sockets[0];
function status(allowed){control.onmessage({data:JSON.stringify({type:'status',state:'active',audio_allowed:allowed})});}
const tick=()=>new Promise(r=>setImmediate(r));
function stream(){return {getTracks:()=>[{stop(){stopped++;}}]};}
async function grant(request){request.resolve(stream());await tick();await tick();}
function messages(){return element('feed').children.map(x=>x.textContent||'').join('\n');}
(async()=>{
 status(true);await tick();
 assert.equal(mediaCalls,0,'an initially idle user must never be recorded automatically');

 let pending=vm.runInContext('doMic()',context), request=permissions.shift();
 assert.equal(mediaCalls,1);
 status(false);await grant(request);await pending;
 assert.equal(stopped,1,'a gate close must stop a permission-pending stream');
 assert.equal(sockets.length,1,'a cancelled permission attempt must not open phone audio');
 status(true);assert.equal(mediaCalls,2,'pending capture resumes once on the next allowed status');
 status(true);await tick();assert.equal(mediaCalls,2,'repeated allowed status must not duplicate pending resume');
 await grant(permissions.shift());
 let voice=sockets.at(-1);voice.readyState=1;
 voice.onmessage({data:JSON.stringify({type:'audio_ready'})});await tick();await tick();

 status(false);await tick();
 assert.equal(stopped,2,'a live stream must pause during JARVIS speech or tail');
 status(true);assert.equal(mediaCalls,3,'live capture resumes once after the speech tail');
 status(true);await tick();assert.equal(mediaCalls,3,'repeated allowed status must not duplicate live resume');
 await grant(permissions.shift());
 voice=sockets.at(-1);voice.readyState=1;
 await vm.runInContext('doMic()',context);await tick();
 assert(voice.sent.some(x=>typeof x==='string'&&JSON.parse(x).type==='audio_stream_end'),
        'explicit microphone stop preserves ordered audio_stream_end');
 status(false);status(true);await tick();
 assert.equal(mediaCalls,3,'explicit microphone stop must cancel auto-resume');

 pending=vm.runInContext('doMic()',context);await grant(permissions.shift());await pending;
 await vm.runInContext('doStop()',context);await tick();status(true);await tick();
 assert.equal(mediaCalls,4,'Stop must cancel auto-resume');
 assert(requests.includes('/api/interrupt'),'Stop uses authenticated control route');

 pending=vm.runInContext('doMic()',context);await grant(permissions.shift());await pending;
 voice=sockets.at(-1);voice.onerror();await tick();status(false);status(true);await tick();
 assert.equal(mediaCalls,5,'phone-audio error or busy must not retry capture');

 pending=vm.runInContext('doMic()',context);await grant(permissions.shift());await pending;
 status(false);status(true);
 assert.equal(mediaCalls,7,'the next allowed status starts one automatic reacquire');
 const denied=Object.assign(new Error('denied'),{name:'NotAllowedError'});
 permissions.shift().reject(denied);await tick();await tick();status(true);await tick();
 assert.equal(mediaCalls,7,'permission failure must clear auto-resume');
 assert.match(messages(),/tap microphone to continue/i,
              'Safari automatic reacquire rejection must explain the manual recovery');

 pending=vm.runInContext('doMic()',context);request=permissions.shift();
 control.onerror();await grant(request);await pending;status(true);await tick();
 assert.equal(mediaCalls,8,'control WebSocket error must clear pending auto-resume');

 pending=vm.runInContext('doMic()',context);await grant(permissions.shift());await pending;
 control.onclose();await tick();status(true);await tick();
 assert.equal(mediaCalls,9,'control WebSocket close must clear active auto-resume');
 console.log('browser voice lifecycle passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run(['node','-e',script],input=html,text=True,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class SpotifyCallbackRouteTests(unittest.TestCase):
    def test_spotify_callback_success_returns_no_store_html(self):
        fake = Mock()
        fake.complete_authorization.return_value = AuthorizationStatus(
            authorized=True, reauthorization_required=False,
            authorized_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
            authorization_expires_at=datetime(2027, 2, 28, 12, 0, tzinfo=timezone.utc),
            access_expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        )
        server = DashboardServer(public_url="https://lacasa-server.tail7a9c2.ts.net", host="127.0.0.1", spotify_oauth=fake)
        client = TestClient(server.app)
        response = client.get("/api/spotify/callback", params={"state": "one-use", "code": "code"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("code", response.text)

    def test_spotify_callback_error_cases_return_400_without_echoing(self):
        fake = Mock()
        fake.complete_authorization.return_value = AuthorizationStatus(
            authorized=False, reauthorization_required=True,
            authorized_at=None, authorization_expires_at=None, access_expires_at=None,
        )
        server = DashboardServer(public_url="https://lacasa-server.tail7a9c2.ts.net", host="127.0.0.1", spotify_oauth=fake)
        client = TestClient(server.app)
        for params in [{}, {"state": "one-use"}, {"state": "stale", "code": "code"},
                       {"state": "replayed", "code": "code"}, {"error": "access_denied"}]:
            with self.subTest(params=params):
                response = client.get("/api/spotify/callback", params=params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.headers["cache-control"], "no-store")
                for value in params.values():
                    self.assertNotIn(value, response.text)


class SpotifyControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_spotify_auth_start_and_status_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "control.sock"
            server = DashboardServer(public_url="https://lacasa-server.tail7a9c2.ts.net")
            fake = Mock()
            fake.start_authorization.return_value = AuthorizationStart(
                url="https://accounts.spotify.com/authorize?x=1",
                state_expires_at=datetime(2026, 1, 1, 12, 10, tzinfo=timezone.utc),
            )
            fake.status.return_value = AuthorizationStatus(
                authorized=True, reauthorization_required=False,
                authorized_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                authorization_expires_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
                access_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            )
            server._spotify_oauth = fake
            task = asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():
                        break
                    await asyncio.sleep(0.01)
                reply = await asyncio.to_thread(request_control, {"command": "spotify-auth-start", "client_id": "CID"}, path)
                self.assertTrue(reply["ok"])
                self.assertEqual(reply["authorization_url"], "https://accounts.spotify.com/authorize?x=1")
                fake.start_authorization.assert_called_once_with("CID")
                reply = await asyncio.to_thread(request_control, {"command": "spotify-auth-status"}, path)
                self.assertTrue(reply["ok"])
                self.assertTrue(reply["authorized"])
                self.assertNotIn("access_token", json.dumps(reply))
                self.assertNotIn("refresh_token", json.dumps(reply))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_control_server_rejects_malformed_framing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "control.sock"
            server = DashboardServer(public_url="https://x")
            task = asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():
                        break
                    await asyncio.sleep(0.01)
                def send_raw():
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                        sock.settimeout(2)
                        sock.connect(str(path))
                        sock.sendall(b'{not json\n')
                        result = b""
                        while b"\n" not in result and len(result) < 4096:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            result += chunk
                        return result
                result = await asyncio.to_thread(send_raw)
                self.assertIn(b"\n", result)
                reply = json.loads(result.partition(b"\n")[0])
                self.assertFalse(reply["ok"])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_control_legacy_pair_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "control.sock"
            server = DashboardServer(public_url="https://lacasa-server.tail7a9c2.ts.net")
            fake = Mock()
            server._spotify_oauth = fake
            task = asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():
                        break
                    await asyncio.sleep(0.01)
                url = await asyncio.to_thread(request_pair, path)
                self.assertTrue(url.startswith("https://lacasa-server.tail7a9c2.ts.net/auto-login?key="))
                fake.start_authorization.assert_not_called()
                fake.status.assert_not_called()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_control_request_sends_only_one_command(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "control.sock"
            server = DashboardServer(public_url="https://lacasa-server.tail7a9c2.ts.net")
            fake = Mock()
            fake.start_authorization.return_value = AuthorizationStart(
                url="https://x",
                state_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            server._spotify_oauth = fake
            task = asyncio.create_task(server.serve_control(path))
            try:
                for _ in range(100):
                    if path.exists():
                        break
                    await asyncio.sleep(0.01)
                def send_two():
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                        sock.settimeout(2)
                        sock.connect(str(path))
                        sock.sendall(b'{"command":"spotify-auth-start","client_id":"CID"}\n{"command":"spotify-auth-start","client_id":"CID"}\n')
                        result = b""
                        while b"\n" not in result and len(result) < 4096:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            result += chunk
                        return result
                result = await asyncio.to_thread(send_two)
                self.assertIn(b"\n", result)
                reply = json.loads(result.partition(b"\n")[0])
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "control frame must contain exactly one command")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()


class SpotifyCallbackRobustnessTests(unittest.TestCase):
    """complete_authorization failures must yield the fixed 400 HTML, never a 500."""

    def _server(self, fake):
        return DashboardServer(
            public_url="https://lacasa-server.tail7a9c2.ts.net", host="127.0.0.1", spotify_oauth=fake
        )

    def test_complete_authorization_exception_returns_400_no_store_html(self):
        fake = Mock()
        fake.complete_authorization.side_effect = RuntimeError("boom SECRET_TOKEN")
        server = self._server(fake)
        client = TestClient(server.app)
        response = client.get("/api/spotify/callback", params={"state": "s", "code": "c"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("SECRET_TOKEN", response.text)
        self.assertNotIn("boom", response.text)

    def test_complete_authorization_config_error_returns_400_html(self):
        fake = Mock()
        fake.complete_authorization.side_effect = SpotifyConfigError("Spotify OAuth store is not an object")
        server = self._server(fake)
        client = TestClient(server.app)
        response = client.get("/api/spotify/callback", params={"state": "s", "code": "c"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("store is not an object", response.text)


class SpotifyOAuthStartupValidationTests(unittest.TestCase):
    """A corrupt OAuth store must fail DashboardServer startup (exit 78 path)."""

    def _patch_default_oauth(self, oauth):
        return patch("dashboard.server.default_oauth", return_value=oauth)

    def test_corrupt_store_fails_dashboard_init(self):
        oauth = Mock()
        oauth.status.side_effect = SpotifyConfigError("malformed Spotify OAuth store")
        with self._patch_default_oauth(oauth):
            with self.assertRaises(SpotifyConfigError):
                DashboardServer(
                    public_url="https://lacasa-server.tail7a9c2.ts.net", host="127.0.0.1"
                )
        oauth.status.assert_called_once()

    def test_missing_store_is_not_an_error(self):
        oauth = Mock()
        oauth.status.return_value = AuthorizationStatus(
            False, True, None, None, None
        )
        with self._patch_default_oauth(oauth):
            server = DashboardServer(
                public_url="https://lacasa-server.tail7a9c2.ts.net", host="127.0.0.1"
            )
        self.assertIs(server._spotify_oauth, oauth)

    def test_injected_oauth_bypasses_default_factory_and_validation(self):
        injected = Mock()
        with patch("dashboard.server.default_oauth") as fake_factory:
            server = DashboardServer(
                public_url="https://lacasa-server.tail7a9c2.ts.net",
                host="127.0.0.1",
                spotify_oauth=injected,
            )
        fake_factory.assert_not_called()
        injected.status.assert_not_called()
        self.assertIs(server._spotify_oauth, injected)
