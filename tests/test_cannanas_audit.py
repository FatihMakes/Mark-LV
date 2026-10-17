"""Fail-closed 0640 JSONL audit: written before every POST, secret-free."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from core.cannanas_audit import (
    CannanasAudit,
    CannanasAuditClosed,
    READ_AUDIT_DEGRADED_WARNING,
)


def _audit_dir(tmp):
    directory = tmp / 'audit-state'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _entries(path: Path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


class TestAuditAppends0640(unittest.TestCase):
    def test_append_only_jsonl_0640_in_0700_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = _audit_dir(Path(tmp))
            path = directory / 'audit.jsonl'
            audit = CannanasAudit(path)
            ok = audit.record(
                'prepared', action='plant_create', payload_hash='hash-1',
                preview='Vorschautext', stable_device_id='device-id',
                auth_session_id='digest-1', user_turn_id=4, state='pending',
                result_id='')
            self.assertTrue(ok)
            ok = audit.record(
                'result', action='plant_create', payload_hash='hash-1',
                preview='Vorschautext', stable_device_id='device-id',
                auth_session_id='digest-1', user_turn_id=4, state='succeeded',
                result_id='plant-17')
            self.assertTrue(ok)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            entries = _entries(path)
            self.assertEqual([e['event'] for e in entries], ['prepared', 'result'])
            self.assertEqual(entries[0]['action'], 'plant_create')
            self.assertEqual(entries[0]['user_turn_id'], 4)
            self.assertEqual(entries[1]['result_id'], 'plant-17')
            self.assertEqual(entries[1]['state'], 'succeeded')
            self.assertIn('timestamp', entries[0])
            self.assertEqual(sorted(entries[0]),
                             sorted(['event', 'timestamp', 'device_id', 'auth_session_id',
                                     'user_turn_id', 'action', 'payload_hash', 'preview',
                                     'state', 'result_id']))


class TestAuditSecretFree(unittest.TestCase):
    def test_record_refuses_free_form_kwargs_and_echoes_no_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'audit.jsonl'
            audit = CannanasAudit(Path(path))
            with self.assertRaises(TypeError):
                audit.record('prepared', action='x', payload_hash='h',
                             confirmation_token='opaque-secret-1')  # type: ignore[arg-type]
            file_text = path.read_text() if path.exists() else ''
            self.assertNotIn('opaque-secret-1', file_text)
            entries = _entries(path) if path.exists() else []
            for entry in entries:
                rendered = json.dumps(entry)
                for forbidden in ('bearer ', 'Bearer ', 'token'):
                    self.assertNotIn(forbidden, rendered)


class TestAuditPreCommitBeforePost(unittest.TestCase):
    def test_confirmed_entry_is_durable_before_the_confirm_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = _audit_dir(Path(tmp))
            path = directory / 'audit.jsonl'
            audit = CannanasAudit(path)
            posts = []
            with audit.pre_post(action='plant_create', payload_hash='hash-2',
                                preview='Vorschautext 2', stable_device_id='device-id',
                                auth_session_id='digest-2', user_turn_id=7) as finish:
                # By the time the confirm call may run, the confirmed entry is
                # already durably committed (fsynced) — proven below the yield.
                self.assertTrue(path.exists())
                confirmed = _entries(path)
                self.assertEqual([e['event'] for e in confirmed], ['confirmed'])
                self.assertEqual(confirmed[0]['state'], 'executing')
                self.assertEqual(confirmed[0]['payload_hash'], 'hash-2')
                posts.append('confirmed-on-disk')
                finish('succeeded', 'plant-9')
            entries = _entries(path)
            self.assertEqual([e['event'] for e in entries], ['confirmed', 'result'])
            self.assertEqual(entries[1]['state'], 'succeeded')
            self.assertEqual(entries[1]['result_id'], 'plant-9')
            self.assertEqual(posts, ['confirmed-on-disk'])

    def test_finish_only_allowed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _audit_dir(Path(tmp)) / 'audit.jsonl'
            audit = CannanasAudit(path)
            with audit.pre_post(action='plant_create', payload_hash='h',
                                preview='p', stable_device_id='d',
                                auth_session_id='s', user_turn_id=1) as finish:
                finish('succeeded', 'id-1')
                with self.assertRaises(RuntimeError):
                    finish('succeeded', 'id-2')
            self.assertEqual(len(_entries(path)), 2)


class TestAuditUnavailableFailsWriteClosed(unittest.TestCase):
    def test_unavailable_audit_refuses_write_with_zero_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The audit target exists but cannot be a valid sink: a directory.
            audit = CannanasAudit(Path(tmp))
            posts = []
            with self.assertRaises(CannanasAuditClosed):
                with audit.pre_post(action='plant_create', payload_hash='h',
                                    preview='p', stable_device_id='d',
                                    auth_session_id='s', user_turn_id=1) as finish:
                    posts.append('POST')          # pragma: no cover - never reached
                    finish('succeeded')           # pragma: no cover - never reached
            self.assertEqual(posts, [])

    def test_record_raises_closed_when_target_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = CannanasAudit(Path(tmp))
            with self.assertRaises(CannanasAuditClosed):
                audit.record('prepared', action='x', payload_hash='h', preview='p',
                             stable_device_id='d', auth_session_id='s', user_turn_id=1,
                             state='pending', result_id='')


class TestReadAuditFailureDegradesWithWarning(unittest.TestCase):
    def test_read_audit_failure_is_non_fatal_with_one_bounded_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = CannanasAudit(Path(tmp))   # a directory: record always fails
            warnings = []
            warn_msg = []
            ok = audit.record_read(
                'read', action='overview', payload_hash='',
                stable_device_id='d', auth_session_id='s', user_turn_id=2,
                state='succeeded', warn=lambda: warn_msg.append(
                    READ_AUDIT_DEGRADED_WARNING))
            self.assertFalse(ok)
            self.assertEqual(warn_msg, [READ_AUDIT_DEGRADED_WARNING])
            # Exactly one bounded warning for the same unavailable sink.
            ok = audit.record_read(
                'read', action='find_plants', payload_hash='',
                stable_device_id='d', auth_session_id='s', user_turn_id=3,
                state='succeeded', warn=lambda: warn_msg.append('second'))
            self.assertFalse(ok)
            self.assertEqual(warn_msg, [READ_AUDIT_DEGRADED_WARNING])


class TestSymlinkedAuditRefused(unittest.TestCase):
    def test_symlinked_target_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = _audit_dir(Path(tmp))
            victim = directory / 'victim.jsonl'
            victim.write_text('')
            victim.chmod(0o640)
            path = directory / 'audit.jsonl'
            path.symlink_to(victim)
            audit = CannanasAudit(path)
            with self.assertRaises(CannanasAuditClosed):
                audit.record('prepared', action='x', payload_hash='h', preview='p',
                             stable_device_id='d', auth_session_id='s', user_turn_id=1,
                             state='pending', result_id='')

    def test_mispermissioned_target_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = _audit_dir(Path(tmp))
            path = directory / 'audit.jsonl'
            path.write_text('')
            path.chmod(0o666)
            audit = CannanasAudit(path)
            with self.assertRaises(CannanasAuditClosed):
                audit.record('prepared', action='x', payload_hash='h', preview='p',
                             stable_device_id='d', auth_session_id='s', user_turn_id=1,
                             state='pending', result_id='')


if __name__ == '__main__':
    unittest.main()