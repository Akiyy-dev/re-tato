import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class AutomaticConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / 'endpoints.json'
        self.key = bytes(range(256))

    def connection(self, **kwargs):
        from tatocollector.automatic import AutomaticConnection
        return AutomaticConnection(
            {'layer': 123, 'api_id': 1, 'app_version': 'synthetic'},
            Path('research/android-static/embedded-public-0.pem').read_bytes(),
            host='9.9.9.9', port=20334, auth_key=self.key, time_offset=31,
            endpoint_cache=self.cache, **kwargs)

    @contextmanager
    def offline_sources(self, native=(), discovered=(), *, refused=(), verifier=None):
        from telethon.network.mtprotosender import MTProtoSender
        attempts = []

        async def connect(sender):
            endpoint = (sender._connection._ip, sender._connection._port)
            attempts.append(endpoint)
            sender._MTProtoSender__disconnected = asyncio.get_running_loop().create_future()
            if endpoint in refused:
                raise ConnectionRefusedError('synthetic private detail')

        with (patch('tatocollector.automatic.native_endpoints', return_value=list(native)),
              patch('tatocollector.automatic.discover_endpoints', new=AsyncMock(return_value=list(discovered))),
              patch('tatocollector.automatic.verify_key_exchange', new=verifier or AsyncMock(
                  return_value={'key_exchange_verified': True})),
              patch.object(MTProtoSender, '_connect', connect)):
            yield attempts

    async def test_native_address_change_preserves_account_transport_material(self):
        from tatocollector.discovery import Endpoint, EndpointCache
        with self.offline_sources(native=[Endpoint('8.8.8.8', 20334, 'native')]) as attempts:
            async with self.connection() as connection:
                self.assertEqual(connection.transport_state(), {
                    'host': '8.8.8.8', 'port': 20334, 'dc_id': 1,
                    'auth_key_hex': self.key.hex(), 'time_offset': 31})
                self.assertEqual(connection.endpoint_status['source'], 'native')
        self.assertEqual(attempts, [('8.8.8.8', 20334)])
        self.assertEqual(EndpointCache(self.cache).read()[0].host, '8.8.8.8')

    async def test_unverified_candidates_never_receive_the_existing_auth_key(self):
        from tatocollector.discovery import Endpoint

        async def verify(host, *_args, **_kwargs):
            return {'key_exchange_verified': host == '1.1.1.1'}

        with self.offline_sources(
                native=[Endpoint('8.8.8.8', 20334, 'native')],
                discovered=[Endpoint('1.1.1.1', 443, 'bootstrap_udp')], verifier=verify) as attempts:
            async with self.connection() as connection:
                self.assertEqual((connection.host, connection.port), ('1.1.1.1', 443))
                self.assertEqual(connection.endpoint_status['source'], 'bootstrap_udp')
        self.assertEqual(attempts, [('1.1.1.1', 443)])

    async def test_connection_failure_cleans_up_before_using_cached_address(self):
        from telethon.crypto import rsa
        from tatocollector.discovery import Endpoint, EndpointCache
        EndpointCache(self.cache).write([Endpoint('8.8.4.4', 443, 'native')])
        original = rsa._server_keys.copy()
        with self.offline_sources(native=[Endpoint('8.8.8.8', 20334, 'native')],
                                  refused=[('8.8.8.8', 20334)]) as attempts:
            async with self.connection() as connection:
                self.assertEqual(connection.host, '8.8.4.4')
                self.assertEqual(connection.endpoint_status['source'], 'cache')
        self.assertEqual(attempts, [('8.8.8.8', 20334), ('8.8.4.4', 443)])
        self.assertEqual(rsa._server_keys, original)

    async def test_public_configuration_recovers_when_no_original_client_is_running(self):
        from tatocollector.discovery import Endpoint
        with self.offline_sources(discovered=[Endpoint('1.1.1.1', 443, 'bootstrap_tcp')],
                                  refused=[('9.9.9.9', 20334)]):
            async with self.connection() as connection:
                self.assertEqual((connection.host, connection.port), ('1.1.1.1', 443))
                self.assertEqual(connection.endpoint_status['source'], 'bootstrap_tcp')

    async def test_working_saved_session_is_not_starved_by_a_long_bootstrap_list(self):
        from tatocollector.discovery import Endpoint, EndpointCache
        EndpointCache(self.cache).write([Endpoint('8.8.4.4', 443, 'cache')])
        discovered = [Endpoint(f'1.1.1.{i}', 443, 'bootstrap_udp') for i in range(1, 9)]
        refused = [('8.8.8.8', 20334), ('8.8.4.4', 443)]
        refused += [(item.host, item.port) for item in discovered]
        with self.offline_sources(native=[Endpoint('8.8.8.8', 20334, 'native')],
                                  discovered=discovered, refused=refused):
            async with self.connection() as connection:
                self.assertEqual(connection.host, '9.9.9.9')

    async def test_later_reconnect_reaches_candidates_beyond_the_first_attempt_budget(self):
        from tatocollector.client import EndpointDiscoveryError
        from tatocollector.discovery import Endpoint
        nodes = [Endpoint(f'8.8.8.{i}', 20334, 'native') for i in range(1, 9)]
        refused = [('9.9.9.9', 20334)] + [(item.host, item.port) for item in nodes[:3]]
        with self.offline_sources(native=nodes, refused=refused):
            with self.assertRaises(EndpointDiscoveryError):
                async with self.connection():
                    self.fail('The first four candidates are unavailable')
            async with self.connection() as connection:
                self.assertEqual(connection.host, '8.8.8.4')

    async def test_cache_write_failure_does_not_discard_a_working_connection(self):
        from tatocollector.discovery import Endpoint
        with (self.offline_sources(native=[Endpoint('8.8.8.8', 20334, 'native')]),
              patch('tatocollector.automatic.EndpointCache.write', side_effect=OSError('synthetic'))):
            async with self.connection() as connection:
                self.assertEqual(connection.host, '8.8.8.8')

    async def test_candidate_attempts_are_bounded_and_failure_status_is_safe(self):
        from tatocollector.client import EndpointDiscoveryError, safe_error
        from tatocollector.discovery import Endpoint
        rejected = AsyncMock(return_value={'key_exchange_verified': False})
        nodes = [Endpoint(f'8.8.8.{i}', 20334, 'native') for i in range(1, 9)]
        with self.offline_sources(native=nodes, discovered=nodes, verifier=rejected) as attempts:
            with self.assertRaises(EndpointDiscoveryError) as caught:
                async with self.connection():
                    self.fail('No candidate passed the pinned key check')
        details = safe_error(caught.exception)
        self.assertEqual(details['connection_attempts'], 4)
        self.assertEqual(details['reason_code'], 'endpoint_discovery_failed')
        self.assertEqual(attempts, [])
        self.assertNotIn('synthetic private', str(details))

    async def test_cancelled_verification_does_not_become_an_ordinary_retry(self):
        from tatocollector.discovery import Endpoint
        started = asyncio.Event()

        async def verify(*_args, **_kwargs):
            started.set()
            await asyncio.Future()

        with self.offline_sources(native=[Endpoint('8.8.8.8', 20334, 'native')], verifier=verify):
            task = asyncio.create_task(self.connection().__aenter__())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_total_deadline_cancels_a_stalled_source(self):
        from tatocollector.client import EndpointDiscoveryError, safe_error

        async def verify(*_args, **_kwargs):
            await asyncio.Future()

        with (self.offline_sources(verifier=verify),
              patch('tatocollector.automatic.TOTAL_TIMEOUT', 0.02)):
            with self.assertRaises(EndpointDiscoveryError) as caught:
                async with self.connection():
                    self.fail('The stalled source must respect the total deadline')
        self.assertEqual(safe_error(caught.exception)['reason_code'], 'endpoint_discovery_timed_out')


if __name__ == '__main__':
    unittest.main()
