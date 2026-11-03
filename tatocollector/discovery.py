"""Bounded anonymous configuration discovery and public endpoint metadata.

Discovered addresses are untrusted candidates. The connection layer must verify
the pinned server key before sending any existing authorization material.
"""

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import time

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


UDP_SEEDS = (('34.96.149.64', 2811), ('35.225.57.235', 2811))
TCP_SEEDS = (('18.167.131.220', 2812), ('18.167.145.241', 2812))
_REQUEST_KEY = b'93C26CA2470836D2'
_RESPONSE_KEY = b'04C607A602DC026E'
_MAX_BYTES = 32 * 1024
_MAX_ENDPOINTS = 8
_SOURCES = frozenset(('bootstrap', 'bootstrap_udp', 'bootstrap_tcp',
                      'native', 'cache', 'session'))
_CACHE_TTL = 24 * 60 * 60


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    source: str = 'bootstrap'


def valid_endpoint(host, port) -> bool:
    """Accept literal public unicast IPv4 addresses and integer TCP ports."""
    if type(host) is not str or type(port) is not int or not 1 <= port <= 65535:
        return False
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        return False
    return (address.is_global and not address.is_multicast
            and not address.is_reserved and not address.is_unspecified
            and not address.is_loopback and not address.is_link_local)


def _unique_pairs(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError('Duplicate configuration field')
        document[key] = value
    return document


def _invalid_constant(_value):
    raise ValueError('Invalid configuration number')


def _json_document(data):
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=_invalid_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid configuration document') from error


def make_config_request(now=None):
    """Encode the public anonymous request; no account or local state is read."""
    document = {
        'use': 'dhc', 'v': '3.03.200105', 'phoneNov': '', 'userId': '',
        'keyId': '0', 'time': str(int(time.time() if now is None else now)),
        'connect': {'errNo': 0, 'errStr': '', 'srvHost': '', 'over': '', 'costTime': 0},
    }
    plaintext = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ciphertext = AES.new(_REQUEST_KEY, AES.MODE_CBC, iv=_REQUEST_KEY).encrypt(pad(plaintext, 16))
    return b'a' + ciphertext


def _deduplicate(endpoints):
    result = []
    seen = set()
    for endpoint in endpoints:
        if (endpoint.host, endpoint.port) not in seen:
            seen.add((endpoint.host, endpoint.port))
            result.append(endpoint)
    return result


def decode_config_response(data, source='bootstrap_udp') -> list[Endpoint]:
    """Validate a complete raw CBC reply, without promoting its candidates to trust."""
    if not 16 <= len(data) <= _MAX_BYTES or len(data) % 16:
        raise ValueError('Invalid configuration size')
    if source not in _SOURCES:
        raise ValueError('Invalid endpoint source')
    plaintext = AES.new(_RESPONSE_KEY, AES.MODE_CBC, iv=_RESPONSE_KEY).decrypt(data)
    document = _json_document(unpad(plaintext, 16).rstrip(b'\x00'))
    if type(document) is not dict:
        raise ValueError('Configuration must be an object')
    over = document.get('over', '')
    if over not in ('', 'tcp', 'tls'):
        raise ValueError('Unsupported configuration transport')
    items = document.get('ip', [])
    if type(items) is not list:
        raise ValueError('Invalid configuration addresses')
    if not items:
        items = document.get('host', [])
    if type(items) is not list or len(items) > _MAX_ENDPOINTS:
        raise ValueError('Invalid configuration addresses')
    endpoints = []
    for item in items:
        if type(item) is not str or len(item) > 21 or item.count(':') != 1:
            raise ValueError('Invalid configuration address')
        host, port_text = item.split(':')
        if (not port_text or len(port_text) > 5
                or any(character not in '0123456789' for character in port_text)):
            raise ValueError('Invalid configuration port')
        port = int(port_text)
        if not valid_endpoint(host, port):
            raise ValueError('Invalid public endpoint')
        endpoints.append(Endpoint(host, port, source))
    return [] if over == 'tls' else _deduplicate(endpoints)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Configuration request expired')
    return remaining


def _udp_query(seed, request, timeout):
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as transport:
        transport.settimeout(_remaining(deadline))
        transport.connect(seed)
        transport.send(request)
        transport.settimeout(_remaining(deadline))
        data = transport.recv(_MAX_BYTES + 1)
    return decode_config_response(data, 'bootstrap_udp')


def _tcp_query(seed, request, timeout):
    deadline = time.monotonic() + timeout
    with socket.create_connection(seed, timeout=_remaining(deadline)) as transport:
        transport.settimeout(_remaining(deadline))
        transport.sendall(request)
        data = bytearray()
        while len(data) <= _MAX_BYTES:
            transport.settimeout(_remaining(deadline))
            piece = transport.recv(min(4096, _MAX_BYTES + 1 - len(data)))
            if not piece:
                return []
            data.extend(piece)
            if len(data) > _MAX_BYTES:
                return []
            if len(data) % 16 == 0:
                try:
                    return decode_config_response(bytes(data), 'bootstrap_tcp')
                except ValueError:
                    # TCP is a stream: an aligned fragment may still be incomplete.
                    continue
    return []


async def discover_endpoints(timeout=3.0) -> list[Endpoint]:
    """Try bounded UDP sources first, then TCP; failure of one source is local."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        return []
    timeout = min(float(timeout), 3.0)
    request = make_config_request()
    for seeds, query in ((UDP_SEEDS, _udp_query), (TCP_SEEDS, _tcp_query)):
        for seed in seeds:
            try:
                endpoints = await asyncio.to_thread(query, seed, request, timeout)
            except (OSError, ValueError):
                continue
            if endpoints:
                return endpoints
    return []


def _validate_metadata(endpoints):
    values = []
    for endpoint in endpoints:
        if len(values) >= _MAX_ENDPOINTS:
            raise ValueError('Too many cached endpoints')
        if (not isinstance(endpoint, Endpoint)
                or not valid_endpoint(endpoint.host, endpoint.port)
                or type(endpoint.source) is not str or endpoint.source not in _SOURCES):
            raise ValueError('Invalid cached endpoint')
        values.append(endpoint)
    return _deduplicate(values)


class EndpointCache:
    """An atomic, expiring metadata cache separate from account/session state."""

    def __init__(self, path=Path('.data/endpoints.local.json'), clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def read(self) -> list[Endpoint]:
        try:
            with self.path.open('rb') as source:
                data = source.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                return []
            document = _json_document(data)
            if (type(document) is not dict
                    or set(document) != {'version', 'updated_at', 'endpoints'}
                    or type(document['version']) is not int or document['version'] != 1):
                return []
            updated = document['updated_at']
            now = self.clock()
            if (type(updated) not in (int, float) or not math.isfinite(updated)
                    or not math.isfinite(now) or updated < 0
                    or not 0 <= now - updated < _CACHE_TTL):
                return []
            records = document['endpoints']
            if type(records) is not list or len(records) > _MAX_ENDPOINTS:
                return []
            endpoints = []
            for item in records:
                if type(item) is not dict or set(item) != {'host', 'port', 'source'}:
                    return []
                endpoints.append(Endpoint(**item))
            return _validate_metadata(endpoints)
        except (OSError, ValueError, TypeError, OverflowError):
            return []

    def write(self, endpoints) -> None:
        endpoints = _validate_metadata(endpoints)
        updated = self.clock()
        if type(updated) not in (int, float) or not math.isfinite(updated) or updated < 0:
            raise ValueError('Invalid cache timestamp')
        document = {
            'version': 1, 'updated_at': updated,
            'endpoints': [{'host': value.host, 'port': value.port, 'source': value.source}
                          for value in endpoints],
        }
        contents = json.dumps(document, allow_nan=False, separators=(',', ':')).encode('utf-8')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.' + self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'wb') as target:
                target.write(contents)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
