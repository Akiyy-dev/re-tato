from contextlib import closing
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from tatocollector.ingest import DeliveryResult


def event(message_id=7, text='synthetic-outbox-text-合成正文'):
    return {'account_id': 123, 'chat_type': 'group', 'chat_id': -456,
            'message_id': message_id, 'date': 0, 'text': text,
            'sender_name': 'synthetic-sender', 'sender_id': 99,
            'conversation_name': 'synthetic-conversation'}


@unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tatocollector.outbox'),
                             'The encrypted persistent outbox is not implemented')
        self.module = importlib.import_module('tatocollector.outbox')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'queue.sqlite'
        self.store = self.module.Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def reopen(self):
        self.store.close()
        self.store = self.module.Store(self.path)

    def commit(self, events, raw=b'synthetic-wire', checkpoint=None):
        packet_id = self.store.put_wire(raw)
        self.store.commit_packet(packet_id, events, checkpoint)
        return packet_id

    def test_raw_is_committed_deduplicated_and_recoverable_after_reopen(self):
        raw = b'synthetic-wire-body\x00\xff'
        packet_id = self.store.put_wire(raw)
        self.assertEqual(self.store.put_wire(raw), packet_id)
        self.reopen()
        self.assertEqual(self.store.pending_wires(), [(packet_id, raw)])
        self.assertEqual(self.store.counts()['undecoded'], 1)
        with closing(sqlite3.connect(self.path)) as check:
            self.assertEqual(check.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_raw_limit_and_encryption_failure_leave_old_queue_intact(self):
        old = self.store.put_wire(b'old-wire')
        with patch('tatocollector.outbox._dpapi',
                   side_effect=AssertionError('Oversized input reached encryption')):
            with self.assertRaises(ValueError):
                self.store.put_wire(b'x' * (1024 * 1024 + 1))
        with patch('tatocollector.outbox._dpapi', side_effect=OSError('synthetic DPAPI failure')):
            with self.assertRaises(OSError):
                self.store.put_wire(b'new-wire')
        self.assertEqual(self.store.pending_wires(), [(old, b'old-wire')])
        largest = b'x' * (1024 * 1024)
        maximum = self.store.put_wire(largest)
        self.assertTrue((maximum, largest) in self.store.pending_wires(),
                        'Maximum-sized wire did not round-trip')

    def test_packet_commit_is_atomic_for_events_checkpoint_and_wire_status(self):
        self.store.set_initial_checkpoint({'pts': 0})
        packet_id = self.store.put_wire(b'packet-with-invalid-event')
        invalid_identity = event(8)
        invalid_identity['message_id'] = 0
        with self.assertRaises(ValueError):
            self.store.commit_packet(packet_id, [event(), invalid_identity],
                                     {'pts': 2})
        self.assertEqual(self.store.counts()['captured'], 0)
        self.assertEqual(self.store.counts()['pending'], 0)
        self.assertEqual(self.store.get_checkpoint(), {'pts': 0})
        self.assertEqual(self.store.pending_wires(),
                         [(packet_id, b'packet-with-invalid-event')])

    def test_payload_validation_failure_preserves_rejected_event_and_other_valid_messages(self):
        rejected = []
        for identifier, field, value in (
            (8, 'sender_name', 's' * 257),
            (9, 'conversation_name', 'c' * 257),
            (10, 'text', 't' * 65537),
            (11, 'date', 'invalid timestamp type'),
        ):
            invalid_payload = event(identifier)
            invalid_payload[field] = value
            rejected.append(invalid_payload)
        packet_id = self.store.put_wire(b'valid-plus-payload-rejections')
        try:
            self.store.commit_packet(packet_id, [event()] + rejected, {'pts': 5})
        except ValueError:
            self.fail('A payload validation rejection must not roll back other captured events')
        counts = self.store.counts()
        self.assertEqual((counts['captured'], counts['pending'], counts['rejected']), (5, 1, 4))
        self.assertEqual(self.store.get_checkpoint(), {'pts': 5})
        self.assertEqual(self.store.pending_wires(), [])
        self.assertEqual(self.store.next_delivery(0)[0], 'tato:123:group:-456:7')
        with closing(sqlite3.connect(self.path)) as check:
            rows = check.execute('''
                SELECT protected_event,last_result FROM events WHERE state='rejected' ORDER BY id
            ''').fetchall()
        for expected, (protected_event, protected_result) in zip(rejected, rows):
            restored = json.loads(self.module._dpapi(protected_event, decrypt=True))
            self.assertTrue(restored == expected, 'Rejected original event was changed or lost')
            result = json.loads(self.module._dpapi(protected_result, decrypt=True))
            self.assertEqual(result['kind'], 'rejected')
            self.assertEqual(result['reason'], 'payload_validation_error')

    def test_mid_commit_encryption_failure_rolls_back_without_losing_older_events(self):
        self.commit([event(1)], raw=b'older-processed-wire')
        packet_id = self.store.put_wire(b'packet-to-rollback')
        actual_dpapi = self.module._dpapi

        def protect(data, *, decrypt=False):
            if not decrypt and b'fail-this-event' in data:
                raise OSError('synthetic DPAPI failure')
            return actual_dpapi(data, decrypt=decrypt)

        with patch('tatocollector.outbox._dpapi', side_effect=protect):
            with self.assertRaises(OSError):
                self.store.commit_packet(packet_id, [event(7), event(8, 'fail-this-event')],
                                         {'pts': 3})
        self.assertEqual(self.store.counts()['captured'], 1)
        self.assertEqual(self.store.counts()['pending'], 1)
        self.assertEqual(self.store.pending_wires(), [(packet_id, b'packet-to-rollback')])

    def test_event_dedup_never_replaces_original_payload_with_changed_metadata(self):
        self.commit([event()])
        original = self.store.next_delivery(0)
        changed = event(text='different content must not replace original')
        changed.update(sender_name='changed', conversation_name='changed', date=86400)
        self.commit([changed], raw=b'replayed-event-in-another-wire')
        self.assertEqual(self.store.next_delivery(0), original)
        self.assertEqual(self.store.counts()['captured'], 1)
        self.assertEqual(self.store.pending_wires(), [])

    def test_nontext_events_are_captured_without_entering_delivery_queue(self):
        self.commit([event(1, None), event(2, ' \r\n\t')])
        counts = self.store.counts()
        self.assertEqual((counts['captured'], counts['no_text'], counts['pending']), (2, 2, 0))
        self.assertIsNone(self.store.next_delivery(10 ** 9))

    def test_failed_packets_remain_available_for_later_redecode(self):
        packet_id = self.store.put_wire(b'undecoded-wire-for-later')
        self.store.fail_packet(packet_id, 'unsupported_constructor', '0x11223344')
        self.reopen()
        self.assertEqual(self.store.pending_wires(), [(packet_id, b'undecoded-wire-for-later')])
        self.assertEqual(self.store.counts()['undecoded'], 1)
        self.store.commit_packet(packet_id, [event()], {'pts': 1})
        self.assertEqual(self.store.counts()['undecoded'], 0)
        self.assertEqual(self.store.counts()['captured'], 1)

    def test_pending_wire_pagination_reaches_new_work_after_many_failed_packets(self):
        expected = []
        for index in range(5):
            raw = f'failed-wire-{index}'.encode()
            packet_id = self.store.put_wire(raw)
            self.store.fail_packet(packet_id, 'unknown_constructor', None)
            expected.append((packet_id, raw))
        raw = b'fresh-pending-wire'
        packet_id = self.store.put_wire(raw)
        expected.append((packet_id, raw))
        actual = []
        after_id = 0
        while True:
            try:
                page = self.store.pending_wires(limit=2, after_id=after_id)
            except TypeError:
                self.fail('Pending raw wires need bounded ID pagination')
            self.assertLessEqual(len(page), 2)
            if not page:
                break
            actual.extend(page)
            after_id = page[-1][0]
        self.assertEqual(actual, expected)
        self.assertEqual(self.store.pending_wires(limit=1), expected[:1])

    def test_checkpoint_cannot_jump_over_failed_packet_and_promotes_after_gap_closes(self):
        self.store.set_initial_checkpoint({'pts': 0})
        first = self.store.put_wire(b'first-wire')
        second = self.store.put_wire(b'second-wire')
        self.store.fail_packet(first, 'unknown_constructor', None)
        self.store.commit_packet(second, [event(2)], {'pts': 2})
        self.assertEqual(self.store.get_checkpoint(), {'pts': 0})
        self.assertEqual(self.store.counts()['captured'], 1)
        self.store.commit_packet(first, [event(1)], {'pts': 1})
        self.assertEqual(self.store.get_checkpoint(), {'pts': 2})
        self.reopen()
        self.assertEqual(self.store.get_checkpoint(), {'pts': 2})

    def test_checkpoint_merger_folds_old_processed_snapshots_after_restart(self):
        self.store.set_initial_checkpoint({'pts': 10, 'channels': {}})
        gap = self.store.put_wire(b'failed-prefix-before-old-snapshots')
        self.store.fail_packet(gap, 'unknown_constructor', None)
        first = self.store.put_wire(b'old-first-dialog-page')
        self.store.commit_packet(first, [], {'pts': 12, 'channels': {'9': {'pts': 20}}})
        second = self.store.put_wire(b'old-second-dialog-page')
        self.store.commit_packet(second, [], {'pts': 11, 'channels': {'10': {'pts': 30}}})
        self.assertEqual(self.store.get_checkpoint(), {'pts': 10, 'channels': {}})
        calls = []

        def merge(left, right):
            calls.append((deepcopy(left), deepcopy(right)))
            channels = deepcopy(left.get('channels', {}))
            for cid, entry in right.get('channels', {}).items():
                channels[cid] = {'pts': max(channels.get(cid, {}).get('pts', 0), entry['pts'])}
            return {'pts': max(left.get('pts', 0), right.get('pts', 0)), 'channels': channels}

        self.store.close()
        try:
            self.store = self.module.Store(self.path, checkpoint_merger=merge)
        except TypeError:
            self.fail('Checkpoint promotion needs an optional merger for already processed snapshots')
        self.store.commit_packet(gap, [], {'pts': 11, 'channels': {}})
        expected = {'pts': 12, 'channels': {'9': {'pts': 20}, '10': {'pts': 30}}}
        self.assertEqual(self.store.get_checkpoint(), expected)
        self.assertEqual(len(calls), 3)
        self.reopen()
        self.assertEqual(self.store.get_checkpoint(), expected)
        self.store.close()
        self.store = self.module.Store(self.path, checkpoint_merger=merge)
        calls.clear()
        later = self.store.put_wire(b'new-stale-component-snapshot')
        self.store.commit_packet(later, [], {'pts': 11, 'channels': {'9': {'pts': 10}}})
        self.assertEqual(self.store.get_checkpoint(), expected)
        self.assertEqual(len(calls), 1, 'Previously promoted candidates were folded again')

    def test_checkpoint_merger_starts_from_empty_object_without_initial_checkpoint(self):
        initial_values = []

        def merge(left, right):
            initial_values.append(deepcopy(left))
            return {**left, **right}

        self.store.close()
        try:
            self.store = self.module.Store(self.path, checkpoint_merger=merge)
        except TypeError:
            self.fail('Checkpoint merger option is not implemented')
        self.commit([], checkpoint={'pts': 1})
        self.assertEqual(initial_values, [{}])
        self.assertEqual(self.store.get_checkpoint(), {'pts': 1})

    def test_checkpoint_merger_failure_rolls_back_packet_events_and_cursor(self):
        self.store.set_initial_checkpoint({'pts': 1})

        def reject_merge(left, right):
            raise ValueError('synthetic merger failure')

        self.store.close()
        try:
            self.store = self.module.Store(self.path, checkpoint_merger=reject_merge)
        except TypeError:
            self.fail('Checkpoint merger option is not implemented')
        packet_id = self.store.put_wire(b'packet-with-failed-cursor-merge')
        with self.assertRaises(ValueError):
            self.store.commit_packet(packet_id, [event()], {'pts': 2})
        self.assertEqual(self.store.get_checkpoint(), {'pts': 1})
        self.assertEqual(self.store.counts()['captured'], 0)
        self.assertEqual(self.store.pending_wires(), [(packet_id, b'packet-with-failed-cursor-merge')])
        self.reopen()
        self.assertEqual(self.store.get_checkpoint(), {'pts': 1})

    def test_initial_checkpoint_cannot_skip_an_already_buffered_packet(self):
        self.store.put_wire(b'not-yet-processed')
        with self.assertRaises(ValueError):
            self.store.set_initial_checkpoint({'pts': 999})
        self.assertIsNone(self.store.get_checkpoint())

    def test_next_delivery_is_due_only_and_returns_a_fresh_frozen_payload(self):
        self.commit([event()])
        key, payload, attempts = self.store.next_delivery(0)
        self.assertEqual(key, 'tato:123:group:-456:7')
        self.assertEqual(payload['message_id'], 'tato:123:7')
        self.assertEqual(payload['conversation_id'], 'tato:123:group:-456')
        self.assertEqual(attempts, 0)
        payload['text'] = 'caller mutation'
        self.assertEqual(self.store.next_delivery(0)[1]['text'], event()['text'])

    def test_accepted_receipts_are_persisted_and_never_requeued(self):
        for identifier, status in ((1, 202), (2, 200)):
            self.commit([event(identifier)], raw=f'packet-{identifier}'.encode())
            key, _, _ = self.store.next_delivery(0)
            self.store.record_delivery(key, DeliveryResult(
                'accepted', receipt_id=40 + identifier, status_code=status, reason='queued'), 0)
            # A stale result cannot revert a terminal delivery.
            self.store.record_delivery(key, DeliveryResult('retry', reason='network_error'), 1)
        self.reopen()
        self.assertIsNone(self.store.next_delivery(10 ** 9))
        self.assertEqual(self.store.counts()['delivered'], 2)
        with closing(sqlite3.connect(self.path)) as check:
            receipts = check.execute('SELECT receipt_id FROM events ORDER BY receipt_id').fetchall()
        self.assertEqual(receipts, [(41,), (42,)])

    def test_retries_use_deterministic_exponential_backoff_capped_at_300_seconds(self):
        self.commit([event()])
        now = 0.0
        for index, delay in enumerate((1, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300)):
            key, _, attempts = self.store.next_delivery(now)
            self.assertEqual(attempts, index)
            self.store.record_delivery(key, DeliveryResult('retry', reason='server_error'), now)
            self.assertIsNone(self.store.next_delivery(now + delay - .001))
            now += delay
            self.assertEqual(self.store.next_delivery(now)[2], index + 1)

    def test_retry_after_is_a_floor_and_later_ready_events_can_still_be_delivered(self):
        self.commit([event(1), event(2)])
        key, _, _ = self.store.next_delivery(100)
        self.store.record_delivery(key, DeliveryResult(
            'retry', status_code=429, retry_after=600, reason='rate_limited'), 100)
        other = self.store.next_delivery(100)
        self.assertTrue(other[0].endswith(':2'))
        self.store.record_delivery(other[0], DeliveryResult(
            'accepted', receipt_id=42, status_code=202, reason='queued'), 100)
        self.assertIsNone(self.store.next_delivery(699.999))
        self.assertEqual(self.store.next_delivery(700)[0], key)

    def test_auth_error_pauses_all_pending_delivery_until_explicit_retry(self):
        self.commit([event(1), event(2)])
        key = self.store.next_delivery(0)[0]
        self.store.record_delivery(key, DeliveryResult(
            'auth_error', status_code=401, reason='unauthorized'), 0)
        self.assertIsNone(self.store.next_delivery(10 ** 9))
        self.assertEqual(self.store.counts()['auth_error'], 1)
        self.reopen()
        self.assertIsNone(self.store.next_delivery(10 ** 9))
        self.store.retry_auth_errors()
        self.assertEqual(self.store.next_delivery(0)[0], key)
        self.assertEqual(self.store.next_delivery(0)[2], 1)
        self.assertEqual(self.store.counts()['pending'], 2)

    def test_permanent_rejections_remain_stored_and_are_not_retried(self):
        self.commit([event()])
        key = self.store.next_delivery(0)[0]
        self.store.record_delivery(key, DeliveryResult(
            'rejected', status_code=409, reason='conflict'), 0)
        self.reopen()
        counts = self.store.counts()
        self.assertEqual((counts['captured'], counts['rejected'], counts['pending']), (1, 1, 0))
        self.assertIsNone(self.store.next_delivery(10 ** 9))

    def test_business_json_checkpoint_raw_and_failure_details_are_not_plaintext_on_disk(self):
        raw = b'synthetic-private-wire-marker'
        checkpoint = {'pts': 1, 'marker': 'synthetic-private-checkpoint-marker'}
        self.commit([event()], raw=raw, checkpoint=checkpoint)
        failed = self.store.put_wire(b'another-wire')
        self.store.fail_packet(failed, 'synthetic-private-failure-marker', '0x11223344')
        protected_values = (raw, event()['text'].encode(), event()['sender_name'].encode(),
                            event()['conversation_name'].encode(), checkpoint['marker'].encode(),
                            b'synthetic-private-failure-marker')
        for database_file in self.path.parent.glob('queue.sqlite*'):
            contents = database_file.read_bytes()
            for sensitive in protected_values:
                self.assertFalse(sensitive in contents, 'Database exposes plaintext business data')

    def test_corrupt_encrypted_wire_is_rejected_without_deleting_the_record(self):
        self.store.put_wire(b'wire-to-corrupt')
        with closing(sqlite3.connect(self.path)) as change:
            change.execute('UPDATE wires SET protected = ?', (b'not-dpapi',))
            change.commit()
        with self.assertRaises(ValueError):
            self.store.pending_wires()
        self.assertEqual(self.store.counts()['undecoded'], 1)

    def test_invalid_delivery_results_and_unknown_packet_ids_do_not_change_existing_work(self):
        self.commit([event()])
        key = self.store.next_delivery(0)[0]
        for result in (
            DeliveryResult('accepted', receipt_id=None, status_code=202),
            DeliveryResult('accepted', receipt_id=42, status_code=201),
            DeliveryResult('retry', retry_after=float('inf')),
        ):
            with self.assertRaises(ValueError):
                self.store.record_delivery(key, result, 0)
        with self.assertRaises(ValueError):
            self.store.commit_packet(999999, [event(8)], {'pts': 2})
        with self.assertRaises(ValueError):
            self.store.fail_packet(999999, 'parse_error', None)
        self.assertEqual(self.store.next_delivery(0)[2], 0)
        self.assertEqual(self.store.counts()['captured'], 1)


if __name__ == '__main__':
    unittest.main()
