from pathlib import Path
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Windows DPAPI required')
class WireLogTests(unittest.TestCase):
    def test_protocol_diagnostic_roundtrip_is_encrypted_and_not_a_session_file(self):
        from tatocollector.wire_log import WireLog
        with tempfile.TemporaryDirectory() as temp:
            log = WireLog(Path(temp))
            raw = bytes.fromhex('11bf4f91') + b'private message text'
            path = log.append(raw)
            self.assertNotIn(b'private message text', path.read_bytes())
            self.assertEqual(log.read(path), raw)
            with self.assertRaises(ValueError):
                log.append(b'x' * 1_048_577)


if __name__ == '__main__':
    unittest.main()
