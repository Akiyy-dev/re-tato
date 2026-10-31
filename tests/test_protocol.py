import struct
import unittest
import zlib

from tatocollector import protocol


NONCE = bytes(range(16))
# Hand-encoded resPQ: two int128s, eight-byte pq, vector<long> with one key.
BODY = bytes.fromhex(
    '63241605 000102030405060708090a0b0c0d0e0f'
    '101112131415161718191a1b1c1d1e1f'
    '08 17ed48941a08f981 000000 15c4b51c 01000000 8877665544332211'
)
PACKET = bytes.fromhex('0000000000000000 0100000000000000 40000000') + BODY


class ProtocolTests(unittest.TestCase):
    def test_request_has_zero_auth_key_and_correct_length_and_constructor(self):
        self.assertEqual(
            protocol.build_req_pq(NONCE, 4),
            bytes.fromhex('0000000000000000 0400000000000000 14000000 f18e7ebe') + NONCE,
        )

    def test_invalid_nonce_and_client_message_id_are_rejected(self):
        for nonce, message_id in [(b'x', 4), (NONCE, 3), (NONCE, -4)]:
            with self.subTest(message_id=message_id, nonce_length=len(nonce)):
                with self.assertRaises(ValueError):
                    protocol.build_req_pq(nonce, message_id)

    def test_res_pq_decodes_only_a_matching_nonce(self):
        decoded = protocol.parse_res_pq(PACKET, NONCE)
        self.assertEqual(decoded['pq'], '17ed48941a08f981')
        self.assertEqual(decoded['fingerprints'], ['1122334455667788'])
        self.assertEqual(decoded['server_nonce'], '101112131415161718191a1b1c1d1e1f')

    def test_nonce_mismatch_is_not_a_successful_handshake(self):
        with self.assertRaisesRegex(ValueError, 'nonce'):
            protocol.parse_res_pq(PACKET, b'X' * 16)

    def test_truncation_bad_length_and_unexpected_constructor_are_rejected(self):
        for bad in [PACKET[:-1], PACKET + b'\0', PACKET[:16] + struct.pack('<I', 4) + BODY,
                    PACKET[:20] + b'\0' * 4 + BODY[4:]]:
            with self.subTest(length=len(bad)):
                with self.assertRaises(ValueError):
                    protocol.parse_res_pq(bad, NONCE)

    def test_invalid_vector_count_and_encrypted_envelope_are_rejected(self):
        bad_count = PACKET[:72] + struct.pack('<I', 0x7fffffff) + PACKET[76:]
        for bad in [bad_count, b'\x01' + PACKET[1:]]:
            with self.assertRaises(ValueError):
                protocol.parse_res_pq(bad, NONCE)

    def test_transport_encodings_use_independent_wire_fixtures(self):
        payload = b'1234'
        self.assertEqual(protocol.encode_frame(payload, 'abridged'), b'\xef\x01' + payload)
        self.assertEqual(protocol.encode_frame(payload, 'intermediate'), b'\xee' * 4 + b'\x04\0\0\0' + payload)
        frame = protocol.encode_frame(payload, 'full')
        self.assertEqual(frame[:12], b'\x10\0\0\0' + b'\0' * 4 + payload)
        self.assertEqual(struct.unpack('<I', frame[-4:])[0], zlib.crc32(frame[:-4]))

    def test_long_abridged_length_uses_three_little_endian_bytes(self):
        frame = protocol.encode_frame(b'X' * 508, 'abridged')
        self.assertEqual(frame[:5], b'\xef\x7f\x7f\0\0')

    def test_invalid_transport_and_unaligned_payload_are_rejected(self):
        for payload, transport in [(b'x', 'full'), (b'1234', 'tls')]:
            with self.assertRaises(ValueError):
                protocol.encode_frame(payload, transport)


if __name__ == '__main__':
    unittest.main()
