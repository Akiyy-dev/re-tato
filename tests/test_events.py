import unittest

from tatocollector.events import PeerCache, normalize_updates


class EventTests(unittest.TestCase):
    def test_private_short_update_uses_peer_and_real_sender_for_each_direction(self):
        incoming = {'_': 'updateShortMessage', 'id': 5, 'user_id': 42, 'message': 'hello',
                    'date': 1700000000, 'pts': 4, 'pts_count': 1, 'out': False}
        event = normalize_updates(incoming, 7)[0]
        self.assertEqual((event['chat_type'], event['chat_id'], event['sender_id']), ('private', 42, 42))
        outgoing = normalize_updates({**incoming, 'out': True}, 7)[0]
        self.assertEqual(outgoing['sender_id'], 7)

    def test_batch_observes_names_before_normalizing_messages(self):
        parsed = {'_': 'updates', 'users': [{'id': 42, 'first_name': 'Alice'}],
                  'chats': [{'id': 9, 'title': 'Team'}], 'updates': [
                      {'_': 'updateNewMessage', 'pts': 6, 'pts_count': 1, 'message': {
                          '_': 'message', 'id': 5, 'from_id': 42, 'to_id': {'_': 'peerChat', 'chat_id': 9},
                          'date': 1700000000, 'message': 'hi', 'out': False}}]}
        event = normalize_updates(parsed, 7, PeerCache())[0]
        self.assertEqual(event['conversation_name'], 'Team')
        self.assertEqual(event['sender_name'], 'Alice')
        self.assertEqual(event['text'], 'hi')

    def test_full_incoming_private_message_addressed_to_self_uses_sender_as_chat(self):
        update = {'_': 'updateShort', 'update': {'_': 'updateNewMessage', 'message': {
            '_': 'message', 'id': 5, 'from_id': 42, 'to_id': {'_': 'peerUser', 'user_id': 7},
            'date': 1700000000, 'message': 'hi', 'out': False}}}
        self.assertEqual(normalize_updates(update, 7)[0]['chat_id'], 42)

    def test_media_caption_is_preserved_without_inventing_text_for_empty_media(self):
        update = {'_': 'updateNewChannelMessage', 'message': {
            '_': 'message', 'id': 5, 'to_id': {'_': 'peerChannel', 'channel_id': 99},
            'date': 1700000000, 'message': '', 'media': {'_': 'messageMediaPhoto', 'caption': 'caption'}}}
        event = normalize_updates(update, 7)[0]
        self.assertEqual((event['chat_type'], event['chat_id'], event['text']), ('channel', 99, 'caption'))
        update['message']['media'].pop('caption')
        self.assertEqual(normalize_updates(update, 7)[0]['text'], '')

    def test_non_message_update_is_not_reported_as_a_new_message(self):
        self.assertEqual(normalize_updates({'_': 'updateUserStatus', 'user_id': 42}, 7), [])

    def test_nontext_media_keeps_file_metadata_without_binary_thumbnail(self):
        update = {'_': 'updateNewChannelMessage', 'message': {
            '_': 'message', 'id': 6, 'to_id': {'_': 'peerChannel', 'channel_id': 99},
            'date': 1700000000, 'message': '', 'media': {'_': 'messageMediaDocument',
                'document': {'_': 'document', 'id': 12, 'access_hash': 345, 'size': 100,
                    'mime_type': 'application/pdf', 'thumb': {'bytes': b'not JSON'},
                    'attributes': [{'_': 'documentAttributeFilename', 'file_name': 'example.pdf'}]}}}}
        event = normalize_updates(update, 7)[0]
        self.assertEqual(event['text'], '')
        self.assertEqual(event['media']['document']['size'], 100)
        self.assertEqual(event['media']['document']['attributes'][0]['file_name'], 'example.pdf')
        self.assertNotIn('bytes', event['media']['document']['thumb'])


if __name__ == '__main__':
    unittest.main()
