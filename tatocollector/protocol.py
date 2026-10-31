"""Strict, unauthenticated MTProto handshake codecs.

Reference: https://core.telegram.org/mtproto/auth_key
Successful resPQ parsing establishes neither server authenticity nor user login.
"""

import struct
import zlib

MAX_PACKET = 65536
TRANSPORTS = ('abridged', 'intermediate', 'full')


def build_req_pq(nonce: bytes, message_id: int) -> bytes:
    if len(nonce) != 16:
        raise ValueError('nonce must contain exactly 16 bytes')
    if not 0 < message_id < 2**64 or message_id % 4:
        raise ValueError('client message_id must be a positive uint64 divisible by 4')
    body = struct.pack('<I', 0xBE7E8EF1) + nonce
    return struct.pack('<QQI', 0, message_id, len(body)) + body


def parse_res_pq(packet: bytes, expected_nonce: bytes) -> dict:
    if len(expected_nonce) != 16:
        raise ValueError('expected nonce must contain 16 bytes')
    if not 20 <= len(packet) <= MAX_PACKET:
        raise ValueError('invalid envelope length')
    auth_key_id, message_id, length = struct.unpack_from('<QQI', packet)
    if auth_key_id != 0:
        raise ValueError('expected unauthenticated envelope')
    if message_id % 2 != 1:
        raise ValueError('invalid server message_id')
    if length != len(packet) - 20 or length % 4 or length < 48:
        raise ValueError('invalid message length')
    body = packet[20:]
    if struct.unpack_from('<I', body)[0] != 0x05162463:
        raise ValueError('unexpected constructor; expected resPQ')
    if body[4:20] != expected_nonce:
        raise ValueError('response nonce does not match request')
    server_nonce = body[20:36]
    # pq is at most 64 bits for this diagnostic. Reject arbitrary-length input.
    size = body[36]
    if not 1 <= size <= 8:
        raise ValueError('invalid pq size')
    pq = body[37:37 + size]
    if len(pq) != size or int.from_bytes(pq, 'big') <= 1:
        raise ValueError('invalid pq')
    offset = 36 + ((1 + size + 3) // 4) * 4
    if len(body) < offset + 8:
        raise ValueError('truncated fingerprint vector')
    constructor, count = struct.unpack_from('<II', body, offset)
    if constructor != 0x1CB5C415 or not 1 <= count <= 64:
        raise ValueError('invalid fingerprint vector')
    if len(body) != offset + 8 + count * 8:
        raise ValueError('truncated or trailing fingerprint vector data')
    fingerprints = struct.unpack_from('<' + 'Q' * count, body, offset + 8)
    return {
        'pq': pq.hex(),
        'server_nonce': server_nonce.hex(),
        'fingerprints': [f'{key:016x}' for key in fingerprints],
    }


def encode_frame(payload: bytes, transport: str, sequence: int = 0) -> bytes:
    if transport not in TRANSPORTS:
        raise ValueError('unsupported transport')
    if not payload or len(payload) % 4 or len(payload) > MAX_PACKET:
        raise ValueError('payload must be bounded, nonempty and 4-byte aligned')
    if not 0 <= sequence <= 0xffffffff:
        raise ValueError('invalid frame sequence')
    if transport == 'abridged':
        words = len(payload) // 4
        header = bytes([words]) if words < 127 else b'\x7f' + words.to_bytes(3, 'little')
        return b'\xef' + header + payload
    if transport == 'intermediate':
        return b'\xee' * 4 + struct.pack('<I', len(payload)) + payload
    frame = struct.pack('<II', len(payload) + 12, sequence) + payload
    return frame + struct.pack('<I', zlib.crc32(frame))
