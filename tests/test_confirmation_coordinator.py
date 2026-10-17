"""Host-side confirmation coordinator: atomic consume, fences, bounded grammar."""
import json
import sys
import threading
import time
import unittest
from dataclasses import fields

from core.cannanas_confirmation import (
    PendingConfirmation,
    ConfirmationCoordinator,
    REFUSAL_ALREADY_USED,
    REFUSAL_CANCELLED,
    REFUSAL_EXPIRED,
    REFUSAL_NONE,
    REFUSAL_WRONG_AUTH_SESSION,
    REFUSAL_WRONG_BOOT,
    REFUSAL_WRONG_DEVICE,
    REFUSAL_WRONG_PAYLOAD,
    REFUSAL_WRONG_SESSION,
    REFUSAL_WRONG_TURN,
)
from core.turn_provenance import (
    CHANNEL_PHONE_PCM,
    IngressEnvelope,
    create_stable,
)
from core.user_turn import FinalizedTurn

TTL_5_MIN = 300.0


def make_token(token='opaque-mcp-token-1'):
    from core.cannanas_confirmation import OpaqueToken
    return OpaqueToken(token)


def make_pending(**overrides):
    now = time.monotonic()
    base = dict(
        action='plant_create',
        club_id='club-1',
        payload_hash='payload-hash-1',
        complete_preview='Neue Pflanze „Brain Damage“ als Steckling, Status SEED, Raum Madrid.',
        jarvis_session_id='jarvis-session-1',
        stable_device_id='device-durable-1',
        auth_session_id='auth-digest-1',
        prepared_turn_id=11,
        adapter_boot_id='boot-1',
        created_at=now,
        expires_at=now + TTL_5_MIN,
        state='pending',
        opaque_mcp_token=make_token(),
    )
    base.update(overrides)
    return PendingConfirmation(**base)


def make_turn(user_turn_id=14, *, device='device-durable-1',
              auth='auth-digest-1', boot=None):
    # Pinned turn identity: the id is supplied explicitly (fresh ids come from
    # create_stable at ingress; here the test fixes them).
    envelope = IngressEnvelope(stable_device_id=device,
                               auth_session_id=auth, capture_id='1:1',
                               user_turn_id=user_turn_id,
                               channel=CHANNEL_PHONE_PCM, final=True)
    return FinalizedTurn(envelope=envelope, finalized_at=2000.0,
                         identity=f'{envelope.channel}:{user_turn_id}')


def expected():
    return {'action': 'plant_create', 'club_id': 'club-1',
            'payload_hash': 'payload-hash-1', 'adapter_boot_id': 'boot-1'}


class TestStoreReplacesPreviousPending(unittest.TestCase):
    def test_one_pending_per_session_new_prepare_cancels_previous(self):
        coordinator = ConfirmationCoordinator()
        first = make_pending(prepared_turn_id=11)
        second = make_pending(prepared_turn_id=12, action='batch_create')
        coordinator.store(first)
        coordinator.store(second)
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(user_turn_id=20),
            dict(expected(), action='batch_create'))
        self.assertEqual(refusal, REFUSAL_NONE)
        self.assertEqual(pending.prepared_turn_id, 12)
        self.assertEqual(pending.state, 'executing')
        # The previous pending no longer exists in the coordinator: a store
        # replaced it (new prepare cancels the old one).
        self.assertEqual(coordinator.current('jarvis-session-1').prepared_turn_id, 12)


class TestConsumeAtomicExactlyOneExecutor(unittest.TestCase):
    def test_two_threads_exactly_one_wins_one_typed_refusal(self):
        coordinator = ConfirmationCoordinator()
        coordinator.store(make_pending())
        outcomes = []
        barrier = threading.Barrier(2)

        def racing():
            barrier.wait()
            outcomes.append(coordinator.consume(
                'jarvis-session-1', make_turn(), expected()))

        threads = [threading.Thread(target=racing) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wins = [(p, r) for p, r in outcomes if r == REFUSAL_NONE]
        losses = [(p, r) for p, r in outcomes if r != REFUSAL_NONE]
        self.assertEqual(len(wins), 1)
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0][1], REFUSAL_ALREADY_USED)


class TestConsumeRejectsWrongSessionDeviceTurnBoot(unittest.TestCase):
    def setUp(self):
        self.coordinator = ConfirmationCoordinator()
        self.coordinator.store(make_pending())

    def check(self, session, turn, exp, refusal):
        pending, got = self.coordinator.consume(session, turn, exp)
        self.assertIsNone(pending)
        self.assertEqual(got, refusal)
        # A refused consume never consumes the pending.
        pending, got = self.coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertEqual(got, REFUSAL_NONE)

    def test_wrong_session(self):
        self.check('other-session', make_turn(), expected(), REFUSAL_WRONG_SESSION)

    def test_wrong_device(self):
        self.check('jarvis-session-1',
                   make_turn(device='other-device'), expected(), REFUSAL_WRONG_DEVICE)

    def test_wrong_auth_session(self):
        self.check('jarvis-session-1',
                   make_turn(auth='other-digest'), expected(), REFUSAL_WRONG_AUTH_SESSION)

    def test_same_turn_as_prepare_is_refused(self):
        self.coordinator2 = ConfirmationCoordinator()
        pending = make_pending(prepared_turn_id=11)
        self.coordinator2.store(pending)
        same = make_turn(user_turn_id=11)
        pending_out, refusal = self.coordinator2.consume('jarvis-session-1', same, expected())
        self.assertIsNone(pending_out)
        self.assertEqual(refusal, REFUSAL_WRONG_TURN)

    def test_older_turn_is_refused(self):
        pending, refusal = self.coordinator.consume(
            'jarvis-session-1', make_turn(user_turn_id=5), expected())
        self.assertIsNone(pending)
        self.assertEqual(refusal, REFUSAL_WRONG_TURN)

    def test_wrong_boot(self):
        bad = dict(expected(), adapter_boot_id='boot-2')
        self.check('jarvis-session-1', make_turn(), bad, REFUSAL_WRONG_BOOT)

    def test_wrong_payload(self):
        bad = dict(expected(), payload_hash='other-hash')
        self.check('jarvis-session-1', make_turn(), bad, REFUSAL_WRONG_PAYLOAD)

    def test_wrong_action(self):
        bad = dict(expected(), action='confirm_batch_create')
        self.check('jarvis-session-1', make_turn(), bad, REFUSAL_WRONG_PAYLOAD)


