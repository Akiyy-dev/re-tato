import asyncio
import gc
import importlib
import struct
import unittest

from tatocollector.tl import Schema


STATE_WIRE = struct.pack('<Iiiiii', 0xA56C2A3E, 7, 0, 1700000000, 3, 0)
STATE_SCHEMA = 'updates.state#a56c2a3e pts:int qts:int date:int seq:int unread_count:int = updates.State;'


class SyntheticConnection:
    """Only the RPC boundary is synthetic; no sender or account is created."""

    def __init__(self, reply=STATE_WIRE, *, error=None, blocked=False):
        self.reply, self.error, self.blocked = reply, error, blocked
        self.entered, self.cancelled = asyncio.Event(), asyncio.Event()
        self.calls, self.local_checks = 0, 0
        self.disconnect_after_reply = False

    def check_connected(self):
        self.local_checks += 1
        if self.disconnect_after_reply and self.calls:
            raise ConnectionResetError('synthetic private transport detail')

    async def call(self, wire, *, timeout):
        if wire != bytes.fromhex('2a88d4ed') or timeout <= 0:
            raise AssertionError('health must issue a bounded getState RPC')
        self.calls += 1
        self.entered.set()
        try:
            if self.blocked:
                await asyncio.Event().wait()
            if self.error is not None:
                raise self.error
            return self.reply
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class HealthTestCase(unittest.IsolatedAsyncioTestCase):
    def health(self):
        try:
            return importlib.import_module('tatocollector.health')
        except ModuleNotFoundError as exc:
            if exc.name != 'tatocollector.health':
                raise
            self.fail('independent business connection health checks are missing')

    async def wait_for_status(self, state, predicate):
        async def wait():
            while not predicate(state.get('health_check', {})):
                await asyncio.sleep(0)
        await asyncio.wait_for(wait(), 1)


