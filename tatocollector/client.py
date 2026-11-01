"""Low-level Tatotalk connection; deliberately does not instantiate TelegramClient."""

import argparse
import asyncio
from collections import defaultdict
import json
import logging
from pathlib import Path
import struct
import traceback

from telethon.crypto import AuthKey, rsa
from telethon.network.mtprotosender import MTProtoSender
from telethon.tl.tlobject import TLRequest

from .authwire import AuthProtocolError, wrap_init
from .compression import BoundedGzip
from .keyexchange import (BoundedConnection, EXPECTED_FINGERPRINT,
                          to_pkcs1_public_key, verify_public_key)


_SERVICE_HOST = '15.197.179.83'
_SERVICE_PORT = 20334


_PUBLIC_RPC_ERRORS = frozenset({
    'BAD_REQUEST', 'UNAUTHORIZED', 'FORBIDDEN', 'NOT_FOUND', 'INTERNAL', 'FLOOD',
    'AUTH_KEY_UNREGISTERED', 'AUTH_KEY_DUPLICATED', 'SESSION_REVOKED', 'SESSION_EXPIRED',
    'SESSION_PASSWORD_NEEDED', 'PHONE_CODE_INVALID', 'PHONE_CODE_EXPIRED', 'PHONE_CODE_EMPTY',
    'PHONE_CODE_HASH_EMPTY', 'PHONE_HASH_EXPIRED', 'EMAIL_INVALID', 'EMAIL_CODE_INVALID',
    'EMAIL_CODE_EXPIRED', 'EMAIL_CODE_EMPTY', 'EMAIL_CODE_HASH_EMPTY', 'PHONE_NUMBER_INVALID',
    'PHONE_NUMBER_UNOCCUPIED', 'API_ID_INVALID', 'API_ID_PUBLISHED_FLOOD', 'FLOOD_WAIT',
    'EMAIL_NOT_ALLOWED', 'PASSWORD_HASH_INVALID', 'SRP_ID_INVALID', 'AUTH_RESTART',
    'INTERNAL_SERVER_ERROR', 'LANG_CODE_INVALID', 'CONNECTION_LAYER_INVALID', 'INPUT_LAYER_INVALID',
    'CAPTCHA_REQUIRED', 'CAPTCHA_INVALID', 'CAPTCHA_EXPIRED', 'CAPTCHA_TOKEN_INVALID',
    'CAPTCHA_ANSWER_INVALID',
})


class RawRequest(TLRequest):
    SUBCLASS_OF_ID = 0

    def __init__(self, wire: bytes, decoder):
        if len(wire) < 4:
            raise ValueError('RPC wire must include constructor')
        self.CONSTRUCTOR_ID = struct.unpack_from('<I', wire)[0]
        self._wire = wire
        self._decoder = decoder

    def _bytes(self):
        return self._wire

    def read_result(self, reader):
        return self._decoder(reader.read())

    def to_dict(self):
        return {'_': 'TatoRawRequest', 'constructor': f'{self.CONSTRUCTOR_ID:08x}'}


