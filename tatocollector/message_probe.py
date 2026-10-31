"""Bounded diagnostic decoders for the recovered short-message wire subset.

Schema evidence: research/message-wire-schema.tl. Only direct short private
and ordinary-group updates without payload-bearing optional fields are
supported. This module performs no I/O and does not provide a full message
collector or difference/reconnect recovery.
"""

import struct


_PRIVATE = 0x914FBF11
_GROUP = 0x16812688
_STATE = 0xA56C2A3E
_CONTACTS_APPLY = 0x17765890
_BOOL_FLAGS = (1 << 1) | (1 << 4) | (1 << 5) | (1 << 13)
_PAYLOAD_FLAGS = (1 << 2) | (1 << 3) | (1 << 7) | (1 << 11)
_MAX_TEXT_BYTES = 65536


class _Reader:
    def __init__(self, data: bytes, maximum: int):
        if not isinstance(data, bytes):
            raise ValueError('diagnostic input must be bytes')
        if len(data) > maximum:
            raise ValueError('diagnostic input exceeds bounded size')
        self.data = data
        self.offset = 0

    def read(self, count: int) -> bytes:
        end = self.offset + count
        if count < 0 or end > len(self.data):
            raise ValueError('truncated diagnostic input')
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def uint(self) -> int:
        return struct.unpack('<I', self.read(4))[0]

    def integer(self) -> int:
        return struct.unpack('<i', self.read(4))[0]

    def positive_id(self, field: str) -> int:
        value = self.integer()
        if value <= 0:
            raise ValueError(f'{field} must be a positive int32')
        return value

    def text(self) -> str:
        size = self.read(1)[0]
        prefix_size = 1
        if size == 254:
            size = int.from_bytes(self.read(3), 'little')
            prefix_size = 4
            if size < 254:
                raise ValueError('noncanonical TL string length')
        elif size == 255:
            raise ValueError('invalid TL string prefix')
        if size > _MAX_TEXT_BYTES:
            raise ValueError('diagnostic text exceeds 64 KiB')
        raw = self.read(size)
        padding = self.read(-(prefix_size + size) % 4)
        if any(padding):
            raise ValueError('nonzero TL string padding')
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raise ValueError('invalid UTF-8 in short update') from None

    def finish(self) -> None:
        if self.offset != len(self.data):
            raise ValueError('trailing diagnostic data')


def decode_short_update(data: bytes) -> dict:
    """Decode one supported boxed short update, rejecting unknown payloads.

    Private outgoing updates identify the peer, not the current account, so
    their sender_id is None. Group sender_id comes from the positive int32
    from_id field even when the out flag is set.
    """
    reader = _Reader(data, _MAX_TEXT_BYTES + 64)
    constructor = reader.uint()
    if constructor not in (_PRIVATE, _GROUP):
        raise ValueError('unsupported short-update constructor')
    flags = reader.uint()
    if flags & _PAYLOAD_FLAGS:
        raise ValueError('short-update optional payloads are not supported')
    if flags & ~_BOOL_FLAGS:
        raise ValueError('unsupported short-update flags')
    out = bool(flags & (1 << 1))
    message_id = reader.positive_id('message_id')
    if constructor == _PRIVATE:
        chat_type = 'private'
        chat_id = reader.positive_id('user_id')
        sender_id = None if out else chat_id
    else:
        chat_type = 'group'
        sender_id = reader.positive_id('from_id')
        chat_id = reader.positive_id('chat_id')
    text = reader.text()
    pts = reader.integer()
    pts_count = reader.integer()
    date = reader.integer()
    reader.finish()
    return {
        'chat_type': chat_type, 'chat_id': chat_id, 'sender_id': sender_id,
        'message_id': message_id, 'text': text, 'pts': pts,
        'pts_count': pts_count, 'date': date, 'out': out,
    }


def parse_sync_state(data: bytes) -> dict:
    """Decode the normal state or the distinct contacts-application state."""
    reader = _Reader(data, 24)
    constructor = reader.uint()
    if constructor == _STATE:
        result = {
            'kind': 'state', 'pts': reader.integer(), 'qts': reader.integer(),
            'date': reader.integer(), 'seq': reader.integer(),
            'unread_count': reader.integer(),
        }
    elif constructor == _CONTACTS_APPLY:
        result = {
            'kind': 'contacts_apply', 'apply_id': reader.integer(),
            'date': reader.integer(), 'unread_count': reader.integer(),
        }
    else:
        raise ValueError('unsupported updates.State constructor')
    reader.finish()
    return result
