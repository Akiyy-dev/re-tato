import struct
import unittest

from tatocollector.authwire import (encode_email_code_request, encode_email_signin,
                                  wrap_init, parse_sent_code, parse_authorization)


class AuthWireTests(unittest.TestCase):
    def test_email_code_request_sets_signin_flag_without_serializing_boolean(self):
        actual = encode_email_code_request('a@b.c', 7, 'key')
        self.assertEqual(actual, bytes.fromhex('ad5b5720 08000000 056140622e630000 07000000 036b6579'))

    def test_captcha_answer_is_boxed_json_and_requires_both_fields(self):
        actual = encode_email_code_request('a@b.c', 7, 'key', captcha_token='tok', captcha_answer={})
        self.assertEqual(actual[-12:], bytes.fromhex('03746f6b 048d747d 027b7d00'))
        self.assertEqual(actual[4:8], b'\x0a\0\0\0')
        with self.assertRaises(ValueError):
            encode_email_code_request('a@b.c', 7, 'key', captcha_token='tok')

    def test_image_text_answer_remains_raw_inside_datajson_string(self):
        actual = encode_email_code_request('a@b.c', 7, 'key', captcha_token='tok', captcha_answer='ABCD')
        self.assertEqual(actual[-12:], bytes.fromhex('048d747d 0441424344000000'))

    def test_signin_uses_three_strings_in_recovered_order(self):
        self.assertEqual(encode_email_signin('a@b.c', 'h', '123456'), bytes.fromhex(
            '45045c40 056140622e630000 01680000 0631323334353600'))

    def test_init_connection_has_no_extra_flags_word(self):
        payload = wrap_init(b'ABCD', {'layer': 123, 'api_id': 7, 'app_version': 'x'})
        self.assertEqual(payload[:16], bytes.fromhex('0d0d9bda 7b000000 a61d48c7 07000000'))
        self.assertEqual(payload[-4:], b'ABCD')

    def test_email_sent_code_exposes_hash_length_and_timeout(self):
        wire = bytes.fromhex('0225005e 04000000 ddf1061b 06000000 01680000 3c000000')
        parsed = parse_sent_code(wire)
        self.assertEqual(parsed, {'kind': 'sent_code', 'code_hash': 'h', 'length': 6, 'timeout': 60})

    def test_app_code_and_sms_next_type_use_recovered_union_shapes(self):
        wire = bytes.fromhex('0225005e 02000000 8659bb3d 06000000 01680000 8c15a372')
        parsed = parse_sent_code(wire)
        self.assertEqual(parsed['delivery'], 'app')
        self.assertEqual(parsed['code_hash'], 'h')

    def test_captcha_is_returned_as_challenge_not_sent_code(self):
        wire = bytes.fromhex('34a994b5 03746f6b 02000000 048d747d 027b7d00')
        parsed = parse_sent_code(wire)
        self.assertEqual(parsed, {'kind': 'captcha', 'token': 'tok', 'type': 2, 'parameters': {}})

    def test_authorization_reads_int32_user_id_after_two_flags(self):
        wire = bytes.fromhex('160905cd 00000000 4482524e 00000000 00000000 7b000000')
        self.assertEqual(parse_authorization(wire)['user_id'], 123)

    def test_authorization_with_tmp_sessions_keeps_user_alignment(self):
        wire = bytes.fromhex('160905cd 01000000 02000000 4482524e 00000000 00000000 7b000000')
        self.assertEqual(parse_authorization(wire)['user_id'], 123)

    def test_authorization_capability_bits_do_not_consume_extra_wire_fields(self):
        # Official Android reads wallet/rpm/channel-rpm/fansX as flag booleans.
        for flags in (0x10000000, 0x20000000, 0x40000000, 0x00400000, 0x70400000):
            with self.subTest(flags=hex(flags)):
                wire = struct.pack('<III', 0xCD050916, flags, 0x4E528244)
                wire += struct.pack('<IIi', 0, 0, 123)
                self.assertEqual(parse_authorization(wire)['user_id'], 123)

    def test_known_android_user_variant_preserves_the_same_flags2_int32_id_prefix(self):
        wire = bytes.fromhex('160905cd 00004070 c126b6bc 01000000 00000000 7b000000')
        self.assertEqual(parse_authorization(wire)['user_id'], 123)

    def test_unknown_authorization_user_layout_reports_only_public_structure(self):
        from tatocollector.authwire import AuthProtocolError
        wire = bytes.fromhex('160905cd 00000007 02000000') + b'private@example.invalid:123456'
        with self.assertRaises(AuthProtocolError) as caught:
            parse_authorization(wire)
        self.assertEqual(caught.exception.diagnostic['reason_code'], 'authorization_user_constructor_unsupported')
        self.assertEqual(caught.exception.diagnostic['user_constructor'], '00000002')
        self.assertNotIn('example', repr(caught.exception.diagnostic))

    def test_authorization_unused_flags_follow_official_prefix_reader(self):
        # Official auth.authorization reader consumes no wire for flags except bit0.
        for flags in (0x07000000, 0x08000000, 0x80000000, 0xFFFFFFFF):
            with self.subTest(flags=hex(flags)):
                wire = struct.pack('<II', 0xCD050916, flags)
                if flags & 1:
                    wire += struct.pack('<i', 2)
                wire += bytes.fromhex('4482524e 00000000 00000000 7b000000')
                self.assertEqual(parse_authorization(wire)['user_id'], 123)

    def test_self_user_result_verifies_single_known_identity_for_pending_transport(self):
        from tatocollector.authwire import parse_self_user
        wire = bytes.fromhex('15c4b51c 01000000 c126b6bc 00000000 00000000 7b000000')
        self.assertEqual(parse_self_user(wire)['user_id'], 123)
        with self.assertRaises(ValueError):
            parse_self_user(bytes.fromhex('15c4b51c 00000000'))

    def test_unknown_types_and_truncation_do_not_claim_authentication(self):
        for wire in [b'', struct.pack('<I', 0), bytes.fromhex('160905cd 00000000 ba500220 7b000000')]:
            with self.assertRaises(ValueError):
                parse_authorization(wire)
        with self.assertRaises(ValueError):
            parse_sent_code(bytes.fromhex('34a994b5 03746f6b 02000000'))


if __name__ == '__main__':
    unittest.main()
