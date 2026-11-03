import asyncio
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


try:
    discovery = importlib.import_module('tatocollector.discovery')
except ModuleNotFoundError:
    discovery = None


def encrypted(document=None, *, raw=None):
    """Synthetic protocol fixture independent of the production codec."""
    plaintext = raw if raw is not None else json.dumps(document).encode('utf-8')
    key = b'04C607A602DC026E'
    return AES.new(key, AES.MODE_CBC, iv=key).encrypt(pad(plaintext, 16))


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(discovery, 'automatic endpoint discovery is not implemented')


class DiscoveryTests(ModuleTests):
    def test_request_is_anonymous_public_protocol_without_length_prefix(self):
        request = discovery.make_config_request(now=1700000000)
        self.assertEqual(request[:1], b'a')
        key = b'93C26CA2470836D2'
        plaintext = unpad(AES.new(key, AES.MODE_CBC, iv=key).decrypt(request[1:]), 16)
        self.assertEqual(json.loads(plaintext), {
            'use': 'dhc', 'v': '3.03.200105', 'phoneNov': '', 'userId': '',
            'keyId': '0', 'time': '1700000000',
            'connect': {'errNo': 0, 'errStr': '', 'srvHost': '', 'over': '', 'costTime': 0},
        })

    def test_host_only_response_defaults_to_tcp_and_deduplicates(self):
        result = discovery.decode_config_response(encrypted({
            'host': ['8.8.8.8:443', '8.8.8.8:443', '9.9.9.9:20334'],
        }))
        self.assertEqual(result, [discovery.Endpoint('8.8.8.8', 443, 'bootstrap_udp'),
                                  discovery.Endpoint('9.9.9.9', 20334, 'bootstrap_udp')])

    def test_nonempty_ip_list_takes_precedence_over_host_list(self):
        result = discovery.decode_config_response(encrypted({
            'ip': ['8.8.8.8:2812'], 'host': ['9.9.9.9:443'], 'over': 'tcp',
        }), source='bootstrap_tcp')
        self.assertEqual(result, [discovery.Endpoint('8.8.8.8', 2812, 'bootstrap_tcp')])
        self.assertEqual(discovery.decode_config_response(encrypted({
            'ip': [], 'host': ['9.9.9.9:443'],
        })), [discovery.Endpoint('9.9.9.9', 443, 'bootstrap_udp')])

    def test_trailing_nul_is_removed_after_pkcs7(self):
        self.assertEqual(discovery.decode_config_response(encrypted(
            raw=b'{"host":["8.8.8.8:443"]}\x00\x00')),
            [discovery.Endpoint('8.8.8.8', 443, 'bootstrap_udp')])

    def test_explicit_tls_never_becomes_a_tcp_candidate(self):
        self.assertEqual(discovery.decode_config_response(encrypted({
            'host': ['8.8.8.8:443'], 'over': 'tls',
        })), [])

    def test_response_rejects_malformed_unbounded_and_ambiguous_data(self):
        invalid = [b'', b'a' * 15, b'a' * 17, b'a' * (32768 + 16),
                   encrypted(raw=b'[]'), encrypted(raw=b'{"ip":[],"ip":[]}'),
                   encrypted(raw=b'{"host":[],"nested":{"x":1,"x":2}}'),
                   encrypted(raw=b'{"host":[],"number":NaN}'),
                   encrypted(raw=b'not json'), encrypted({'ip': '8.8.8.8:443'}),
                   encrypted({'host': ['8.8.8.8:443'] * 9}),
                   encrypted({'ip': ['8.8.8.8:443'] * 9, 'host': []}),
                   encrypted({'host': ['8.8.8.8:443'], 'over': 'quic'})]
        # Deterministically invalid final padding byte, rather than random corruption.
        key = b'04C607A602DC026E'
        invalid.append(AES.new(key, AES.MODE_CBC, iv=key).encrypt(b'\x00' * 16))
        for value in invalid:
            with self.subTest(length=len(value)):
                with self.assertRaises(ValueError):
                    discovery.decode_config_response(value)

    def test_endpoint_validation_rejects_nonpublic_hosts_and_noninteger_ports(self):
        rejected = ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1',
                    '169.254.1.1', '100.64.0.1', '0.0.0.0', '224.0.0.1',
                    '255.255.255.255', '192.0.2.1', '198.51.100.1', '203.0.113.1',
                    '::1', 'example.invalid', '008.008.008.008', '8.8.8.8 ']
        for host in rejected:
            with self.subTest(host=host):
                self.assertFalse(discovery.valid_endpoint(host, 443))
                with self.assertRaises(ValueError):
                    discovery.decode_config_response(encrypted({'host': [host + ':443']}))
        for port in [0, -1, 65536, True, '443', 443.0, None]:
            with self.subTest(port=port):
                self.assertFalse(discovery.valid_endpoint('8.8.8.8', port))
        for value in ['8.8.8.8:0', '8.8.8.8:65536', '8.8.8.8:+443',
                      '8.8.8.8: 443', '8.8.8.8:４４３', '8.8.8.8:443:1', 42]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                discovery.decode_config_response(encrypted({'host': [value]}))
        self.assertTrue(discovery.valid_endpoint('8.8.8.8', 1))
        self.assertTrue(discovery.valid_endpoint('9.9.9.9', 65535))


