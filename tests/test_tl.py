import struct
import unittest

from tatocollector.tl import Schema, TLDecodeError


SCHEMA = '''
peerUser#9db1bc6d user_id:int = Peer;
message#81ff9c4a flags:# out:flags.1?true flags2:# id:int from_id:flags.8?int to_id:Peer date:int message:string = Message;
updateNewMessage#1f2b0afd message:Message pts:int pts_count:int = Update;
updates#74ae4240 updates:Vector<Update> date:int seq:int = Updates;
'''


class TLTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema(SCHEMA)

    def test_nested_message_reads_flags2_before_id_and_int32_peer(self):
        data = bytes.fromhex('fd0a2b1f 4a9cff81 00010000 00000000 7b000000 09000000'
                             '6dbcb19d 2a000000 00f15365 02686900 0c000000 01000000')
        result = self.schema.decode(data)
        self.assertEqual(result['message']['id'], 123)
        self.assertEqual(result['message']['to_id']['user_id'], 42)
        self.assertEqual(result['message']['message'], 'hi')
        self.assertEqual(result['pts'], 12)
        self.assertFalse(result['message']['out'])

    def test_zero_byte_flag_and_unused_flags_do_not_consume_wire_data(self):
        data = bytes.fromhex('4a9cff81 02000080 00000000 7b000000 6dbcb19d 2a000000 00f15365 02686900')
        self.assertTrue(self.schema.decode(data)['out'])

    def test_unknown_constructor_reports_only_type_and_offset(self):
        with self.assertRaises(TLDecodeError) as error:
            self.schema.decode(bytes.fromhex('78563412') + b'private message')
        self.assertEqual(error.exception.constructor, 0x12345678)
        self.assertNotIn('private message', str(error.exception))

    def test_prefix_decode_leaves_rpc_padding_but_strict_decode_rejects_it(self):
        data = bytes.fromhex('6dbcb19d 2a000000') + b'padding'
        result, consumed = self.schema.decode_prefix(data)
        self.assertEqual((result['user_id'], consumed), (42, 8))
        with self.assertRaises(TLDecodeError):
            self.schema.decode(data)

    def test_vector_marker_count_and_boxed_union_are_checked(self):
        empty = bytes.fromhex('4042ae74 15c4b51c 00000000 00f15365 01000000')
        self.assertEqual(self.schema.decode(empty)['updates'], [])
        for bad in [empty[:4] + b'\0' * 4 + empty[8:],
                    empty[:8] + struct.pack('<i', 1000000) + empty[12:]]:
            with self.assertRaises(TLDecodeError):
                self.schema.decode(bad)
        with self.assertRaises(TLDecodeError):
            self.schema.decode(bytes.fromhex('6dbcb19d 2a000000'), expected_type='Message')

    def test_truncated_and_invalid_utf8_strings_do_not_succeed(self):
        prefix = bytes.fromhex('4a9cff81 00000000 00000000 7b000000 6dbcb19d 2a000000 00f15365')
        for suffix in [b'\x02h', b'\x02\xff\xff\0']:
            with self.assertRaises(TLDecodeError):
                self.schema.decode(prefix + suffix)

    def test_conflicting_constructor_and_unresolved_flag_reference_are_rejected(self):
        with self.assertRaises(ValueError):
            self.schema.add('other#9db1bc6d id:long = Other;')
        with self.assertRaises(ValueError):
            Schema('invalid#12345678 value:missing.1?int = Bad;')

    def test_functions_are_not_registered_as_response_objects(self):
        schema = Schema('peerUser#9db1bc6d user_id:int = Peer;\n---functions---\napi.call#12345678 = Peer;')
        with self.assertRaises(TLDecodeError):
            schema.decode(bytes.fromhex('78563412'))


if __name__ == '__main__':
    unittest.main()
