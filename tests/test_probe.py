import contextlib
import importlib.util
import socket
import struct
import threading
import unittest
import zlib

from tatocollector.probe import probe_endpoint
from test_protocol import BODY


@contextlib.contextmanager
def server(transport, mode='valid', obfuscated=False):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    listener.settimeout(3)
    failures = []

    def run():
        try:
            conn, _ = listener.accept()
            with conn, conn.makefile('rb') as reader:
                conn.settimeout(2)
                encrypt_response = None
                if obfuscated:
                    from Crypto.Cipher import AES
                    init = reader.read(64)
                    decrypt_request = AES.new(init[8:40], AES.MODE_CTR, nonce=b'', initial_value=init[40:56])
                    header = decrypt_request.decrypt(init)
                    expected_tag = b'\xef' * 4 if transport == 'abridged' else b'\xee' * 4
                    assert header[56:60] == expected_tag
                    reversed_init = init[::-1]
                    encrypt_response = AES.new(reversed_init[8:40], AES.MODE_CTR, nonce=b'', initial_value=reversed_init[40:56])
                    frame_size = 41 if transport == 'abridged' else 44
                    plaintext = decrypt_request.decrypt(reader.read(frame_size))
                    expected_frame_header = b'\x0a' if transport == 'abridged' else b'\x28\0\0\0'
                    assert plaintext.startswith(expected_frame_header)
                    request = plaintext[len(expected_frame_header):]
                elif transport == 'abridged':
                    assert reader.read(2) == b'\xef\x0a'
                elif transport == 'intermediate':
                    assert reader.read(8) == b'\xee' * 4 + b'\x28\0\0\0'
                else:
                    assert reader.read(8) == b'\x34\0\0\0' + b'\0' * 4
                if not obfuscated:
                    request = reader.read(40)
                assert request[:8] == b'\0' * 8
                assert request[16:24] == bytes.fromhex('14000000 f18e7ebe')
                nonce = request[24:40] if mode != 'wrong_nonce' else b'X' * 16
                body = BODY[:4] + nonce + BODY[20:]
                packet = struct.pack('<QQI', 0, 1, len(body)) + body
                if transport == 'abridged':
                    response = b'\x15' + packet
                elif transport == 'intermediate':
                    response = struct.pack('<I', len(packet)) + packet
                else:
                    response = struct.pack('<II', len(packet) + 12, 0) + packet
                    response += struct.pack('<I', zlib.crc32(response))
                if mode == 'truncated':
                    response = response[:-1]
                elif mode == 'oversized':
                    response = b'\xff\xff\xff\x7f'
                elif mode == 'bad_crc':
                    response = response[:-4] + b'\0' * 4
                if mode == 'silent':
                    threading.Event().wait(0.15)
                else:
                    conn.sendall(encrypt_response.encrypt(response) if encrypt_response else response)
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        thread.join(4)
        listener.close()
        if failures:
            raise failures[0]
        if thread.is_alive():
            raise AssertionError('controlled server did not finish')


class ProbeTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('Crypto'), 'optional pycryptodome is not installed')
    def test_obfuscated_request_and_response_keep_separate_ctr_streams(self):
        for transport in ['abridged', 'intermediate']:
            with self.subTest(transport=transport), server(transport, obfuscated=True) as port:
                result = probe_endpoint('127.0.0.1', port, transport, 2, obfuscated=True)
                self.assertTrue(result['protocol_verified'], result)
                self.assertFalse(result['authenticated'])

    def test_silent_peer_is_bounded_by_response_deadline(self):
        with server('abridged', mode='silent') as port:
            result = probe_endpoint('127.0.0.1', port, 'abridged', 0.05)
        self.assertFalse(result['protocol_verified'])
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertLess(result['elapsed_seconds'], 0.14)

    def test_real_tcp_round_trip_checks_nonce_in_each_transport(self):
        for transport in ['abridged', 'intermediate', 'full']:
            with self.subTest(transport=transport), server(transport) as port:
                result = probe_endpoint('127.0.0.1', port, transport, 2)
                self.assertTrue(result['protocol_verified'], result)
                self.assertFalse(result['authenticated'])
                self.assertEqual(result['fingerprints'], ['1122334455667788'])

    def test_tcp_connection_with_wrong_nonce_is_not_protocol_success(self):
        with server('abridged', 'wrong_nonce') as port:
            result = probe_endpoint('127.0.0.1', port, 'abridged', 2)
        self.assertTrue(result['connected'])
        self.assertFalse(result['protocol_verified'])
        self.assertIn('nonce', result['error'])

    def test_truncated_frame_oversized_length_and_bad_crc_fail_boundedly(self):
        for transport, mode in [('abridged', 'truncated'), ('intermediate', 'oversized'), ('full', 'bad_crc')]:
            with self.subTest(mode=mode), server(transport, mode) as port:
                result = probe_endpoint('127.0.0.1', port, transport, 2)
                self.assertFalse(result['protocol_verified'], result)
                self.assertTrue(result['error'])

    def test_invalid_timeout_or_port_does_not_attempt_connection(self):
        for port, timeout in [(0, 2), (65536, 2), (20334, 0), (20334, float('nan'))]:
            with self.assertRaises(ValueError):
                probe_endpoint('127.0.0.1', port, 'abridged', timeout)


if __name__ == '__main__':
    unittest.main()
