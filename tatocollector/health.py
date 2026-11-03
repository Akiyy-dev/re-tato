"""Independent, read-only business RPC health checks and worker supervision."""

import asyncio
from datetime import datetime, timezone
import inspect
import math
import time


_HEALTH_REASONS = frozenset({
    'connection_health_timeout',
    'connection_health_rpc_failed',
    'connection_health_invalid_response',
})
_GET_STATE = bytes.fromhex('2a88d4ed')
_STATE_FIELDS = ('pts', 'qts', 'date', 'seq', 'unread_count')


class HealthCheckError(ConnectionError):
    def __init__(self, reason_code):
        if type(reason_code) is not str or reason_code not in _HEALTH_REASONS:
            raise ValueError('invalid health reason')
        super().__init__(reason_code)
        self.reason_code = reason_code


def _positive_seconds(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('health timing must be finite and positive')
    return float(value)


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _check(connection, schema, timeout):
    try:
        connection.check_connected()
        # Bound even implementations that do not enforce their timeout argument.
        raw = await asyncio.wait_for(connection.call(_GET_STATE, timeout=timeout), timeout)
    except asyncio.TimeoutError:
        raise HealthCheckError('connection_health_timeout') from None
    except Exception:
        raise HealthCheckError('connection_health_rpc_failed') from None

    try:
        current, _ = schema.decode_prefix(raw, 'updates.State')
        if not isinstance(current, dict) or current.get('_') != 'updates.state':
            raise ValueError('invalid state constructor')
        for name in _STATE_FIELDS:
            value = current.get(name)
            if type(value) is not int or not 0 <= value <= 0x7fffffff:
                raise ValueError('invalid state integer')
    except Exception:
        raise HealthCheckError('connection_health_invalid_response') from None

    try:
        connection.check_connected()
    except Exception:
        raise HealthCheckError('connection_health_rpc_failed') from None


async def health_loop(connection, schema, state, stop, *, interval=30.0, timeout=10.0):
    interval, timeout = _positive_seconds(interval), _positive_seconds(timeout)
    health = state.setdefault('health_check', {})
    for name, value in (
        ('checks', 0), ('failures', 0), ('last_checked_at', None),
        ('last_success_at', None), ('last_latency_ms', None),
    ):
        health.setdefault(name, value)
    health.update(status='checking', interval_seconds=interval, timeout_seconds=timeout, last_error=None)

    stop_task = asyncio.create_task(stop.wait())
    check_task = None
    try:
        while not stop.is_set():
            health.update(status='checking', last_checked_at=_now())
            health['checks'] += 1
            started = time.monotonic()
            check_task = asyncio.create_task(_check(connection, schema, timeout))
            await asyncio.wait((check_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                return
            try:
                await check_task
            except HealthCheckError as exc:
                health.update(status='failed', last_latency_ms=(time.monotonic() - started) * 1000,
                              last_error={'error_type': 'HealthCheckError', 'reason_code': exc.reason_code})
                health['failures'] += 1
                state['connected'] = False
                raise
            health.update(status='healthy', last_success_at=_now(),
                          last_latency_ms=(time.monotonic() - started) * 1000, last_error=None)
            check_task = None
            try:
                await asyncio.wait_for(asyncio.shield(stop_task), interval)
            except asyncio.TimeoutError:
                continue
            return
    finally:
        tasks = [task for task in (check_task, stop_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if health['status'] != 'failed':
            health['status'] = 'stopped'


async def supervise_connection(coroutines, stop):
    """Wait for one worker or shutdown, then finish every owned task."""
    pending, tasks = [], []
    try:
        # Retain unstarted coroutines so partial task construction cannot leak
        # them, including when the iterable itself raises during iteration.
        for coroutine in coroutines:
            pending.append(coroutine)
        if stop.is_set():
            return
        worker_count = len(pending)
        pending.append(stop.wait())
        for coroutine in pending:
            tasks.append(asyncio.create_task(coroutine))
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if stop.is_set():
            return
        workers = tasks[:worker_count]
        # Errors take precedence over a simultaneous normal worker exit, in the
        # caller's order (health can be first to preserve its useful reason).
        for task in workers:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error
        for task in workers:
            if task.done():
                if task.cancelled():
                    raise asyncio.CancelledError
                raise RuntimeError('connection_worker_stopped')
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for coroutine in pending[len(tasks):]:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
