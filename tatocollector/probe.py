"""No-account MTProto transport probe. This module never requests login codes."""

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import secrets
import socket
import struct
import time
import zlib

from .protocol import MAX_PACKET, TRANSPORTS, build_req_pq, encode_frame, parse_res_pq


class _ObfuscatedSocket:
    """Standard MTProto obfuscated transport; no proxy secret or authentication."""

    def __init__(self, sock, transport):
        try:
            from Crypto.Cipher import AES
        except ImportError as exc:
            raise ValueError('obfuscated transport requires optional pycryptodome dependency') from exc
        self.sock = sock
        while True:
            init = bytearray(secrets.token_bytes(64))
            if init[0] != 0xef and init[:4] not in (
                b'\xdd' * 4, b'\xee' * 4, b'POST', b'GET ', b'HEAD', b'OPTI', b'\x16\x03\x01\x02'
            ) and init[4:8] != b'\0' * 4:
                break
        init[56:60] = b'\xef' * 4 if transport == 'abridged' else b'\xee' * 4
        reversed_init = bytes(init[::-1])
        self.encrypt = AES.new(bytes(init[8:40]), AES.MODE_CTR, nonce=b'', initial_value=bytes(init[40:56]))
        self.decrypt = AES.new(reversed_init[8:40], AES.MODE_CTR, nonce=b'', initial_value=reversed_init[40:56])
        encrypted_init = self.encrypt.encrypt(bytes(init))
        self.sock.sendall(bytes(init[:56]) + encrypted_init[56:64])

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def sendall(self, data):
        self.sock.sendall(self.encrypt.encrypt(data))

    def recv(self, count):
        return self.decrypt.decrypt(self.sock.recv(count))


def _read_frame(sock, transport, deadline, captured):
    def read_exact(count):
        if not 0 < count <= MAX_PACKET + 12:
            raise ValueError('invalid frame length')
        data = bytearray()
        while len(data) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('response deadline exceeded')
            sock.settimeout(remaining)
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise EOFError(f'peer closed after {len(data)} of {count} expected bytes')
            data.extend(chunk)
            captured.extend(chunk[:max(0, 32 - len(captured))])
        return bytes(data)

    if transport == 'abridged':
        marker = read_exact(1)[0]
        if marker >= 128:
            raise ValueError('unexpected quick-ack or non-abridged response')
        size = (int.from_bytes(read_exact(3), 'little') if marker == 127 else marker) * 4
        if not 4 <= size <= MAX_PACKET:
            raise ValueError('invalid abridged frame length')
        payload = read_exact(size)
    else:
        header = read_exact(4)
        signed_size = struct.unpack('<i', header)[0]
        if signed_size < 0:
            raise ValueError(f'transport error or nonstandard frame: {signed_size}')
        size = signed_size
        overhead = 12 if transport == 'full' else 0
        if not 4 + overhead <= size <= MAX_PACKET + overhead or size % 4:
            raise ValueError('invalid frame length')
        if transport == 'full':
            rest = read_exact(size - 4)
            if struct.unpack_from('<I', rest)[0] != 0:
                raise ValueError('unexpected first transport sequence')
            if zlib.crc32(header + rest[:-4]) != struct.unpack('<I', rest[-4:])[0]:
                raise ValueError('invalid frame CRC32')
            payload = rest[4:-4]
        else:
            payload = read_exact(size)
    if len(payload) == 4:
        raise ValueError(f'transport error: {struct.unpack("<i", payload)[0]}')
    return payload


def probe_endpoint(host: str, port: int, transport: str, timeout: float, *, obfuscated: bool = False) -> dict:
    ipaddress.ip_address(host)  # Literal IPs only; no unbounded DNS or URL handling.
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('timeout must be finite, positive and at most 30 seconds')
    if transport not in TRANSPORTS:
        raise ValueError('unsupported transport')
    if obfuscated and transport not in ('abridged', 'intermediate'):
        raise ValueError('obfuscated diagnostic supports abridged or intermediate only')
    result = {
        'host': host, 'port': port, 'transport': transport,
        'connected': False, 'protocol_verified': False, 'authenticated': False,
        'stage': 'connect',
        'obfuscated': obfuscated,
    }
    captured = bytearray()
    start = time.monotonic()
    deadline = start + timeout
    nonce = secrets.token_bytes(16)
    message_id = ((time.time_ns() << 32) // 1_000_000_000) & ~3
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            result['connected'] = True
            result['stage'] = 'send_req_pq'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('connection consumed response deadline')
            sock.settimeout(remaining)
            frame = encode_frame(build_req_pq(nonce, message_id), transport)
            stream = sock
            if obfuscated:
                stream = _ObfuscatedSocket(sock, transport)
                frame = frame[1 if transport == 'abridged' else 4:]
            stream.sendall(frame)
            result['stage'] = 'read_response'
            packet = _read_frame(stream, transport, deadline, captured)
            result['stage'] = 'validate_res_pq'
            result.update(parse_res_pq(packet, nonce))
            result['protocol_verified'] = True
            result['stage'] = 'res_pq_received'
    except (OSError, ValueError, EOFError) as exc:
        result['error_type'] = type(exc).__name__
        result['error'] = str(exc)
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    result['response_prefix_hex'] = captured.hex()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='explicit service IP from endpoint configuration')
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--transport', choices=(*TRANSPORTS, 'all'), default='all')
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--obfuscated', action='store_true', help='standard obfuscated transport, requires pycryptodome')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    transports = TRANSPORTS if args.transport == 'all' else (args.transport,)
    if args.obfuscated and args.transport == 'all':
        transports = ('abridged', 'intermediate')
    try:
        results = [probe_endpoint(args.host, args.port, transport, args.timeout, obfuscated=args.obfuscated) for transport in transports]
    except ValueError as exc:
        parser.error(str(exc))
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'unauthenticated req_pq only; no account login or message capture',
        'results': results,
    }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + '\n', encoding='utf-8')
    print(serialized)
    return 0 if any(result['protocol_verified'] for result in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
