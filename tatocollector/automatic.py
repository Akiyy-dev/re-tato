"""Bounded endpoint changes, with pinned identity before reusing account keys."""

import asyncio
from collections import OrderedDict
import os
from pathlib import Path
import time

from telethon.errors import SecurityError

from .client import EndpointDiscoveryError, TatoConnection
from .discovery import Endpoint, EndpointCache, discover_endpoints, valid_endpoint
from .keyexchange import verify_key_exchange
from .native_endpoints import native_endpoints


TOTAL_TIMEOUT = 45
MAX_ATTEMPTS = 4
ATTEMPT_TIMEOUT = 10
_RECENT_FAILURES = OrderedDict()
_FAILURE_TTL = 60


def _unique(endpoints):
    result, seen = [], set()
    for endpoint in endpoints:
        if not isinstance(endpoint, Endpoint) or not valid_endpoint(endpoint.host, endpoint.port):
            continue
        key = endpoint.host, endpoint.port
        if key not in seen:
            seen.add(key)
            result.append(endpoint)
        if len(result) == 8:
            break
    return result


class AutomaticConnection(TatoConnection):
    def __init__(self, *args, endpoint_cache=Path('.data/endpoints.local.json'), **kwargs):
        super().__init__(*args, **kwargs)
        self._preferred = Endpoint(self.host, self.port, 'session')
        self._cache = EndpointCache(endpoint_cache)
        self._namespace = os.path.normcase(str(self._cache.path.absolute()))
        self._attempts = 0
        self.endpoint_status = {}

    async def __aenter__(self):
        self._attempts = 0
        try:
            return await asyncio.wait_for(self._connect_automatically(), TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            raise EndpointDiscoveryError('endpoint_discovery_timed_out', self._attempts) from None

    async def _connect_automatically(self):
        native = await asyncio.to_thread(native_endpoints)
        cached = [Endpoint(item.host, item.port, 'cache') for item in self._cache.read()]
        initial = self._ready(native[:1] + cached[:1] + [self._preferred] + native[1:] + cached[1:])
        seen = set()
        for endpoint in initial[:2]:
            seen.add((endpoint.host, endpoint.port))
            if await self._try_endpoint(endpoint):
                self._remember(endpoint, initial)
                return self
        try:
            discovered = await discover_endpoints()
        except (OSError, ValueError):
            discovered = []
        remaining = self._ready([self._preferred] + discovered + initial[2:])
        for endpoint in remaining:
            if self._attempts >= MAX_ATTEMPTS:
                break
            key = endpoint.host, endpoint.port
            if key in seen:
                continue
            seen.add(key)
            if await self._try_endpoint(endpoint):
                self._remember(endpoint, discovered + initial)
                return self
        raise EndpointDiscoveryError('endpoint_discovery_failed', self._attempts)

    def _failure_key(self, endpoint):
        return self._namespace, endpoint.host, endpoint.port

    def _ready(self, endpoints):
        now = time.monotonic()
        for key, expires in list(_RECENT_FAILURES.items()):
            if expires <= now:
                del _RECENT_FAILURES[key]
        # Filter before limiting the list, so repeated bounded reconnects can
        # reach later candidates instead of retrying the same failed prefix.
        return _unique(endpoint for endpoint in endpoints
                       if isinstance(endpoint, Endpoint)
                       and valid_endpoint(endpoint.host, endpoint.port)
                       and self._failure_key(endpoint) not in _RECENT_FAILURES)

    def _failed(self, endpoint):
        key = self._failure_key(endpoint)
        _RECENT_FAILURES[key] = time.monotonic() + _FAILURE_TTL
        _RECENT_FAILURES.move_to_end(key)
        while len(_RECENT_FAILURES) > 64:
            _RECENT_FAILURES.popitem(last=False)

    async def _try_endpoint(self, endpoint):
        self._attempts += 1
        self.host, self.port = endpoint.host, endpoint.port
        self.sender = None
        self._disconnect_future = None
        self._disconnect_error = None
        try:
            if self.auth_key:
                # Existing keys skip the SDK's normal DH exchange. Authenticate
                # the new destination with a disposable key before using them.
                result = await verify_key_exchange(self.host, self.port, self.public_key, timeout=6)
                if result.get('key_exchange_verified') is not True:
                    self._failed(endpoint)
                    return False
            await asyncio.wait_for(super().__aenter__(), ATTEMPT_TIMEOUT)
        except (OSError, ValueError, SecurityError):
            self._failed(endpoint)
            return False
        _RECENT_FAILURES.pop(self._failure_key(endpoint), None)
        self.endpoint_status = {'host': self.host, 'port': self.port, 'source': endpoint.source}
        return True

    def _remember(self, endpoint, candidates):
        try:
            self._cache.write(_unique([endpoint] + candidates))
        except (OSError, ValueError):
            # The connected session remains usable on a read-only filesystem.
            pass
