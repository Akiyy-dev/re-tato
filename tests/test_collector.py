import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch


class FakeStore:
    def __init__(self):
        self.events = []
        self.failed = []
        self.cursor = {'pts': 1, 'qts': 0, 'seq': 0, 'date': 1700000000, 'channels': {}}

    def get_checkpoint(self):
        return self.cursor

    def commit_packet(self, packet_id, events, checkpoint=None):
        self.events.extend(events)
        if checkpoint is not None:
            self.cursor = checkpoint

    def fail_packet(self, packet_id, reason, constructor=None):
        self.failed.append((packet_id, reason, constructor))


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class CollectorTests(unittest.TestCase):
    def test_real_wire_decode_saves_events_and_cursor_together(self):
        from tatocollector.collector import PacketProcessor
        store = FakeStore()
        processor = PacketProcessor(store, 7)
        raw = bytes.fromhex('11bf4f91 00000000 01000000 2a000000 02686900 02000000 01000000 00f15365')
        self.assertTrue(processor.process(1, raw))
        self.assertEqual(store.events[0]['text'], 'hi')
        self.assertEqual(store.cursor['pts'], 2)
        self.assertNotIn('hi', json.dumps(processor.stats))

    def test_unknown_packet_is_retained_and_no_cursor_or_message_is_invented(self):
        from tatocollector.collector import PacketProcessor
        store = FakeStore()
        processor = PacketProcessor(store, 7)
        self.assertFalse(processor.process(2, struct.pack('<I', 0xcafebabe) + b'private data'))
        self.assertEqual(store.events, [])
        self.assertEqual(store.cursor['pts'], 1)
        self.assertEqual(store.failed, [(2, 'unknown_constructor', 'cafebabe')])
        self.assertNotIn('private data', json.dumps(processor.stats))

    def test_checkpoint_candidates_include_earlier_success_while_durable_cursor_is_pinned(self):
        from tatocollector.collector import PacketProcessor
        store = FakeStore()
        store.commit_packet = Mock()  # An earlier failed packet pins durable cursor.
        processor = PacketProcessor(store, 7)
        processor.schema = Mock()
        processor.schema.decode_prefix.return_value = ({'_': 'messages.dialogs', 'users': [],
            'chats': [{'id': 9, 'access_hash': 123}], 'dialogs': [
                {'peer': {'_': 'peerChannel', 'channel_id': 9}, 'pts': 20}]}, 4)
        self.assertTrue(processor.process(2, struct.pack('<I', 1)))
        processor.schema.decode_prefix.return_value = ({'_': 'updates.differenceEmpty', 'date': 1700000001, 'seq': 1}, 4)
        self.assertTrue(processor.process(3, struct.pack('<I', 2)))
        candidate = store.commit_packet.call_args.args[2]
        self.assertEqual(candidate['channels']['9']['pts'], 20)

    def test_status_file_is_atomic_json_with_no_message_content(self):
        from tatocollector.collector import write_status
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'status.json'
            write_status(path, {'running': False, 'connected': False})
            self.assertFalse(json.loads(path.read_text())['connected'])
            self.assertEqual(len(list(Path(directory).glob('*.tmp'))), 0)


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_local_failure_keeps_connection_available_for_other_chats(self):
        from tatocollector.collector import recover_channel_safely
        connection, processor, state = Mock(), Mock(), {}
        connection.sender.is_connected.return_value = True
        with patch('tatocollector.collector.recover_channel', AsyncMock(side_effect=ValueError('private server text'))):
            delay = await recover_channel_safely(connection, processor, 9, state)
        self.assertGreaterEqual(delay, 1)
        self.assertIn('9', state['channel_errors'])
        self.assertNotIn('private server text', json.dumps(state))


if __name__ == '__main__':
    unittest.main()