class HealthTests(HealthTestCase):
    async def test_hung_rpc_times_out_even_when_local_sender_claims_connected(self):
        health = self.health()
        connection, state = SyntheticConnection(blocked=True), {'connected': True}
        with self.assertRaises(health.HealthCheckError) as caught:
            await asyncio.wait_for(health.health_loop(
                connection, Schema(STATE_SCHEMA), state, asyncio.Event(), timeout=.01), 1)
        self.assertEqual(caught.exception.reason_code, 'connection_health_timeout')
        self.assertFalse(state['connected'])
        self.assertEqual(state['health_check']['status'], 'failed')
        self.assertEqual(state['health_check']['checks'], 1)
        self.assertEqual(state['health_check']['failures'], 1)
        self.assertTrue(connection.cancelled.is_set())

    async def test_idle_connection_repeatedly_checks_without_advancing_sync_state(self):
        health = self.health()
        connection, stop = SyntheticConnection(), asyncio.Event()
        checkpoint = {'pts': 2, 'date': 1600000000, 'seq': 1}
        state = {'connected': True, 'last_message_at': None, 'checkpoint': checkpoint.copy()}
        task = asyncio.create_task(health.health_loop(
            connection, Schema(STATE_SCHEMA), state, stop, interval=.01, timeout=.2))
        try:
            await self.wait_for_status(state, lambda h: h.get('checks', 0) >= 3 and h.get('status') == 'healthy')
            record = state['health_check']
            self.assertTrue(state['connected'])
            self.assertEqual(state['checkpoint'], checkpoint)
            self.assertIsNone(state['last_message_at'])
            self.assertEqual(record['failures'], 0)
            self.assertIsNone(record['last_error'])
            self.assertIsNotNone(record['last_success_at'])
            self.assertIsNotNone(record['last_checked_at'])
            self.assertGreaterEqual(record['last_latency_ms'], 0)
            self.assertEqual(record['interval_seconds'], .01)
            self.assertEqual(record['timeout_seconds'], .2)
            self.assertGreaterEqual(connection.local_checks, 6)
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(state['health_check']['status'], 'stopped')

    async def test_get_state_prefix_accepts_synthetic_rpc_padding(self):
        health = self.health()
        connection = SyntheticConnection(STATE_WIRE + b'\x83\x01\xfa\x00\x10\x11\x12\x13')
        state, stop = {'connected': True}, asyncio.Event()
        task = asyncio.create_task(health.health_loop(connection, Schema(STATE_SCHEMA), state, stop))
        try:
            await self.wait_for_status(state, lambda h: h.get('status') == 'healthy')
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
        self.assertTrue(state['connected'])
        self.assertEqual(state['health_check']['failures'], 0)

    async def test_invalid_wire_and_negative_state_are_rejected(self):
        health = self.health()
        for reply in (b'', STATE_WIRE[:12], bytes.fromhex('b5757299'),
                      struct.pack('<Iiiiii', 0xA56C2A3E, -1, 0, 1700000000, 3, 0)):
            with self.subTest(reply=reply.hex()):
                state = {'connected': True}
                with self.assertRaises(health.HealthCheckError) as caught:
                    await health.health_loop(SyntheticConnection(reply), Schema(STATE_SCHEMA), state, asyncio.Event())
                self.assertEqual(caught.exception.reason_code, 'connection_health_invalid_response')
                self.assertFalse(state['connected'])
                self.assertEqual(state['health_check']['failures'], 1)

    async def test_every_state_field_requires_non_boolean_bounded_integer(self):
        health = self.health()
        valid = {'_': 'updates.state', 'pts': 7, 'qts': 0, 'date': 1700000000, 'seq': 3, 'unread_count': 0}

        class DecodedSchema:
            def __init__(self, value):
                self.value = value

            def decode_prefix(self, raw, expected_type):
                if expected_type != 'updates.State':
                    raise AssertionError('health must require updates.State')
                return self.value, len(raw)

        malformed = [None, [], {**valid, '_': 'unexpected'}]
        for field in ('pts', 'qts', 'date', 'seq', 'unread_count'):
            malformed += [{**valid, field: bad} for bad in (True, -1, 2**31, 1.5, '7', None)]
            malformed.append({key: value for key, value in valid.items() if key != field})
        for value in malformed:
            with self.subTest(value=value):
                state = {'connected': True}
                with self.assertRaises(health.HealthCheckError) as caught:
                    await health.health_loop(SyntheticConnection(), DecodedSchema(value), state, asyncio.Event())
                self.assertEqual(caught.exception.reason_code, 'connection_health_invalid_response')
                self.assertEqual(state['health_check']['failures'], 1)

    async def test_rpc_exception_text_is_not_exposed_by_health_status_or_error(self):
        health = self.health()
        private = 'synthetic secret body and account material'
        state = {'connected': True}
        with self.assertRaises(health.HealthCheckError) as caught:
            await health.health_loop(SyntheticConnection(error=ValueError(private)),
                                     Schema(STATE_SCHEMA), state, asyncio.Event())
        self.assertEqual(caught.exception.reason_code, 'connection_health_rpc_failed')
        self.assertEqual(state['health_check']['last_error'], {
            'error_type': 'HealthCheckError', 'reason_code': 'connection_health_rpc_failed'})
        self.assertNotIn(private, str(state))
        self.assertNotIn(private, str(caught.exception))

    async def test_disconnect_reported_after_rpc_is_a_health_failure(self):
        health = self.health()
        connection, state = SyntheticConnection(), {'connected': True}
        connection.disconnect_after_reply = True
        with self.assertRaises(health.HealthCheckError) as caught:
            await health.health_loop(connection, Schema(STATE_SCHEMA), state, asyncio.Event())
        self.assertEqual(caught.exception.reason_code, 'connection_health_rpc_failed')
        self.assertFalse(state['connected'])
        self.assertEqual(state['health_check']['failures'], 1)
        self.assertIsNone(state['health_check']['last_success_at'])

    async def test_stop_during_rpc_cancels_it_without_recording_failure(self):
        health = self.health()
        connection, stop, state = SyntheticConnection(blocked=True), asyncio.Event(), {'connected': True}
        task = asyncio.create_task(health.health_loop(connection, Schema(STATE_SCHEMA), state, stop))
        await asyncio.wait_for(connection.entered.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(connection.cancelled.is_set())
        self.assertEqual(state['health_check']['status'], 'stopped')
        self.assertEqual(state['health_check']['failures'], 0)
        self.assertIsNone(state['health_check']['last_error'])

    async def test_outer_cancellation_is_propagated_without_recording_failure(self):
        health = self.health()
        connection, state = SyntheticConnection(blocked=True), {'connected': True}
        task = asyncio.create_task(health.health_loop(connection, Schema(STATE_SCHEMA), state, asyncio.Event()))
        await asyncio.wait_for(connection.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(connection.cancelled.is_set())
        self.assertEqual(state['health_check']['status'], 'stopped')
        self.assertEqual(state['health_check']['failures'], 0)

    async def test_preexisting_stop_avoids_rpc_and_counts_no_checks(self):
        health = self.health()
        connection, stop, state = SyntheticConnection(), asyncio.Event(), {'connected': True}
        stop.set()
        await health.health_loop(connection, Schema(STATE_SCHEMA), state, stop)
        self.assertEqual(connection.calls, 0)
        self.assertEqual(state['health_check']['checks'], 0)
        self.assertEqual(state['health_check']['failures'], 0)
        self.assertEqual(state['health_check']['status'], 'stopped')

    async def test_counts_survive_a_failed_connection_then_successful_reconnect(self):
        health = self.health()
        state = {'connected': True}
        with self.assertRaises(health.HealthCheckError):
            await health.health_loop(SyntheticConnection(error=ConnectionError('synthetic')),
                                     Schema(STATE_SCHEMA), state, asyncio.Event())
        stop = asyncio.Event()
        state['connected'] = True
        task = asyncio.create_task(health.health_loop(SyntheticConnection(), Schema(STATE_SCHEMA), state, stop))
        try:
            await self.wait_for_status(state, lambda h: h.get('status') == 'healthy')
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(state['health_check']['checks'], 2)
        self.assertEqual(state['health_check']['failures'], 1)
        self.assertIsNone(state['health_check']['last_error'])

    async def test_invalid_schedule_is_rejected_before_rpc(self):
        health = self.health()
        for option in ('interval', 'timeout'):
            for bad in (0, -1, float('inf'), float('nan'), True):
                with self.subTest(option=option, bad=bad):
                    connection = SyntheticConnection()
                    with self.assertRaises(ValueError):
                        await health.health_loop(connection, Schema(STATE_SCHEMA), {}, asyncio.Event(), **{option: bad})
                    self.assertEqual(connection.calls, 0)

    async def test_error_reason_cannot_accept_arbitrary_external_text(self):
        health = self.health()
        for bad in ('synthetic secret', '', None, []):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as caught:
                    health.HealthCheckError(bad)
                self.assertNotIn('synthetic secret', str(caught.exception))


class SupervisorTests(HealthTestCase):
    async def assert_no_new_tasks(self, before):
        await asyncio.sleep(0)
        self.assertEqual(asyncio.all_tasks() - before, set())

    async def test_health_failure_cancels_blocked_workers_and_keeps_original_exception(self):
        health = self.health()
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = health.HealthCheckError('connection_health_timeout')
        before = asyncio.all_tasks()

        async def failing_health():
            await entered.wait()
            raise original

        async def blocked_worker():
            entered.set()
            try:
                await release.wait()
            finally:
                cancelled.set()

        with self.assertRaises(health.HealthCheckError) as caught:
            await health.supervise_connection([failing_health(), blocked_worker()], asyncio.Event())
        self.assertIs(caught.exception, original)
        self.assertTrue(cancelled.is_set())
        await self.assert_no_new_tasks(before)

    async def test_first_failed_worker_has_priority_over_an_earlier_normal_exit(self):
        health = self.health()
        original = health.HealthCheckError('connection_health_timeout')

        async def normal_exit():
            return

        async def fail():
            raise original

        with self.assertRaises(health.HealthCheckError) as caught:
            await health.supervise_connection([normal_exit(), fail()], asyncio.Event())
        self.assertIs(caught.exception, original)

    async def test_multiple_failures_prioritize_worker_list_order_and_retrieve_all(self):
        health = self.health()
        first, second = health.HealthCheckError('connection_health_timeout'), ValueError('synthetic secondary failure')
        reports, loop = [], asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda loop, context: reports.append(context))

        async def fail(error):
            raise error

        try:
            with self.assertRaises(health.HealthCheckError) as caught:
                await health.supervise_connection([fail(first), fail(second)], asyncio.Event())
            self.assertIs(caught.exception, first)
            gc.collect()
            await asyncio.sleep(0)
            self.assertEqual(reports, [])
        finally:
            loop.set_exception_handler(previous)

    async def test_unexpected_normal_worker_exit_is_a_connection_failure(self):
        health = self.health()

        async def normal_exit():
            return

        with self.assertRaisesRegex(RuntimeError, '^connection_worker_stopped$'):
            await health.supervise_connection([normal_exit()], asyncio.Event())

    async def test_stop_wins_over_simultaneous_failure_and_cleans_workers(self):
        health = self.health()
        stop, before = asyncio.Event(), asyncio.all_tasks()

        async def fail_while_stopping():
            stop.set()
            raise ValueError('synthetic simultaneous shutdown')

        await health.supervise_connection([fail_while_stopping()], stop)
        await self.assert_no_new_tasks(before)

    async def test_stop_cancels_workers_without_waiting_for_their_normal_completion(self):
        health = self.health()
        stop, entered, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        before = asyncio.all_tasks()

        async def worker():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(health.supervise_connection([worker()], stop))
        await asyncio.wait_for(entered.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        await self.assert_no_new_tasks(before)

    async def test_supervisor_outer_cancellation_awaits_worker_cleanup(self):
        health = self.health()
        entered, cancelled = asyncio.Event(), asyncio.Event()
        before = asyncio.all_tasks()

        async def worker():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cancelled.set()

        task = asyncio.create_task(health.supervise_connection([worker()], asyncio.Event()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        await self.assert_no_new_tasks(before)

    async def test_preexisting_stop_closes_unstarted_coroutines(self):
        health = self.health()
        stop, ran = asyncio.Event(), []
        stop.set()

        async def worker():
            ran.append(True)

        coroutine = worker()
        await health.supervise_connection([coroutine], stop)
        self.assertEqual(ran, [])
        self.assertIsNone(coroutine.cr_frame)

    async def test_task_construction_failure_closes_remaining_coroutines(self):
        health = self.health()
        before = asyncio.all_tasks()

        async def worker():
            await asyncio.Event().wait()

        first, last = worker(), worker()
        with self.assertRaises(TypeError):
            await health.supervise_connection([first, object(), last], asyncio.Event())
        self.assertIsNone(first.cr_frame)
        self.assertIsNone(last.cr_frame)
        await self.assert_no_new_tasks(before)


if __name__ == '__main__':
    unittest.main()
