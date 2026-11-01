import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class EndpointMigrationTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def offline_connection(self, **transport):
        from telethon.network.mtprotosender import MTProtoSender
        from tatocollector.client import TatoConnection

        async def connect(sender):
            # Keep the real sender and transport setup, but never open sockets.
            sender._MTProtoSender__disconnected = asyncio.get_running_loop().create_future()

        with patch.object(MTProtoSender, '_connect', connect):
            async with TatoConnection(
                    {'layer': 123, 'api_id': 1, 'app_version': 'synthetic'},
                    Path('research/android-static/embedded-public-0.pem').read_bytes(),
                    **transport) as connection:
                yield connection

    async def test_new_login_connects_to_the_verified_current_endpoint(self):
        async with self.offline_connection() as connection:
            transport = connection.sender._connection
            self.assertEqual((transport._ip, transport._port), ('15.197.179.83', 20334))

    async def test_saved_transport_migrates_without_changing_session_material(self):
        for kind in ('pending', 'verified'):
            with self.subTest(kind=kind):
                saved = {
                    'auth_key_hex': bytes(range(256)).hex(),
                    'host': '15.197.252.89', 'port': 20334,
                    'dc_id': 1, 'time_offset': 37,
                }
                if kind == 'verified':
                    saved['user_id'] = 123
                original = dict(saved)
                async with self.offline_connection(
                        host=saved['host'], port=saved['port'], dc_id=saved['dc_id'],
                        auth_key=bytes.fromhex(saved['auth_key_hex']),
                        time_offset=saved['time_offset']) as connection:
                    transport = connection.sender._connection
                    self.assertEqual((transport._ip, transport._port), ('15.197.179.83', 20334))
                    actual = (connection.session_state(saved['user_id'])
                              if kind == 'verified' else connection.transport_state())
                    self.assertEqual(actual, {**original, 'host': '15.197.179.83'})
                self.assertEqual(saved, original)

    async def test_other_explicit_endpoints_are_preserved(self):
        for host, port in (('127.0.0.1', 12345), ('15.197.252.89', 20335),
                           ('15.197.179.83', 20334)):
            with self.subTest(host=host, port=port):
                async with self.offline_connection(host=host, port=port) as connection:
                    transport = connection.sender._connection
                    self.assertEqual((transport._ip, transport._port), (host, port))


if __name__ == '__main__':
    unittest.main()