class TatoConnection:
    def __init__(self, profile, public_key, *, host=_SERVICE_HOST, port=_SERVICE_PORT,
                 dc_id=1, auth_key=None, time_offset=0, updates_queue=None):
        verify_public_key(public_key, EXPECTED_FINGERPRINT)
        self.profile = profile
        self.public_key = public_key
        # Old sessions/checkpoints retain the retired endpoint. Migrate only
        # this verified replacement; keep credentials and custom endpoints.
        if (host, port) == ('15.197.252.89', 20334):
            host, port = _SERVICE_HOST, _SERVICE_PORT
        self.host, self.port, self.dc_id = host, port, dc_id
        self.time_offset = time_offset
        self.auth_key = auth_key
        self.updates_queue = updates_queue
        self.sender = None
        self._original_keys = None
        self._gzip = None
        self._disconnect_future = None
        self._disconnect_error = None

    def _observe_disconnect(self):
        # The public property returns a shielded Future. Own exactly one for
        # this connection, and retrieve its exception even during idle periods.
        if self.sender is not None and self._disconnect_future is None:
            self._disconnect_future = self.sender.disconnected
            self._disconnect_future.add_done_callback(self._record_disconnect)

    def _record_disconnect(self, future):
        if not future.cancelled():
            self._disconnect_error = future.exception()

    def check_connected(self):
        if self._disconnect_future is not None and self._disconnect_future.done():
            self._record_disconnect(self._disconnect_future)
        if self._disconnect_error is not None:
            raise self._disconnect_error
        if self.sender is None or not self.sender.is_connected():
            raise ConnectionError('transport_disconnected')

    async def __aenter__(self):
        self._original_keys = rsa._server_keys.copy()
        rsa._server_keys.clear()
        rsa.add_key(to_pkcs1_public_key(self.public_key), old=False)
        loggers = defaultdict(lambda: logging.getLogger('tatocollector.transport'))
        self.sender = MTProtoSender(AuthKey(self.auth_key), loggers=loggers,
                                    retries=1, auto_reconnect=False, connect_timeout=10,
                                    updates_queue=self.updates_queue)
        self.sender._state.time_offset = self.time_offset
        try:
            self._gzip = BoundedGzip()
            self._gzip.__enter__()
            await asyncio.wait_for(self.sender.connect(
                BoundedConnection(self.host, self.port, self.dc_id, loggers=loggers)), timeout=25)
            self._observe_disconnect()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def call(self, wire, decoder=bytes, *, timeout=20):
        self.check_connected()
        request = RawRequest(wrap_init(wire, self.profile), decoder)
        return await asyncio.wait_for(self.sender.send(request), timeout=timeout)

    def transport_state(self):
        return {
            'auth_key_hex': self.sender.auth_key.key.hex(),
            'host': self.host, 'port': self.port, 'dc_id': self.dc_id,
            'time_offset': self.sender._state.time_offset,
        }

    def session_state(self, user_id):
        return {**self.transport_state(), 'user_id': user_id}

    async def __aexit__(self, *_):
        try:
            if self.sender is not None:
                # Failed setup can also leave an exceptional sender Future.
                self._observe_disconnect()
                await self.sender.disconnect()
        finally:
            if self._disconnect_future is not None and self._disconnect_future.done():
                self._record_disconnect(self._disconnect_future)
            try:
                if self._original_keys is not None:
                    rsa._server_keys.clear()
                    rsa._server_keys.update(self._original_keys)
                    self._original_keys = None
            finally:
                if self._gzip is not None:
                    self._gzip.__exit__(None, None, None)
                    self._gzip = None


def safe_error(exc):
    error = {'error_type': type(exc).__name__}
    if isinstance(exc, OSError):
        for name in ('errno', 'winerror'):
            value = getattr(exc, name, None)
            if type(value) is int:
                error[name] = value
    if isinstance(exc, AuthProtocolError):
        error.update(exc.diagnostic)
    if hasattr(exc, 'code'):
        error['rpc_code'] = exc.code
    if hasattr(exc, 'message'):
        message = exc.message
        if type(message) is str and message in _PUBLIC_RPC_ERRORS:
            error['rpc_message'] = message
        elif message is not None:
            error['rpc_message_redacted'] = True
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        frame = frames[-1]
        error['error_location'] = f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}'
    return error


async def probe_initialization(profile, public_key):
    async with TatoConnection(profile, public_key) as connection:
        # help.getConfig is a read-only public configuration request. Decode only
        # its outer identity, not unknown config fields or any account data.
        data = await connection.call(struct.pack('<I', 0xC4F9186B))
        if len(data) < 4:
            raise ValueError('empty configuration RPC result')
        return {'encrypted_rpc_received': True, 'layer': profile['layer'],
                'config_constructor': f'{struct.unpack_from("<I", data)[0]:08x}',
                'response_bytes': len(data), 'user_authenticated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Probe encrypted app initialization, without requesting user login')
    parser.add_argument('--profile', type=Path, default=Path('config/client-profile.local.json'))
    parser.add_argument('--public-key', type=Path, default=Path('research/android-static/embedded-public-0.pem'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(probe_initialization(json.loads(args.profile.read_text(encoding='utf-8')),
                                                   args.public_key.read_bytes()))
    except Exception as exc:
        result = {'encrypted_rpc_received': False, 'user_authenticated': False, **safe_error(exc)}
    report = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + '\n', encoding='utf-8')
    print(report)
    return 0 if result['encrypted_rpc_received'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