class TestConsumeRejectsExpiredAndReplay(unittest.TestCase):
    def test_expired_is_refused_and_lazily_cleared(self):
        coordinator = ConfirmationCoordinator(clock=lambda: 3000.0)
        # Pending pinned to the coordinator's overridden clock: expires at 2200.
        coordinator.store(make_pending(created_at=2000.0, expires_at=2200.0))
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertIsNone(pending)
        self.assertEqual(refusal, REFUSAL_EXPIRED)
        self.assertEqual(coordinator.current('jarvis-session-1').state, 'expired')

    def test_replay_after_consume_is_refused(self):
        coordinator = ConfirmationCoordinator()
        coordinator.store(make_pending())
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertEqual(refusal, REFUSAL_NONE)
        coordinator.mark_consumed('jarvis-session-1')
        # The consumed record stays so a replay is refused as already_used.
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertIsNone(pending)
        self.assertEqual(refusal, REFUSAL_ALREADY_USED)


class TestCancelClearsPending(unittest.TestCase):
    def test_cancel_makes_the_pending_unconsumable(self):
        coordinator = ConfirmationCoordinator()
        coordinator.store(make_pending())
        self.assertTrue(coordinator.cancel('jarvis-session-1'))
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertIsNone(pending)
        self.assertEqual(refusal, REFUSAL_CANCELLED)


class TestTokenNeverVisible(unittest.TestCase):
    def test_opaque_token_not_in_repr_str_or_serialization(self):
        pending = make_pending(opaque_mcp_token=make_token('token-never-see-1'))
        rendered = repr(pending) + str(pending)
        self.assertNotIn('token-never-see-1', rendered)
        self.assertIn('<redacted>', rendered)
        # json.dumps of the dataclass must fail closed rather than leak.
        with self.assertRaises((TypeError, ValueError)):
            json.dumps(pending)


class TestRestartInvalidatesAll(unittest.TestCase):
    def test_adapter_restart_invalidates_every_pending(self):
        coordinator = ConfirmationCoordinator()
        coordinator.store(make_pending())
        coordinator.invalidate_all_on_adapter_restart()
        pending, refusal = coordinator.consume(
            'jarvis-session-1', make_turn(), expected())
        self.assertIsNone(pending)
        self.assertEqual(refusal, REFUSAL_CANCELLED)


class TestPendingRecordShape(unittest.TestCase):
    def test_owner_record_has_exactly_the_design_fields_and_ttl_cap(self):
        names = {f.name for f in fields(PendingConfirmation)}
        self.assertEqual(names, {'action', 'club_id', 'payload_hash', 'complete_preview',
                                 'jarvis_session_id', 'stable_device_id', 'auth_session_id',
                                 'prepared_turn_id', 'adapter_boot_id', 'created_at',
                                 'expires_at', 'state', 'opaque_mcp_token'})
        pending = make_pending()
        self.assertLessEqual(pending.expires_at - pending.created_at, 600.0)
        self.assertEqual(pending.expires_at - pending.created_at, TTL_5_MIN)


class TestGrammarBounded(unittest.TestCase):
    def test_exact_affirmation_forms(self):
        from core.cannanas_host import confirmation_grammar
        for text in ('ja', 'JA', 'ja.', 'ja!', 'bestätigen', 'Bestätigen.',
                     'bitte bestätigen', 'ok', 'okay', 'ja bitte'):
            self.assertEqual(confirmation_grammar(text), 'affirm', text)

    def test_exact_deny_forms(self):
        from core.cannanas_host import confirmation_grammar
        for text in ('nein', 'Nein.', 'abbrechen', 'Abbrechen!', 'brich ab',
                     'stop', 'stopp'):
            self.assertEqual(confirmation_grammar(text), 'deny', text)

    def test_everything_else_is_none_no_fuzzy_matching(self):
        from core.cannanas_host import confirmation_grammar
        for text in ('jaa', 'yes', 'kannst du das bestätigen', 'ja gerne',
                     'bestätige die pflanze', 'nicht speichern', '',
                     None, 'vielleicht', 'breche die pflanze ab', 'abbruch bitte',
                     'mach den rest'):
            self.assertEqual(confirmation_grammar(text), 'none', text)


class TestAdapterBootIdPerProcess(unittest.TestCase):
    def test_boot_id_is_a_fresh_process_identity(self):
        import subprocess
        code = (
            'import sys; sys.path.insert(0, "/root/Mark-LV-LaCasa/.worktrees/'
            'cannanas-integration");'
            'from core.cannanas_host import adapter_boot_id; print(adapter_boot_id())')
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        first = out.stdout.strip()
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        second = out.stdout.strip()
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)


if __name__ == '__main__':
    unittest.main()