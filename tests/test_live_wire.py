import importlib.util
import struct
import unittest


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'optional Telethon is not installed')
class LiveWireTests(unittest.TestCase):
    def test_registry_override_captures_tato_updates_and_restores_originals(self):
        from telethon.extensions import BinaryReader
        from telethon.tl.alltlobjects import tlobjects
        from tatocollector.live_wire import TatoUpdatesRegistry
        constructor = 0x914FBF11
        before = tlobjects.get(constructor)
        wire = bytes.fromhex('11bf4f91 00000000 01000000 2a000000 02686900 02000000 01000000 00f15365')
        with TatoUpdatesRegistry():
            message = BinaryReader(wire).tgread_object()
            self.assertEqual(message.raw, wire)
            self.assertEqual(message.SUBCLASS_OF_ID, 0x8AF52AAC)
            self.assertNotIn('hi', str(message))
        self.assertIs(tlobjects.get(constructor), before)

    def test_overreading_raw_update_does_not_consume_a_container_sibling(self):
        from telethon.extensions import BinaryReader
        from tatocollector.live_wire import TatoUpdatesRegistry
        first = bytes.fromhex('11bf4f91 00000000 01000000 2a000000 02686900 02000000 01000000 00f15365')
        sibling = struct.pack('<IqI', 0xF35C6D01, 4, 0x997275B5)
        wire = struct.pack('<IIqii', 0x73F1F8DC, 2, 1, 1, len(first)) + first
        wire += struct.pack('<qii', 3, 3, len(sibling)) + sibling
        with TatoUpdatesRegistry():
            parsed = BinaryReader(wire).tgread_object()
        self.assertEqual(len(parsed.messages), 2)
        self.assertEqual(parsed.messages[1].obj.req_msg_id, 4)
        self.assertEqual(parsed.messages[0].obj.raw[:len(first)], first)


if __name__ == '__main__':
    unittest.main()
