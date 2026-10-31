"""Scoped registration of Tatotalk root updates with a redacted representation."""

import struct

from telethon.tl.alltlobjects import tlobjects
from telethon.tl.tlobject import TLObject


ROOT_UPDATES = (0x914FBF11, 0x16812688, 0x78D4DEC1, 0x725B04C3, 0x74AE4240, 0x11F1331C, 0xE317AF7E)


class RawTatoUpdates(TLObject):
    SUBCLASS_OF_ID = 0x8AF52AAC

    def __init__(self, raw):
        self.raw = raw

    @classmethod
    def from_reader(cls, reader):
        # The pinned library restores message-container boundaries after this
        # callback. A schema decoder later consumes the self-delimiting object;
        # its remaining bytes may include protocol padding or container siblings.
        raw = struct.pack('<I', cls.CONSTRUCTOR_ID) + reader.read()
        if len(raw) > 1_048_576:
            raise ValueError('update diagnostic exceeds bounded size')
        result = cls(raw)
        # A production/probe sink can persist the encrypted sample before the
        # SDK acknowledges the server message or queues it in process memory.
        result.sample_path = cls._sink(raw) if cls._sink is not None else None
        return result

    def to_dict(self):
        return {'_': 'RawTatoUpdates', 'constructor': f'{self.CONSTRUCTOR_ID:08x}', 'bytes': len(self.raw)}


class TatoUpdatesRegistry:
    def __init__(self, sink=None):
        self.sink = sink

    def __enter__(self):
        self.previous = {}
        for constructor in ROOT_UPDATES:
            self.previous[constructor] = tlobjects.get(constructor)
            tlobjects[constructor] = type(f'TatoUpdates{constructor:08x}', (RawTatoUpdates,),
                                          {'CONSTRUCTOR_ID': constructor, '_sink': staticmethod(self.sink) if self.sink else None})
        return self

    def __exit__(self, *_):
        for constructor, previous in self.previous.items():
            if previous is None:
                tlobjects.pop(constructor, None)
            else:
                tlobjects[constructor] = previous
