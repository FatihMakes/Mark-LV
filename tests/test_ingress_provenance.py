"""Immutable ingress provenance envelope: durable device + auth session + capture
+ finalized user-turn identity attached where each ingress source is accepted."""
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from fastapi.testclient import TestClient

from core.device_credentials import DeviceCredentialStore
from core.turn_provenance import (
    CHANNEL_CONTROL_WS_TEXT,
    CHANNEL_HTTP_TEXT,
    CHANNEL_PHONE_PCM,
    IngressEnvelope,
    create_stable,
    validate,
)
from dashboard.server import DashboardServer


def _active_device_ids(store: DeviceCredentialStore) -> frozenset:
    return frozenset(
        summary.device_id for summary in store.list_devices()
        if summary.revoked_at is None
    )


class TestHTTPTextEnvelopeCarriesStableDevice(unittest.TestCase):
    def test_durable_http_text_command_carries_device_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceCredentialStore(Path(directory) / 'devices.json')
            device = store.create('LaCasa iPhone 1')
            server = DashboardServer(public_url='https://example.test', host='127.0.0.1',
                                     device_store=store)
            record = server._sessions.issue(device.summary.device_id)
            with TestClient(server.app) as client:
                headers = {'Authorization': f'Bearer {record.token}'}
                self.assertEqual(
                    client.post('/api/command', headers=headers,
                                json={'text': 'welche pflanzen'}).status_code, 200)
            text = server._command_queue.get_nowait()
            self.assertEqual(text, 'welche pflanzen')
            envelope = text.envelope
            self.assertIsInstance(envelope, IngressEnvelope)
            self.assertEqual(envelope.channel, CHANNEL_HTTP_TEXT)
            self.assertEqual(envelope.stable_device_id, device.summary.device_id)
            self.assertTrue(envelope.final)
            self.assertTrue(validate(envelope, known_devices=_active_device_ids(store)))


class TestControlWSTextEnvelopeCarriesAuthSession(unittest.TestCase):
    def test_control_ws_text_command_carries_auth_session_envelope(self):
        server = DashboardServer(public_url='https://example.test', host='127.0.0.1')
        key = server.new_key()
        with TestClient(server.app) as client:
            token = client.post('/login', json={'pin': key}).json()['token']
            with client.websocket_connect(f'/ws?token={token}') as ws:
                ws.receive_json()
                ws.send_json({'type': 'command', 'text': 'status madrid'})
        text = server._command_queue.get_nowait()
        self.assertEqual(text, 'status madrid')
        envelope = text.envelope
        self.assertEqual(envelope.channel, CHANNEL_CONTROL_WS_TEXT)
        self.assertTrue(envelope.auth_session_id)
        # Durable pin-login sessions carry no device identity; the envelope
        # records that honestly instead of inventing one.
        self.assertIsNone(envelope.stable_device_id)
        self.assertTrue(validate(envelope))


class TestPhonePCMEnvelopeCarriesCaptureID(unittest.TestCase):
    def test_phone_pcm_chunks_and_end_share_capture_and_turn_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceCredentialStore(Path(directory) / 'devices.json')
            device = store.create('LaCasa iPhone 2')
            server = DashboardServer(public_url='https://example.test', host='127.0.0.1',
                                     device_store=store)
            server.set_audio_allowed_callback(lambda: True)
            record = server._sessions.issue(device.summary.device_id)
            with TestClient(server.app) as client:
                with client.websocket_connect(
                        f'/ws/phone-audio?token={record.token}') as ws:
                    ws.receive_json()
                    ws.send_bytes(b'\x01\x02')
                    ws.send_bytes(b'\x03\x04')
                    ws.send_json({'type': 'audio_stream_end'})
                    self.assertEqual(ws.receive_json()['type'], 'audio_ended')
            pcm = server._phone_audio_queue.get_nowait()
            pcm2 = server._phone_audio_queue.get_nowait()
            end = server._phone_audio_queue.get_nowait()
            for chunk in (pcm, pcm2, end):
                envelope = chunk['envelope']
                self.assertEqual(envelope.channel, CHANNEL_PHONE_PCM)
                self.assertEqual(envelope.stable_device_id, device.summary.device_id)
            self.assertFalse(pcm['envelope'].final)
            self.assertFalse(pcm2['envelope'].final)
            self.assertTrue(end['envelope'].final)
            self.assertEqual(pcm['envelope'].capture_id, pcm2['envelope'].capture_id)
            self.assertTrue(pcm['envelope'].capture_id)
            self.assertEqual(pcm['envelope'].capture_id, end['envelope'].capture_id)
            self.assertEqual(pcm['envelope'].user_turn_id, end['envelope'].user_turn_id)
            self.assertEqual(pcm['envelope'].user_turn_id, pcm2['envelope'].user_turn_id)
            self.assertTrue(validate(pcm['envelope'], known_devices=_active_device_ids(store)))


