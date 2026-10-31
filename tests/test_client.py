import importlib.util
import hashlib
import logging
import struct
import time
from collections import defaultdict
import unittest


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'optional Telethon is not installed')
class RawRequestTests(unittest.TestCase):
    def test_safe_error_reports_schema_reason_without_echoing_untrusted_exception_text(self):
        from tatocollector.client import safe_error
        from tatocollector.authwire import AuthProtocolError
        private = 'email@example.invalid:secret-code'
        self.assertNotIn(private, str(safe_error(ValueError(private))))
        details = safe_error(AuthProtocolError('authorization_flags_unsupported', authorization_flags=0x70400002))
        self.assertEqual(details['reason_code'], 'authorization_flags_unsupported')
        self.assertEqual(details['authorization_flags'], '70400002')

    def test_rpc_error_reflection_cannot_write_email_code_or_hash_to_status(self):
        from telethon.errors import RPCError
        from tatocollector.client import safe_error
        private = 'private@example.invalid:synthetic-otp:synthetic-hash'
        details = safe_error(RPCError(request=None, message=private, code=400))
        self.assertNotIn(private, str(details))
        self.assertTrue(details['rpc_message_redacted'])
        known = safe_error(RPCError(request=None, message='EMAIL_CODE_INVALID', code=400))
        self.assertEqual(known['rpc_message'], 'EMAIL_CODE_INVALID')
    def test_real_encrypted_rpc_padding_and_container_siblings_do_not_break_sent_code(self):
        from telethon.crypto import AuthKey, AES
        from telethon.extensions import BinaryReader
        from telethon.network.mtprotostate import MTProtoState
        from tatocollector.client import RawRequest
        from tatocollector.login import parse_sent_code

        auth_key = bytes(range(256))  # Synthetic offline test key, never used on network.
        result_wire = bytes.fromhex('0225005e 04000000 ddf1061b 06000000 01680000 3c000000')
        rpc = struct.pack('<Iq', 0xF35C6D01, 4) + result_wire
        now_id = (int(time.time()) << 32) | 1
        for container in (False, True):
            with self.subTest(container=container):
                state = MTProtoState(AuthKey(auth_key), loggers=defaultdict(lambda: logging.getLogger('test')))
                body = rpc
                if container:
                    sibling = struct.pack('<IqI', 0xF35C6D01, 8, 0x997275B5)
                    body = struct.pack('<IIqii', 0x73F1F8DC, 2, now_id, 1, len(rpc)) + rpc
                    body += struct.pack('<qii', now_id + 2, 3, len(sibling)) + sibling
                plain = struct.pack('<qqqii', 0, state.id, now_id + 4, 1, len(body)) + body
                padding_size = 12 + (-(len(plain) + 12) % 16)
                plain += b'\xa5' * padding_size
                msg_key = hashlib.sha256(auth_key[96:128] + plain).digest()[8:24]
                aes_key, iv = state._calc_key(auth_key, msg_key, False)
                packet = struct.pack('<Q', state.auth_key.key_id) + msg_key + AES.encrypt_ige(plain, aes_key, iv)
                decoded = state.decrypt_message_data(packet)
                result = decoded.obj.messages[0].obj if container else decoded.obj
                request = RawRequest(b'ABCD', parse_sent_code)
                self.assertEqual(request.read_result(BinaryReader(result.body))['code_hash'], 'h')

    def test_request_wire_is_preserved_without_exposing_secret_fields_in_repr(self):
        from tatocollector.client import RawRequest
        secret = b'user@example.invalid:secret_code_hash'
        wire = b'\x45\x04\x5c\x40' + secret
        request = RawRequest(wire, bytes)
        self.assertEqual(bytes(request), wire)
        self.assertNotIn('example', str(request))
        self.assertNotIn('secret_code', str(request.to_dict()))

    def test_rpc_result_uses_requested_decoder(self):
        from telethon.extensions import BinaryReader
        from tatocollector.client import RawRequest
        request = RawRequest(b'ABCD', lambda data: data.decode('utf-8'))
        self.assertEqual(request.read_result(BinaryReader('中文'.encode())), '中文')


if __name__ == '__main__':
    unittest.main()
