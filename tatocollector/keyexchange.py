"""Pinned-public-key, no-user MTProto DH handshake diagnostic.

Uses Telethon's transport and handshake only, never TelegramClient/user login.
Generated transport keys are discarded in memory; no session file is created.
"""

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha1
import ipaddress
import json
import logging
import math
from pathlib import Path
import struct
import time
import rsa as rsa_library

from Crypto.PublicKey import RSA
from telethon.crypto import rsa
from telethon.network.authenticator import do_authentication
from telethon.network.connection.tcpabridged import AbridgedPacketCodec
from telethon.network.connection.tcpobfuscated import ConnectionTcpObfuscated
from telethon.network.mtprotoplainsender import MTProtoPlainSender
from telethon.tl import TLObject

from .protocol import MAX_PACKET

EXPECTED_FINGERPRINT = '2bad57ceedcd0248'


def to_pkcs1_public_key(pem: bytes) -> bytes:
    key = RSA.import_key(pem)
    return rsa_library.PublicKey(key.n, key.e).save_pkcs1()


def verify_public_key(pem: bytes, expected_fingerprint: str) -> str:
    key = RSA.import_key(pem)
    if key.has_private() or key.size_in_bits() != 2048 or key.e != 65537:
        raise ValueError('expected a 2048-bit public-only RSA server key with exponent 65537')
    integers = [number.to_bytes((number.bit_length() + 7) // 8, 'big') for number in (key.n, key.e)]
    fingerprint = sha1(b''.join(TLObject.serialize_bytes(n) for n in integers)).digest()[-8:][::-1].hex()
    if fingerprint != expected_fingerprint.lower():
        raise ValueError('server public key fingerprint does not match pinned identity')
    return fingerprint


class BoundedAbridgedCodec(AbridgedPacketCodec):
    async def read_packet(self, reader):
        first = (await reader.readexactly(1))[0]
        if first >= 128:
            raise ValueError('unexpected quick ack in no-account handshake')
        words = int.from_bytes(await reader.readexactly(3), 'little') if first == 127 else first
        size = words * 4
        if not 4 <= size <= MAX_PACKET:
            raise ValueError('handshake response exceeds bounded frame size')
        return await reader.readexactly(size)


class BoundedConnection(ConnectionTcpObfuscated):
    packet_codec = BoundedAbridgedCodec


async def verify_key_exchange(host, port, public_key: bytes, *, timeout=15):
    ipaddress.ip_address(host)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid port')
    if not math.isfinite(timeout) or not 0 < timeout <= 30:
        raise ValueError('invalid timeout')
    fingerprint = verify_public_key(public_key, EXPECTED_FINGERPRINT)
    normalized_key = to_pkcs1_public_key(public_key)
    # Telethon's RSA loader uses PKCS#1. Its default Telegram keys are not trusted
    # for this service; restore global library state when the diagnostic finishes.
    original_keys = rsa._server_keys.copy()
    rsa._server_keys.clear()
    rsa.add_key(normalized_key, old=False)
    loggers = defaultdict(lambda: logging.getLogger('tatocollector.keyexchange'))
    connection = BoundedConnection(host, port, 1, loggers=loggers)
    result = {
        'host': host, 'port': port, 'public_key_fingerprint': fingerprint,
        'transport': 'obfuscated_abridged', 'key_exchange_verified': False,
        'user_authenticated': False, 'session_saved': False,
        'trace': [],
    }
    start = time.monotonic()
    try:
        async def exchange():
            await connection.connect(timeout=timeout)
            result['trace'].append({'stage': 'connected'})

            class TracedPlainSender(MTProtoPlainSender):
                async def send(self, request):
                    trace = {'request': type(request).__name__, 'state': 'waiting'}
                    result['trace'].append(trace)
                    response = await super().send(request)
                    trace.update(state='received', response=type(response).__name__)
                    return response

            sender = TracedPlainSender(connection, loggers=loggers)
            auth_key, offset = await do_authentication(sender)
            if not auth_key.key or len(auth_key.key) != 256:
                raise ValueError('unexpected DH key size')
            result.update(key_exchange_verified=True, server_time_offset_seconds=offset)
            del auth_key

        await asyncio.wait_for(exchange(), timeout=timeout)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc) or 'handshake did not finish before deadline')
    finally:
        await connection.disconnect()
        rsa._server_keys.clear()
        rsa._server_keys.update(original_keys)
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(verify_key_exchange(args.host, args.port, args.public_key.read_bytes(), timeout=args.timeout))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'DH transport key only; no user login, no session persisted', 'result': result}
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + '\n', encoding='utf-8')
    print(serialized)
    return 0 if result['key_exchange_verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
