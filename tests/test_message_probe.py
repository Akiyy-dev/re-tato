"""Hand-encoded, synthetic TL fixtures; no sessions or network access."""

import importlib
import importlib.util
import struct
import unittest


# Constructor, flags, message id, peer id, TL string "Hi", pts/count/date.
PRIVATE = bytes.fromhex(
    '11bf4f91 00000000 07000000 2a000000 '
    '02486900 0a000000 01000000 e8030000'
)
# All four zero-byte bool flags, id=8, from=17, chat=99, UTF-8 "你好".
GROUP = bytes.fromhex(
    '88268116 32200000 08000000 11000000 63000000 '
    '06e4bda0e5a5bd00 0b000000 01000000 e9030000'
)
STATE = bytes.fromhex('3e2a6ca5 0a000000 14000000 1e000000 28000000 32000000')
CONTACTS = bytes.fromhex('90587617 07000000 e8030000 02000000')


class MessageProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec('tatocollector.message_probe'),
            'The short-message diagnostic decoder is not implemented',
        )
        self.probe = importlib.import_module('tatocollector.message_probe')

    def test_incoming_private_message_has_peer_as_sender(self):
        self.assertEqual(self.probe.decode_short_update(PRIVATE), {
            'chat_type': 'private', 'chat_id': 42, 'sender_id': 42,
            'message_id': 7, 'text': 'Hi', 'pts': 10, 'pts_count': 1,
            'date': 1000, 'out': False,
        })

    def test_outgoing_private_message_does_not_invent_own_sender_id(self):
        wire = PRIVATE[:4] + b'\x02\0\0\0' + PRIVATE[8:]
        result = self.probe.decode_short_update(wire)
        self.assertEqual((result['chat_id'], result['sender_id'], result['out']),
                         (42, None, True))

    def test_group_keeps_chat_and_positive_sender_separate(self):
        self.assertEqual(self.probe.decode_short_update(GROUP), {
            'chat_type': 'group', 'chat_id': 99, 'sender_id': 17,
            'message_id': 8, 'text': '你好', 'pts': 11, 'pts_count': 1,
            'date': 1001, 'out': True,
        })

    def test_supported_bool_flags_add_no_wire_fields(self):
        for flags, out in [(0x10, False), (0x20, False), (0x2000, False),
                           (0x2030, False), (0x2032, True)]:
            with self.subTest(flags=flags):
                wire = PRIVATE[:4] + struct.pack('<I', flags) + PRIVATE[8:]
                result = self.probe.decode_short_update(wire)
                self.assertEqual((result['text'], result['pts'], result['date'],
                                  result['out']), ('Hi', 10, 1000, out))

    def test_unknown_or_payload_bearing_flags_are_rejected(self):
        for bit in set(range(32)) - {1, 4, 5, 13}:
            with self.subTest(bit=bit):
                wire = PRIVATE[:4] + struct.pack('<I', 1 << bit) + PRIVATE[8:]
                with self.assertRaises(ValueError):
                    self.probe.decode_short_update(wire)

    def test_nonpositive_group_sender_is_rejected(self):
        for sender in (0, -1, -2147483648):
            with self.subTest(sender=sender):
                wire = GROUP[:12] + struct.pack('<i', sender) + GROUP[16:]
                with self.assertRaises(ValueError):
                    self.probe.decode_short_update(wire)

    def test_nonpositive_peer_or_message_identity_is_rejected(self):
        for wire, position in [(PRIVATE, 8), (PRIVATE, 12), (GROUP, 16)]:
            for value in (0, -1):
                with self.subTest(position=position, value=value):
                    malformed = wire[:position] + struct.pack('<i', value) + wire[position + 4:]
                    with self.assertRaises(ValueError):
                        self.probe.decode_short_update(malformed)

    def test_extended_tl_string_length_and_padding_are_decoded(self):
        text = b'x' * 254
        wire = PRIVATE[:16] + b'\xfe\xfe\0\0' + text + b'\0\0' + PRIVATE[20:]
        result = self.probe.decode_short_update(wire)
        self.assertEqual((result['text'], result['pts']), ('x' * 254, 10))

    def test_empty_tl_string_does_not_shift_counters(self):
        wire = PRIVATE[:16] + b'\0' * 4 + PRIVATE[20:]
        self.assertEqual(self.probe.decode_short_update(wire)['text'], '')
        self.assertEqual(self.probe.decode_short_update(wire)['pts_count'], 1)

    def test_invalid_utf8_is_not_replaced_or_silently_accepted(self):
        for text_bytes in (b'\xc0\xaf', b'\xe4\xbd', b'\xed\xa0'):
            with self.subTest(text_bytes=text_bytes):
                wire = PRIVATE[:17] + text_bytes + PRIVATE[19:]
                with self.assertRaises(ValueError):
                    self.probe.decode_short_update(wire)

    def test_invalid_or_noncanonical_string_prefix_and_padding_are_rejected(self):
        malformed_strings = [
            b'\xffHi\0',
            b'\x02Hi\x01',
            b'\xfe\xfd\0\0' + b'x' * 253 + b'\0' * 3,
            b'\xfe\xfe\0',
        ]
        for string in malformed_strings:
            with self.subTest(length=len(string), prefix=string[:4]):
                with self.assertRaises(ValueError):
                    self.probe.decode_short_update(PRIVATE[:16] + string + PRIVATE[20:])

    def test_oversized_diagnostic_text_is_rejected(self):
        wire = PRIVATE[:16] + b'\xfe\x01\0\x01' + b'x' * 65537 + b'\0' * 3 + PRIVATE[20:]
        with self.assertRaises(ValueError):
            self.probe.decode_short_update(wire)

    def test_each_truncated_short_update_is_rejected(self):
        for wire in (PRIVATE, GROUP):
            for length in range(len(wire)):
                with self.subTest(constructor=wire[:4].hex(), length=length):
                    with self.assertRaises(ValueError):
                        self.probe.decode_short_update(wire[:length])

    def test_short_update_rejects_wrong_constructor_and_trailing_data(self):
        for wire in (b'\0' * 4 + PRIVATE[4:], PRIVATE + b'\0', PRIVATE + b'\0' * 4):
            with self.subTest(length=len(wire)):
                with self.assertRaises(ValueError):
                    self.probe.decode_short_update(wire)

    def test_normal_state_preserves_all_five_ints(self):
        self.assertEqual(self.probe.parse_sync_state(STATE), {
            'kind': 'state', 'pts': 10, 'qts': 20, 'date': 30,
            'seq': 40, 'unread_count': 50,
        })

    def test_contacts_apply_state_never_exposes_a_pts_value(self):
        result = self.probe.parse_sync_state(CONTACTS)
        self.assertEqual(result, {
            'kind': 'contacts_apply', 'apply_id': 7,
            'date': 1000, 'unread_count': 2,
        })
        self.assertNotIn('pts', result)

    def test_state_rejects_every_truncation_and_trailing_data(self):
        for wire in (STATE, CONTACTS):
            for length in range(len(wire)):
                with self.subTest(constructor=wire[:4].hex(), length=length):
                    with self.assertRaises(ValueError):
                        self.probe.parse_sync_state(wire[:length])
            with self.assertRaises(ValueError):
                self.probe.parse_sync_state(wire + b'\0' * 4)

    def test_state_rejects_other_constructors(self):
        with self.assertRaises(ValueError):
            self.probe.parse_sync_state(PRIVATE)

    def test_non_bytes_inputs_are_rejected(self):
        for data in (None, '', bytearray(PRIVATE), memoryview(PRIVATE)):
            for decoder in (self.probe.decode_short_update, self.probe.parse_sync_state):
                with self.subTest(type=type(data).__name__, decoder=decoder.__name__):
                    with self.assertRaises(ValueError):
                        decoder(data)


if __name__ == '__main__':
    unittest.main()
