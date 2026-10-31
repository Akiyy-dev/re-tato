import importlib.util
import asyncio
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tatocollector.filters import CaptureFilter


class MemoryStore:
    def __init__(self):
        self.cursor = {'pts': 1, 'qts': 0, 'seq': 0, 'date': 1700000000,
                       'channels': {'9': {'pts': 20, 'access_hash': 123}}}
        self.events = []

    def get_checkpoint(self):
        return self.cursor

    def commit_packet(self, packet_id, events, checkpoint=None):
        self.events.extend(events)
        self.cursor = checkpoint

    def fail_packet(self, *args):
        raise AssertionError('A valid synthetic packet was rejected')


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class CaptureFilterIntegrationTests(unittest.TestCase):
    def make_processor(self, selected):
        from tatocollector.collector import PacketProcessor
        store, manager = MemoryStore(), Mock()
        manager.current.return_value = CaptureFilter(True, frozenset(selected))
        return store, PacketProcessor(store, 7, filters=manager)

    def test_excluded_private_message_still_commits_its_global_progress(self):
        store, processor = self.make_processor({('group', 9)})
        raw = bytes.fromhex('11bf4f91 00000000 01000000 2a000000 02686900 02000000 01000000 00f15365')
        self.assertTrue(processor.process(1, raw))
        self.assertEqual(store.events, [])
        self.assertEqual(store.cursor['pts'], 2)
        self.assertEqual(processor.stats['filter_skipped'], 1)

    def test_mixed_packet_only_captures_selected_namespace_but_advances_all_scopes(self):
        store, processor = self.make_processor({('group', 9)})
        parsed = {'_': 'updates', 'date': 1700000001, 'seq': 1, 'users': [], 'chats': [],
                  'updates': [{'_': 'updateShortChatMessage', 'id': 5, 'chat_id': 9,
                               'from_id': 42, 'message': 'selected', 'date': 1700000001,
                               'pts': 2, 'pts_count': 1},
                              {'_': 'updateNewChannelMessage', 'pts': 21, 'pts_count': 1,
                               'message': {'_': 'message', 'id': 6, 'message': 'excluded',
                                           'to_id': {'_': 'peerChannel', 'channel_id': 9},
                                           'date': 1700000001}}]}
        processor.schema = Mock()
        processor.schema.decode_prefix.return_value = (parsed, 4)
        processor.schema.definitions = {0x74ae4240: SimpleNamespace(result='Updates')}
        self.assertTrue(processor.process(2, struct.pack('<I', 0x74ae4240)))
        self.assertEqual([event['text'] for event in store.events], ['selected'])
        self.assertEqual(store.cursor['pts'], 2)
        self.assertEqual(store.cursor['channels']['9']['pts'], 21)

    def test_channel_difference_uses_same_filter_and_keeps_empty_packet_progress(self):
        store, processor = self.make_processor({('group', 9)})
        processor.schema = Mock()
        processor.schema.decode_prefix.return_value = ({'_': 'updates.channelDifference',
            'final': True, 'pts': 21, 'users': [], 'chats': [], 'other_updates': [],
            'new_messages': [{'_': 'message', 'id': 5, 'message': 'excluded',
                              'to_id': {'_': 'peerChannel', 'channel_id': 9},
                              'date': 1700000001}]}, 4)
        self.assertTrue(processor.process(3, b'TATOCHN1'+struct.pack('<iI', 9, 1)))
        self.assertEqual(store.events, [])
        self.assertEqual(store.cursor['channels']['9']['pts'], 21)

    def test_global_difference_filters_each_message_after_full_decode(self):
        store, processor = self.make_processor({('group', 9)})
        processor.schema = Mock()
        messages = [{'_': 'message', 'id': index, 'from_id': 42, 'message': 'synthetic',
                     'date': 1700000001, 'to_id': peer} for index, peer in enumerate([
                         {'_': 'peerUser', 'user_id': 7}, {'_': 'peerChat', 'chat_id': 9}], 1)]
        processor.schema.decode_prefix.return_value = ({'_': 'updates.difference',
            'users': [], 'chats': [], 'other_updates': [], 'new_messages': messages,
            'new_encrypted_messages': [], 'state': {'pts': 3, 'qts': 0, 'seq': 1, 'date': 1700000001}}, 4)
        self.assertTrue(processor.process(4, struct.pack('<I', 1)))
        self.assertEqual([(e['chat_type'], e['chat_id']) for e in store.events], [('group', 9)])
        self.assertEqual(store.cursor['pts'], 3)


@unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI required')
class PendingDeliveryFilterTests(unittest.TestCase):
    def test_old_non_target_pending_is_held_without_blocking_later_target_or_changing_payload(self):
        from tatocollector.ingest import DeliveryResult
        from tatocollector.outbox import Store
        base = {'account_id': 7, 'message_id': 1, 'chat_type': 'group', 'chat_id': 9,
                'date': 1700000000, 'text': 'frozen synthetic body'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'queue.sqlite3'
            store = Store(path)
            try:
                packet_id = store.put_wire(b'synthetic-mixed-pending')
                store.commit_packet(packet_id, [base, base | {'chat_type': 'channel', 'message_id': 2}])
                channel_only = CaptureFilter(True, frozenset({('channel', 9)}))
                item = store.next_delivery(0, accepts=channel_only.allows)
                self.assertEqual(item[1]['conversation_id'], 'tato:7:channel:9')
                store.record_delivery(item[0], DeliveryResult('accepted', receipt_id=1, status_code=202), 0)
                self.assertIsNone(store.next_delivery(0, accepts=channel_only.allows))
                counts = store.counts(accepts=channel_only.allows)
                self.assertEqual((counts['pending'], counts['filter_held']), (1, 1))
            finally:
                store.close()
            store = Store(path)
            try:
                group_only = CaptureFilter(True, frozenset({('group', 9)}))
                item = store.next_delivery(100, accepts=group_only.allows)
                self.assertEqual(item[1]['text'], base['text'])
                self.assertEqual(item[1]['conversation_id'], 'tato:7:group:9')
                self.assertEqual(item[2], 0)
            finally:
                store.close()


@unittest.skipUnless(sys.platform == 'win32' and importlib.util.find_spec('telethon'), 'Windows and protocol dependencies required')
class DeliveryWorkerFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_only_sends_selected_old_pending_with_existing_frozen_payload(self):
        from tatocollector.collector import delivery_loop
        from tatocollector.filters import FilterManager, save_filter
        from tatocollector.ingest import DeliveryResult, IngestConfig
        from tatocollector.outbox import Store
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            http_path, filter_path = directory / 'http.json', directory / 'filter.json'
            http_path.write_text('{}')
            save_filter(filter_path, CaptureFilter(True, frozenset({('channel', 9)})))
            store = Store(directory / 'queue.sqlite3')
            base = {'account_id': 7, 'message_id': 1, 'chat_type': 'group', 'chat_id': 9,
                    'date': 1700000000, 'text': 'frozen synthetic'}
            store.commit_packet(store.put_wire(b'synthetic-worker-packet'), [base, base | {'chat_type': 'channel', 'message_id': 2}])
            stop, state, sent = asyncio.Event(), {}, []
            loop = asyncio.get_running_loop()

            def fake_deliver(payload, config):
                sent.append(payload)
                loop.call_soon_threadsafe(stop.set)
                return DeliveryResult('accepted', receipt_id=42, status_code=202)

            try:
                with patch('tatocollector.collector.load_ingest_config', return_value=IngestConfig('https://example.invalid/api/ingest/v1/messages', 'tgfi_synthetic_only')), patch('tatocollector.collector.deliver', side_effect=fake_deliver):
                    await asyncio.wait_for(delivery_loop(store, http_path, state, stop, FilterManager(filter_path)), 2)
                self.assertEqual([payload['conversation_id'] for payload in sent], ['tato:7:channel:9'])
                self.assertEqual((store.counts()['delivered'], store.counts()['pending']), (1, 1))
            finally:
                store.close()

    async def test_corrupt_filter_never_sends_queued_message(self):
        from tatocollector.collector import delivery_loop
        from tatocollector.filters import FilterManager
        from tatocollector.ingest import IngestConfig
        from tatocollector.outbox import Store
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            http_path, filter_path = directory / 'http.json', directory / 'filter.json'
            http_path.write_text('{}')
            filter_path.write_text('{broken')
            store = Store(directory / 'queue.sqlite3')
            store.commit_packet(store.put_wire(b'synthetic-corrupt-filter'), [{
                'account_id': 7, 'message_id': 1, 'chat_type': 'group', 'chat_id': 9,
                'date': 1700000000, 'text': 'synthetic'}])
            stop = asyncio.Event()
            asyncio.get_running_loop().call_later(0.03, stop.set)
            try:
                with patch('tatocollector.collector.load_ingest_config', return_value=IngestConfig('https://example.invalid/api/ingest/v1/messages', 'tgfi_synthetic_only')), patch('tatocollector.collector.deliver') as sender:
                    await asyncio.wait_for(delivery_loop(store, http_path, {}, stop, FilterManager(filter_path)), 1)
                sender.assert_not_called()
                self.assertEqual(store.counts()['pending'], 1)
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