class CacheTests(ModuleTests):
    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'cache.json'
        self.cache = discovery.EndpointCache(self.path, clock=lambda: 200000.0)

    def test_cache_round_trip_contains_only_valid_metadata(self):
        endpoints = [discovery.Endpoint('8.8.8.8', 443, 'native'),
                     discovery.Endpoint('9.9.9.9', 2812, 'bootstrap_tcp')]
        self.cache.write(endpoints + endpoints[:1])
        self.assertEqual(self.cache.read(), endpoints)
        self.assertEqual(json.loads(self.path.read_text()), {
            'version': 1, 'updated_at': 200000.0,
            'endpoints': [{'host': '8.8.8.8', 'port': 443, 'source': 'native'},
                          {'host': '9.9.9.9', 'port': 2812, 'source': 'bootstrap_tcp'}],
        })

    def test_missing_corrupt_old_future_and_incorrect_schema_cache_are_ignored(self):
        self.assertEqual(self.cache.read(), [])
        valid = {'version': 1, 'updated_at': 200000.0,
                 'endpoints': [{'host': '8.8.8.8', 'port': 443, 'source': 'cache'}]}
        invalid = [b'not json', b'{"version":1,"version":1}', b'[]',
                   json.dumps({**valid, 'updated_at': 200000.0 - 86400}).encode(),
                   json.dumps({**valid, 'updated_at': 200001.0}).encode(),
                   json.dumps({**valid, 'updated_at': float('nan')}).encode(),
                   json.dumps({**valid, 'version': True}).encode(),
                   json.dumps({**valid, 'extra': 'private'}).encode(),
                   json.dumps({**valid, 'endpoints': valid['endpoints'] * 9}).encode(),
                   json.dumps({**valid, 'endpoints': [{'host': '127.0.0.1', 'port': 443,
                                                       'source': 'cache'}]}).encode(),
                   json.dumps({**valid, 'endpoints': [{'host': '8.8.8.8', 'port': 443,
                                                       'source': 'account secret'}]}).encode(),
                   json.dumps({**valid, 'endpoints': [{'host': '8.8.8.8', 'port': 443,
                                                       'source': 'cache', 'user_id': 42}]}).encode(),
                   b' ' * 32769]
        for contents in invalid:
            with self.subTest(contents=contents[:32]):
                self.path.write_bytes(contents)
                self.assertEqual(self.cache.read(), [])
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.cache.read(), [])

    def test_failed_atomic_replace_preserves_old_cache_and_removes_temporary(self):
        first = [discovery.Endpoint('8.8.8.8', 443, 'session')]
        self.cache.write(first)
        original = self.path.read_bytes()
        with patch.object(os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.cache.write([discovery.Endpoint('9.9.9.9', 443, 'cache')])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_atomic_write_creates_parent_and_rejects_invalid_or_excess_metadata(self):
        path = self.path.parent / 'new' / 'cache.json'
        cache = discovery.EndpointCache(path, clock=lambda: 200000.0)
        cache.write([discovery.Endpoint('8.8.8.8', 443)])
        self.assertEqual(cache.read(), [discovery.Endpoint('8.8.8.8', 443)])
        for endpoints in [[discovery.Endpoint('127.0.0.1', 443)],
                          [discovery.Endpoint('8.8.8.8', 443, 'unknown')],
                          [discovery.Endpoint('8.8.8.8', 443)] * 9]:
            with self.assertRaises(ValueError):
                self.cache.write(endpoints)


@contextmanager
def loopback_server(kind, reply, *, fragments=False, stall=False, drip=False):
    """Only a controlled local server, never an external service."""
    server = socket.socket(socket.AF_INET, kind)
    server.bind(('127.0.0.1', 0))
    server.settimeout(2)
    if kind == socket.SOCK_STREAM:
        server.listen(1)
    stop = threading.Event()
    requests = []
    errors = []

    def serve():
        try:
            if kind == socket.SOCK_DGRAM:
                request, remote = server.recvfrom(32769)
                requests.append(request)
                if not stall:
                    server.sendto(reply, remote)
                else:
                    stop.wait(1)
            else:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(2)
                    request = bytearray()
                    while not request or (len(request) - 1) % 16:
                        request.extend(connection.recv(4096))
                    requests.append(bytes(request))
                    if stall:
                        stop.wait(1)
                    elif drip:
                        for _ in range(20):
                            connection.sendall(b'x' * 16)
                            if stop.wait(0.02):
                                break
                    elif fragments:
                        # First complete AES blocks are intentionally not a complete JSON reply.
                        for piece in (reply[:16], reply[16:23], reply[23:]):
                            connection.sendall(piece)
                            time.sleep(0.02)
                        stop.wait(1)  # A successful client must not wait for EOF.
                    else:
                        connection.sendall(reply)
        except (OSError, ValueError) as error:
            if not stop.is_set():
                errors.append(error)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server.getsockname(), requests
    finally:
        stop.set()
        server.close()
        thread.join(2.5)
        if errors:
            raise AssertionError('local fixture server failed') from errors[0]


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(discovery, 'automatic endpoint discovery is not implemented')

    async def test_udp_returns_first_usable_reply_without_tcp_fallback(self):
        reply = encrypted({'host': ['8.8.8.8:443']})
        with loopback_server(socket.SOCK_DGRAM, reply) as (seed, requests):
            with patch.object(discovery, 'UDP_SEEDS', (seed,)), \
                    patch.object(discovery, 'TCP_SEEDS', (('127.0.0.1', 1),)):
                result = await discovery.discover_endpoints(timeout=0.5)
        self.assertEqual(result, [discovery.Endpoint('8.8.8.8', 443, 'bootstrap_udp')])
        self.assertEqual(requests[0][:1], b'a')

    async def test_bad_udp_source_does_not_prevent_later_valid_source(self):
        with loopback_server(socket.SOCK_DGRAM, b'invalid') as (bad, _), \
                loopback_server(socket.SOCK_DGRAM, encrypted({'host': ['9.9.9.9:443']})) as (good, _):
            with patch.object(discovery, 'UDP_SEEDS', (bad, good)), \
                    patch.object(discovery, 'TCP_SEEDS', ()):
                result = await discovery.discover_endpoints(timeout=0.5)
        self.assertEqual(result, [discovery.Endpoint('9.9.9.9', 443, 'bootstrap_udp')])

    async def test_tcp_fallback_accumulates_fragments_and_returns_before_eof(self):
        reply = encrypted({'host': ['9.9.9.9:20334']})
        with loopback_server(socket.SOCK_DGRAM, b'invalid') as (udp, _), \
                loopback_server(socket.SOCK_STREAM, reply, fragments=True) as (tcp, requests):
            with patch.object(discovery, 'UDP_SEEDS', (udp,)), \
                    patch.object(discovery, 'TCP_SEEDS', (tcp,)):
                result = await asyncio.wait_for(discovery.discover_endpoints(timeout=0.4), 0.7)
        self.assertEqual(result, [discovery.Endpoint('9.9.9.9', 20334, 'bootstrap_tcp')])
        self.assertEqual(requests[0][:1], b'a')

    async def test_oversized_udp_and_tcp_reply_are_rejected(self):
        for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            with self.subTest(kind=kind):
                with loopback_server(kind, b'x' * 32769) as (seed, _):
                    with patch.object(discovery, 'UDP_SEEDS', (seed,) if kind == socket.SOCK_DGRAM else ()), \
                            patch.object(discovery, 'TCP_SEEDS', (seed,) if kind == socket.SOCK_STREAM else ()):
                        self.assertEqual(await discovery.discover_endpoints(timeout=0.3), [])

    async def test_stalled_source_times_out_without_blocking_event_loop(self):
        for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
            with self.subTest(kind=kind):
                with loopback_server(kind, b'', stall=True) as (seed, _):
                    with patch.object(discovery, 'UDP_SEEDS', (seed,) if kind == socket.SOCK_DGRAM else ()), \
                            patch.object(discovery, 'TCP_SEEDS', (seed,) if kind == socket.SOCK_STREAM else ()):
                        ticks = []

                        async def heartbeat():
                            for _ in range(3):
                                await asyncio.sleep(0.015)
                                ticks.append(True)

                        result, _ = await asyncio.wait_for(asyncio.gather(
                            discovery.discover_endpoints(timeout=0.1), heartbeat()), 0.5)
                        self.assertEqual(result, [])
                        self.assertEqual(len(ticks), 3)

    async def test_partial_tcp_traffic_cannot_extend_absolute_deadline(self):
        with loopback_server(socket.SOCK_STREAM, b'', drip=True) as (seed, _):
            with patch.object(discovery, 'UDP_SEEDS', ()), \
                    patch.object(discovery, 'TCP_SEEDS', (seed,)):
                result = await asyncio.wait_for(discovery.discover_endpoints(timeout=0.1), 0.3)
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