class TestRevokedDeviceRejected(unittest.TestCase):
    def test_validate_fails_closed_for_revoked_and_unknown_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceCredentialStore(Path(directory) / 'devices.json')
            device = store.create('LaCasa iPhone 3')
            device_ids = _active_device_ids(store)
            envelope = create_stable(CHANNEL_HTTP_TEXT,
                                     stable_device_id=device.summary.device_id,
                                     auth_session_id='session-a')
            store.revoke(device.summary.device_id)
            self.assertFalse(validate(envelope, known_devices=_active_device_ids(store)))
            self.assertTrue(validate(envelope))  # structural check stays valid
            unknown = create_stable(CHANNEL_HTTP_TEXT,
                                    stable_device_id='unknown-device-id',
                                    auth_session_id='session-b')
            self.assertFalse(validate(unknown, known_devices=device_ids))

    def test_validate_fails_closed_on_mixed_or_impossible_sources(self):
        # Phone PCM without a capture is a mixed/unknown source.
        self.assertFalse(validate(create_stable(CHANNEL_PHONE_PCM,
                                                stable_device_id='device-id',
                                                auth_session_id='session')))
        # A durable device without an auth session is a mixed source.
        self.assertFalse(validate(create_stable(CHANNEL_HTTP_TEXT,
                                                stable_device_id='device-id',
                                                auth_session_id='')))
        self.assertFalse(validate(create_stable('unknown-channel',
                                                stable_device_id='device-id',
                                                auth_session_id='session')))


class TestBearerRefreshRetainsStableDevice(unittest.TestCase):
    def test_refreshed_bearer_keeps_device_and_changes_auth_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceCredentialStore(Path(directory) / 'devices.json')
            device = store.create('LaCasa iPhone 4')
            server = DashboardServer(public_url='https://example.test', host='127.0.0.1',
                                     device_store=store)
            first = server._sessions.issue(device.summary.device_id)
            server._sessions.discard(first.token)   # bearer refresh: new session
            second = server._sessions.issue(device.summary.device_id)
            known_devices = _active_device_ids(store)
            env_a = create_stable(CHANNEL_HTTP_TEXT,
                                  stable_device_id=device.summary.device_id,
                                  auth_session_id=first.session_key)
            env_b = create_stable(CHANNEL_HTTP_TEXT,
                                  stable_device_id=device.summary.device_id,
                                  auth_session_id=second.session_key)
            self.assertNotEqual(env_a.auth_session_id, env_b.auth_session_id)
            self.assertEqual(env_a.stable_device_id, env_b.stable_device_id)
            self.assertTrue(validate(env_a, known_devices=known_devices,
                                     active_auth_sessions={env_a.auth_session_id}))
            self.assertTrue(validate(env_b, known_devices=known_devices,
                                     active_auth_sessions={env_b.auth_session_id}))
            # A superseded session no longer validates.
            self.assertFalse(validate(env_a, known_devices=known_devices,
                                      active_auth_sessions={env_b.auth_session_id}))


class TestEnvelopeNeverContainsSecrets(unittest.TestCase):
    def test_envelope_fields_and_repr_exclude_bearer_and_device_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeviceCredentialStore(Path(directory) / 'devices.json')
            device = store.create('LaCasa iPhone 5')
            server = DashboardServer(public_url='https://example.test', host='127.0.0.1',
                                     device_store=store)
            record = server._sessions.issue(device.summary.device_id)
            with TestClient(server.app) as client:
                client.post('/api/command', headers={'Authorization': f'Bearer {record.token}'},
                            json={'text': 'hello'})
            text = server._command_queue.get_nowait()
            envelope = text.envelope
            # Secrets are the bearer token, the session key, the durable device
            # secret and any label; the pseudonymous device *id* is by design
            # the envelope's stable identity.
            secrets = [record.token, record.session_key, device.secret,
                       device.summary.label]
            rendered = repr(envelope)
            for secret in secrets:
                self.assertNotIn(str(secret), rendered)
            for field_value in vars(envelope).values():
                for secret in secrets:
                    self.assertNotEqual(str(field_value), str(secret))
            # auth_session_id in the envelope is a one-way digest of the
            # session key; the usable secret itself never leaves the record.
            self.assertNotEqual(envelope.auth_session_id, record.session_key)
            self.assertNotEqual(envelope.auth_session_id, record.token)


class TestEnvelopeIsImmutableAndFresh(unittest.TestCase):
    def test_envelope_is_frozen_and_create_stable_stamps_fresh_turns(self):
        envelope = create_stable(CHANNEL_HTTP_TEXT,
                                 stable_device_id='device-id',
                                 auth_session_id='session')
        with self.assertRaises(FrozenInstanceError):
            envelope.user_turn_id = 999
        other = create_stable(CHANNEL_HTTP_TEXT,
                              stable_device_id='device-id',
                              auth_session_id='session')
        self.assertNotEqual(envelope.user_turn_id, other.user_turn_id)
        self.assertGreater(other.user_turn_id, envelope.user_turn_id)


if __name__ == '__main__':
    unittest.main()