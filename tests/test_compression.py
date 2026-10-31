import gzip
import importlib.util
import struct
import unittest


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'optional Telethon is not installed')
class BoundedGzipTests(unittest.TestCase):
    def guard(self):
        self.assertIsNotNone(
            importlib.util.find_spec('tatocollector.compression'),
            'the bounded gzip context is not implemented',
        )
        from tatocollector.compression import BoundedGzip
        return BoundedGzip()

    def decode(self, compressed, entry):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        from telethon.tl.tlobject import TLObject
        body = TLObject.serialize_bytes(compressed)
        if entry == 'read':
            return GzipPacked.read(BinaryReader(struct.pack('<I', 0x3072CFA1) + body))
        return GzipPacked.from_reader(BinaryReader(body)).data

    def test_both_entry_points_expand_valid_compressed_content(self):
        content = b'synthetic protocol payload\x00\x01'
        for entry in ('read', 'from_reader'):
            with self.subTest(entry=entry), self.guard():
                self.assertEqual(self.decode(gzip.compress(content), entry), content)

    def test_service_object_parsing_uses_the_bounded_classmethod(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        wire = bytes(GzipPacked(b'synthetic service update'))
        with self.guard():
            self.assertEqual(BinaryReader(wire).tgread_object().data, b'synthetic service update')

    def test_more_than_one_mib_of_output_is_rejected_on_both_paths(self):
        bomb = gzip.compress(b'A' * 1_048_577)
        for entry in ('read', 'from_reader'):
            with self.subTest(entry=entry), self.guard():
                with self.assertRaisesRegex(ValueError, 'gzip_output_too_large'):
                    self.decode(bomb, entry)

    def test_exactly_one_mib_of_output_is_accepted(self):
        with self.guard():
            decoded = self.decode(gzip.compress(b'A' * 1_048_576), 'read')
        self.assertEqual(len(decoded), 1_048_576)
        self.assertEqual(decoded, b'A' * 1_048_576)

    def test_compressed_length_is_rejected_before_reading_the_declared_body(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        # No body is supplied: the length check must precede a body allocation/read.
        excessive_length = b'\xfe' + (1_048_577).to_bytes(3, 'little')
        for entry in ('read', 'from_reader'):
            with self.subTest(entry=entry), self.guard():
                wire = struct.pack('<I', 0x3072CFA1) + excessive_length
                with self.assertRaisesRegex(ValueError, 'gzip_input_too_large'):
                    if entry == 'read':
                        GzipPacked.read(BinaryReader(wire))
                    else:
                        GzipPacked.from_reader(BinaryReader(excessive_length))

    def test_truncated_corrupt_and_empty_gzip_streams_are_rejected(self):
        valid = gzip.compress(b'synthetic compressed content')
        bad_streams = (valid[:-1], valid[:10], valid[:-8] + b'\0' * 8, b'', b'invalid')
        for entry in ('read', 'from_reader'):
            for number, compressed in enumerate(bad_streams):
                with self.subTest(entry=entry, case=number), self.guard():
                    with self.assertRaisesRegex(ValueError, 'gzip_invalid_or_truncated'):
                        self.decode(compressed, entry)

    def test_single_member_rule_rejects_concatenated_streams_and_trailing_data(self):
        valid = gzip.compress(b'synthetic content')
        for entry in ('read', 'from_reader'):
            for suffix in (valid, b'trailing data'):
                with self.subTest(entry=entry, suffix_length=len(suffix)), self.guard():
                    with self.assertRaisesRegex(ValueError, 'gzip_trailing_data'):
                        self.decode(valid + suffix, entry)

    def test_tl_byte_array_errors_are_normalized_without_echoing_payload(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        # Invalid long-form marker and a missing padded payload, respectively.
        for body in (b'\xff', b'\x05privat'):
            with self.subTest(length=len(body)), self.guard():
                with self.assertRaisesRegex(ValueError, '^gzip_invalid_bytes$'):
                    GzipPacked.from_reader(BinaryReader(body))

    def test_read_rejects_an_unexpected_constructor(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        with self.guard():
            with self.assertRaisesRegex(ValueError, 'gzip_constructor_mismatch'):
                GzipPacked.read(BinaryReader(b'\0' * 4))

    def test_consuming_a_packed_object_preserves_the_following_sibling(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.core import GzipPacked
        from telethon.tl.tlobject import TLObject
        packed = TLObject.serialize_bytes(gzip.compress(b'compressed first object'))
        for entry in ('read', 'from_reader'):
            prefix = struct.pack('<I', 0x3072CFA1) if entry == 'read' else b''
            reader = BinaryReader(prefix + packed + b'NEXT')
            with self.subTest(entry=entry), self.guard():
                if entry == 'read':
                    self.assertEqual(GzipPacked.read(reader), b'compressed first object')
                else:
                    self.assertEqual(GzipPacked.from_reader(reader).data, b'compressed first object')
                self.assertEqual(reader.read(4), b'NEXT')

    def test_scope_replaces_and_restores_the_original_descriptors(self):
        from telethon.tl.core import GzipPacked
        original_read = GzipPacked.__dict__['read']
        original_from_reader = GzipPacked.__dict__['from_reader']
        with self.guard():
            self.assertIsInstance(GzipPacked.__dict__['read'], staticmethod)
            self.assertIsInstance(GzipPacked.__dict__['from_reader'], classmethod)
            self.assertIsNot(GzipPacked.__dict__['read'], original_read)
            self.assertIsNot(GzipPacked.__dict__['from_reader'], original_from_reader)
        self.assertIs(GzipPacked.__dict__['read'], original_read)
        self.assertIs(GzipPacked.__dict__['from_reader'], original_from_reader)

    def test_scope_restores_descriptors_when_decompression_raises(self):
        from telethon.tl.core import GzipPacked
        original_read = GzipPacked.__dict__['read']
        original_from_reader = GzipPacked.__dict__['from_reader']
        with self.assertRaisesRegex(ValueError, 'gzip_output_too_large'):
            with self.guard():
                self.decode(gzip.compress(b'A' * 1_048_577), 'read')
        self.assertIs(GzipPacked.__dict__['read'], original_read)
        self.assertIs(GzipPacked.__dict__['from_reader'], original_from_reader)

    def test_scope_restores_descriptors_and_propagates_an_unrelated_exception(self):
        from telethon.tl.core import GzipPacked
        original_read = GzipPacked.__dict__['read']
        original_from_reader = GzipPacked.__dict__['from_reader']
        with self.assertRaisesRegex(RuntimeError, 'synthetic scope failure'):
            with self.guard():
                raise RuntimeError('synthetic scope failure')
        self.assertIs(GzipPacked.__dict__['read'], original_read)
        self.assertIs(GzipPacked.__dict__['from_reader'], original_from_reader)


if __name__ == '__main__':
    unittest.main()
