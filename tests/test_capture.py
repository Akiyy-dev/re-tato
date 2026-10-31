import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32' and importlib.util.find_spec('telethon'), 'Windows and protocol dependencies required')
class CaptureTests(unittest.TestCase):
    def test_update_is_encrypted_before_decode_and_stats_do_not_contain_text(self):
        from tatocollector.capture import new_stats, process_update, load_schema
        from tatocollector.events import PeerCache
        from tatocollector.wire_log import WireLog
        with tempfile.TemporaryDirectory() as directory:
            log = WireLog(Path(directory))
            stats = new_stats()
            raw = bytes.fromhex('11bf4f91 00000000 01000000 2a000000 02686900 02000000 01000000 00f15365')
            events = process_update(raw, load_schema(), PeerCache(), 7, log, stats)
            self.assertEqual(events[0]['text'], 'hi')
            self.assertEqual(stats['messages'], 1)
            self.assertNotIn('"text"', json.dumps(stats))
            self.assertEqual(len(list(Path(directory).glob('*.dpapi'))), 1)
            unknown = struct.pack('<I', 0xCAFEBABE) + b'private unknown data'
            self.assertEqual(process_update(unknown, load_schema(), PeerCache(), 7, log, stats), [])
            self.assertEqual(stats['unknown_constructors']['cafebabe'], 1)
            self.assertEqual(len(list(Path(directory).glob('*.dpapi'))), 2)
            self.assertNotIn('private unknown data', json.dumps(stats))


if __name__ == '__main__':
    unittest.main()
