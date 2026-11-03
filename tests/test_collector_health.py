import asyncio
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


def message(mid=1, pts=2):
    return bytes.fromhex('11bf4f91 00000000') + struct.pack('<ii', mid, 42) + bytes.fromhex(
        '02686900') + struct.pack('<iii', pts, 1, 1700000000)


def state_wire(pts=1):
    return struct.pack('<Iiiiii', 0xa56c2a3e, pts, 0, 1700000000, 0, 0)


@unittest.skipUnless(sys.platform == 'win32' and importlib.util.find_spec('telethon'),
                     'Windows and protocol dependencies required')
class CollectorHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from tatocollector.collector import PacketProcessor
        from tatocollector.outbox import Store
        from tatocollector.sync import merge_cursors
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'queue.sqlite', checkpoint_merger=merge_cursors)
        self.addCleanup(self.store.close)
        self.store.set_initial_checkpoint({'pts': 1, 'qts': 0, 'seq': 0,
                                          'date': 1700000000, 'channels': {}})
        self.processor = PacketProcessor(self.store, 7)

    def process(self, raw):
        return self.processor.process(self.store.put_wire(raw), raw)

    async def test_live_message_is_processed_while_dialog_recovery_is_stalled(self):
        from tatocollector.collector import run_connected
        from tatocollector.live_wire import RawTatoUpdates
        blocked, stop, queue, status = asyncio.Event(), asyncio.Event(), asyncio.Queue(), {'connected': True}

        async def call(wire, **_kwargs):
            if wire == bytes.fromhex('2a88d4ed'):
                return state_wire()
            blocked.set()
            await asyncio.Future()

        connection = SimpleNamespace(call=call, check_connected=lambda: None)
        task = asyncio.create_task(run_connected(connection, queue, self.store, self.processor, status, stop,
                                                  health_interval=0.02, health_timeout=0.05))
        try:
            await asyncio.wait_for(blocked.wait(), 2)
            await queue.put(RawTatoUpdates(message()))
            async def captured():
                while self.store.counts()['captured'] != 1:
                    await asyncio.sleep(0)
            await asyncio.wait_for(captured(), 2)
            self.assertTrue(status['connected'])
            self.assertEqual(self.store.get_checkpoint()['pts'], 2)
        finally:
            stop.set()
            await asyncio.wait_for(task, 2)

    async def test_half_open_connection_is_closed_before_the_next_connection_is_created(self):
        from tatocollector.collector import connection_loop
        stop = asyncio.Event()
        first_closed, recovery_cancelled = asyncio.Event(), asyncio.Event()
        opened = []

        class Connection:
            def __init__(self, *_args, **_kwargs):
                self.index = len(opened)
                if self.index:
                    self_outer.assertTrue(first_closed.is_set())
                opened.append(self)
                self.calls = 0
                self.host, self.port = '8.8.8.8', 20334
                self.endpoint_status = {'host': self.host, 'port': self.port, 'source': 'native'}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                if not self.index:
                    self_outer.assertTrue(recovery_cancelled.is_set())
                    first_closed.set()

            def check_connected(self):
                pass  # The local connection flag never reports the half-open failure.

            async def call(self, wire, **_kwargs):
                if wire == bytes.fromhex('2a88d4ed'):
                    self.calls += 1
                    if self.calls == 1:
                        if self.index:
                            stop.set()
                        return state_wire()
                    await asyncio.Future()
                try:
                    await asyncio.Future()
                finally:
                    recovery_cancelled.set()

        self_outer = self
        profile = Path(self.temp.name) / 'profile.json'
        profile.write_text(json.dumps({'layer': 123, 'api_id': 1, 'app_version': 'synthetic'}))
        args = SimpleNamespace(session=Path(self.temp.name) / 'session', profile=profile,
                               public_key=Path('research/android-static/embedded-public-0.pem'),
                               health_interval=0.02, health_timeout=0.03)
        session = {'host': '8.8.8.8', 'port': 20334, 'dc_id': 1,
                   'auth_key_hex': bytes(range(256)).hex(), 'time_offset': 0}
        status = {}
        with (patch('tatocollector.collector.load_session', return_value=session),
              patch('tatocollector.collector.TatoConnection', Connection),
              patch('tatocollector.collector.TatoUpdatesRegistry', lambda **_kwargs: nullcontext()),
              patch('tatocollector.collector._pause', new=AsyncMock())):
            await asyncio.wait_for(connection_loop(args, self.store, self.processor, status, stop), 2)
        self.assertEqual(len(opened), 2)
        self.assertEqual(status['health_check']['failures'], 1)
        self.assertFalse(status['connected'])

    async def test_old_global_reply_does_not_clear_a_gap_seen_during_the_request(self):
        from tatocollector.collector import recover_global
        self.processor.gaps.add('global')

        async def call(_wire):
            self.assertTrue(self.process(message(1, 5)))
            return struct.pack('<Iii', 0x5d75a138, 1700000001, 0)

        await recover_global(SimpleNamespace(call=call), self.processor, {})
        self.assertIn('global', self.processor.gaps)

    async def test_old_channel_reply_does_not_clear_a_new_channel_gap(self):
        from tatocollector.collector import recover_channel
        self.processor.working_cursor['channels']['9'] = {'pts': 20, 'access_hash': 123}
        self.processor.gaps.add('channel:9')
        # Store the same channel baseline through a real transaction.
        self.store.commit_packet(self.store.put_wire(b'synthetic-baseline'), [], self.processor.working_cursor)

        async def call(_wire):
            self.assertTrue(self.process(struct.pack('<IIiii', 0x78d4dec1, 0xeb0467fb, 0, 9, 1700000001)))
            return struct.pack('<IIi', 0x3e11affb, 1, 20)

        await recover_channel(SimpleNamespace(call=call), self.processor, 9, {})
        self.assertIn('channel:9', self.processor.gaps)

    async def test_new_gap_keeps_urgent_global_schedule_after_an_old_reply(self):
        from tatocollector.collector import synchronize_updates
        stop, schedule = asyncio.Event(), {'global_due': 0.0, 'channel_due': {}}

        async def call(_wire):
            self.process(message(1, 5))
            stop.set()
            return struct.pack('<Iii', 0x5d75a138, 1700000001, 0)

        with patch('tatocollector.collector.refresh_dialogs', new=AsyncMock()):
            await synchronize_updates(SimpleNamespace(call=call), self.processor, {}, stop, schedule)
        self.assertEqual(schedule['global_due'], 0)

    async def test_new_channel_gap_keeps_urgent_schedule_after_an_old_reply(self):
        from tatocollector.collector import synchronize_updates
        self.processor.working_cursor['channels']['9'] = {'pts': 20, 'access_hash': 123}
        self.store.commit_packet(self.store.put_wire(b'synthetic-baseline'), [], self.processor.working_cursor)
        stop, schedule = asyncio.Event(), {'global_due': 0.0, 'channel_due': {}}

        async def call(_wire):
            self.process(struct.pack('<IIiii', 0x78d4dec1, 0xeb0467fb, 0, 9, 1700000001))
            stop.set()
            return struct.pack('<IIi', 0x3e11affb, 1, 20)

        with (patch('tatocollector.collector.refresh_dialogs', new=AsyncMock()),
              patch('tatocollector.collector.recover_global', new=AsyncMock())):
            await synchronize_updates(SimpleNamespace(call=call, check_connected=lambda: None),
                                      self.processor, {}, stop, schedule)
        self.assertEqual(schedule['channel_due']['9'], 0)

    async def test_old_difference_does_not_roll_back_concurrent_live_progress(self):
        from tatocollector.collector import recover_global

        async def call(_wire):
            self.process(message(1, 2))
            self.process(message(2, 3))
            return (struct.pack('<I', 0x00f49ca0)
                    + struct.pack('<II', 0x1cb5c415, 0) * 5 + state_wire(2))

        await recover_global(SimpleNamespace(call=call), self.processor, {})
        self.assertEqual(self.store.counts()['captured'], 2)
        self.assertEqual(self.store.get_checkpoint()['pts'], 3)
        self.assertEqual(self.processor.working_cursor['pts'], 3)

    async def test_pending_packets_are_processed_before_the_initial_baseline_is_saved(self):
        from tatocollector.collector import PacketProcessor, connection_loop
        from tatocollector.outbox import Store
        from tatocollector.sync import merge_cursors
        store = Store(Path(self.temp.name) / 'new-queue.sqlite', checkpoint_merger=merge_cursors)
        self.addCleanup(store.close)
        store.put_wire(message())
        processor = PacketProcessor(store, 7)
        stop = asyncio.Event()

        class Connection:
            host, port = '8.8.8.8', 20334
            endpoint_status = {'host': host, 'port': port, 'source': 'native'}

            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def call(self, _wire):
                stop.set()
                return state_wire()

        profile = Path(self.temp.name) / 'baseline-profile.json'
        profile.write_text(json.dumps({'layer': 123, 'api_id': 1, 'app_version': 'synthetic'}))
        args = SimpleNamespace(session=Path(self.temp.name) / 'session', profile=profile,
                               public_key=Path('research/android-static/embedded-public-0.pem'))
        session = {'host': '8.8.8.8', 'port': 20334, 'dc_id': 1,
                   'auth_key_hex': bytes(range(256)).hex(), 'time_offset': 0}
        with (patch('tatocollector.collector.load_session', return_value=session),
              patch('tatocollector.collector.TatoConnection', Connection),
              patch('tatocollector.collector.TatoUpdatesRegistry', lambda **_kwargs: nullcontext())):
            await connection_loop(args, store, processor, {}, stop)
        self.assertEqual(store.get_checkpoint()['pts'], 1)
        self.assertEqual(store.counts()['captured'], 1)

    async def test_already_replayed_gap_packets_still_schedule_urgent_recovery(self):
        from tatocollector.collector import receive_updates
        from tatocollector.live_wire import RawTatoUpdates
        for scope, raw in (
                ('global', message(1, 5)),
                ('channel:9', struct.pack('<IIiii', 0x78d4dec1, 0xeb0467fb, 0, 9, 1700000001))):
            with self.subTest(scope=scope):
                packet_id = self.store.put_wire(raw)
                self.assertTrue(self.processor.process(packet_id, raw))
                processed = self.processor.stats['decoded_packets']
                schedule = {'global_due': time.monotonic() + 30,
                            'channel_due': {'9': time.monotonic() + 300}}
                queue, stop = asyncio.Queue(), asyncio.Event()
                update = RawTatoUpdates(raw)
                update.sample_path = packet_id
                await queue.put(update)
                task = asyncio.create_task(receive_updates(queue, self.store, self.processor, {}, stop, schedule))
                try:
                    await asyncio.wait_for(queue.join(), 2)
                    if scope == 'global':
                        self.assertLessEqual(schedule['global_due'], time.monotonic() + 1.1)
                    else:
                        self.assertEqual(schedule['channel_due']['9'], 0)
                    self.assertEqual(self.processor.stats['decoded_packets'], processed)
                finally:
                    stop.set()
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_replay_yields_without_losing_packets_or_advancing_past_them(self):
        for mid in range(1, 17):
            self.store.put_wire(message(mid, mid + 1))
        observed = []

        async def observer():
            observed.append(self.processor.stats['decoded_packets'])

        marker = asyncio.create_task(observer())
        await self.processor.replay_async()
        await marker
        self.assertLess(observed[0], 16)
        self.assertEqual(self.store.counts()['captured'], 16)
        self.assertEqual(self.store.get_checkpoint()['pts'], 17)

    async def test_duplicate_live_wire_is_not_processed_twice(self):
        from tatocollector.collector import receive_updates
        from tatocollector.live_wire import RawTatoUpdates
        queue, stop = asyncio.Queue(), asyncio.Event()
        for _ in range(2):
            await queue.put(RawTatoUpdates(message()))
        task = asyncio.create_task(receive_updates(queue, self.store, self.processor, {}, stop,
                                                  {'global_due': 0.0, 'channel_due': {}}))
        try:
            await asyncio.wait_for(queue.join(), 2)
            self.assertEqual(self.processor.stats['decoded_packets'], 1)
            self.assertEqual(self.store.counts()['captured'], 1)
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@unittest.skipUnless(importlib.util.find_spec('telethon'), 'Protocol dependencies required')
class HealthCliTests(unittest.TestCase):
    def test_invalid_health_timing_is_rejected_before_any_session_or_network_access(self):
        for name, value in (('--health-interval', 'nan'), ('--health-interval', '0'),
                            ('--health-timeout', 'inf'), ('--health-timeout', '61')):
            with self.subTest(name=name, value=value):
                result = subprocess.run([sys.executable, '-m', 'tatocollector.collector', name, value],
                                        input='', text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(name[2:], result.stderr)


if __name__ == '__main__':
    unittest.main()
