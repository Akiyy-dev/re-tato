import struct
import unittest

from tatocollector.sync import (advance, channel_events, channel_request, dialogs_cursor,
                                dialogs_request, difference_events, difference_request, merge_cursors)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.cursor = {'pts': 10, 'qts': 0, 'date': 1700000000, 'seq': 3, 'channels': {}}

    def test_gap_never_advances_cursor_past_missing_global_message(self):
        packet = {'_': 'updateShortMessage', 'pts': 12, 'pts_count': 1, 'date': 1700000001}
        result, gaps = advance(self.cursor, packet)
        self.assertEqual(result, self.cursor)
        self.assertEqual(gaps, {'global'})
        packet['pts'] = 11
        result, gaps = advance(self.cursor, packet)
        self.assertEqual((result['pts'], result['date'], gaps), (11, 1700000001, set()))
        self.assertEqual(self.cursor['pts'], 10)

    def test_duplicate_does_not_move_backwards_and_channel_pts_is_separate(self):
        self.cursor['channels'] = {'9': {'pts': 20, 'access_hash': 123}}
        packet = {'_': 'updates', 'date': 1700000001, 'seq': 4, 'updates': [
            {'_': 'updateReadHistoryInbox', 'pts': 10, 'pts_count': 1},
            {'_': 'updateNewChannelMessage', 'pts': 21, 'pts_count': 1,
             'message': {'to_id': {'_': 'peerChannel', 'channel_id': 9}}}]}
        result, gaps = advance(self.cursor, packet)
        self.assertEqual(result['pts'], 10)
        self.assertEqual(result['channels']['9']['pts'], 21)
        self.assertEqual(gaps, set())

    def test_seq_and_too_long_trigger_recovery(self):
        self.assertEqual(advance(self.cursor, {'_': 'updatesTooLong'})[1], {'global'})
        result, gaps = advance(self.cursor, {'_': 'updates', 'seq': 5, 'date': 1700000002, 'updates': []})
        self.assertEqual(result, self.cursor)
        self.assertEqual(gaps, {'global'})
        self.assertEqual(advance(self.cursor, {'_': 'updateShort', 'date': 1, 'update': {
            '_': 'updateChannelTooLong', 'channel_id': 9}})[1], {'channel:9'})

    def test_global_difference_keeps_each_real_message_and_metadata(self):
        parsed = {'_': 'updates.difference', 'users': [], 'chats': [], 'other_updates': [],
                  'new_messages': [{'_': 'message', 'id': 5, 'from_id': 42,
                                    'to_id': {'_': 'peerUser', 'user_id': 7},
                                    'date': 1700000000, 'message': '真实文字'}],
                  'new_encrypted_messages': [], 'state': {'pts': 11, 'qts': 0, 'seq': 4, 'date': 1700000001}}
        events, cursor, more = difference_events(parsed, self.cursor, 7)
        self.assertEqual(events[0]['text'], '真实文字')
        self.assertEqual(events[0]['chat_id'], 42)
        self.assertEqual(cursor['pts'], 11)
        self.assertFalse(more)
        parsed['_'] = 'updates.differenceSlice'
        parsed['intermediate_state'] = parsed.pop('state')
        self.assertTrue(difference_events(parsed, self.cursor, 7)[2])

    def test_empty_preserves_pts_and_too_long_is_not_silently_skipped(self):
        events, cursor, more = difference_events({'_': 'updates.differenceEmpty', 'date': 1700000010, 'seq': 4}, self.cursor, 7)
        self.assertEqual((events, cursor['pts'], cursor['seq'], more), ([], 10, 4, False))
        with self.assertRaisesRegex(ValueError, 'difference_too_long'):
            difference_events({'_': 'updates.differenceTooLong', 'pts': 1000}, self.cursor, 7)

    def test_exact_global_request_uses_saved_pts_date_and_qts(self):
        self.assertEqual(difference_request(self.cursor), struct.pack('<Iiiii', 0x25939651, 0, 10, 1700000000, 0))

    def test_channel_difference_is_scoped_and_exact_request_matches_recovered_widths(self):
        self.cursor['channels'] = {'9': {'pts': 20, 'access_hash': 123}}
        raw = channel_request(9, self.cursor['channels']['9'])
        self.assertEqual(raw, struct.pack('<II Iiq Iii', 0x03173d78, 0, 0xafeb712e, 9, 123, 0x94d42ee7, 20, 100))
        parsed = {'_': 'updates.channelDifference', 'final': True, 'pts': 21,
                  'new_messages': [], 'other_updates': [], 'users': [], 'chats': []}
        events, cursor, more = channel_events(parsed, self.cursor, 9, 7)
        self.assertEqual((events, cursor['pts'], cursor['channels']['9']['pts'], more), ([], 10, 21, False))
        parsed['_'] = 'updates.channelDifferenceTooLong'
        parsed['messages'] = []
        with self.assertRaisesRegex(ValueError, 'channel_difference_too_long'):
            channel_events(parsed, self.cursor, 9, 7)

    def test_dialog_baseline_adds_channels_but_never_skips_an_existing_offline_cursor(self):
        self.cursor['channels'] = {'9': {'pts': 20, 'access_hash': 123}}
        parsed = {'dialogs': [{'peer': {'_': 'peerChannel', 'channel_id': 9}, 'pts': 30},
                              {'peer': {'_': 'peerChannel', 'channel_id': 10}, 'pts': 40}],
                  'chats': [{'id': 9, 'access_hash': 456}, {'id': 10, 'access_hash': 789}]}
        cursor = dialogs_cursor(parsed, self.cursor)
        self.assertEqual(cursor['channels']['9'], {'pts': 20, 'access_hash': 456})
        self.assertEqual(cursor['channels']['10'], {'pts': 40, 'access_hash': 789})
        self.assertEqual(dialogs_request(), struct.pack('<IIiiIii', 0xa0ee3b73, 0, 0, 0, 0x7f3b18ea, 100, 0))

    def test_merging_candidates_never_loses_channel_or_rolls_back_any_progress(self):
        old = {**self.cursor, 'channels': {'9': {'pts': 20, 'access_hash': 123}}}
        new = {**self.cursor, 'pts': 12, 'date': 1699999999,
               'channels': {'9': {'pts': 19, 'access_hash': 456}, '10': {'pts': 5, 'access_hash': 789}}}
        merged = merge_cursors(old, new)
        self.assertEqual((merged['pts'], merged['date']), (12, 1700000000))
        self.assertEqual(merged['channels']['9'], {'pts': 20, 'access_hash': 456})
        self.assertIn('10', merged['channels'])
        self.assertEqual(old['channels']['9']['access_hash'], 123)

    def test_new_channel_after_initial_baseline_never_adopts_latest_pts_as_if_gap_were_filled(self):
        cursor = self.cursor | {'initial_dialogs_done': True}
        parsed = {'dialogs': [{'peer': {'_': 'peerChannel', 'channel_id': 9}, 'pts': 100}],
                  'chats': [{'id': 9, 'access_hash': 123}]}
        result = dialogs_cursor(parsed, cursor)
        self.assertEqual(result['channels']['9']['pts'], 1)


if __name__ == '__main__':
    unittest.main()
