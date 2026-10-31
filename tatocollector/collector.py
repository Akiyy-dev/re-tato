"""Standalone Tatotalk receive, encrypted durable queue and HTTP delivery."""

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import struct
import time

from telethon.tl.functions import PingRequest

from .capture import load_schema
from .client import TatoConnection, safe_error
from .events import PeerCache, normalize_updates
from .ingest import DeliveryResult, deliver
from .live_wire import RawTatoUpdates, TatoUpdatesRegistry
from .session_store import load_session
from .settings import load_ingest_config
from .sync import (advance, channel_events, channel_request, dialogs_cursor, dialogs_offset,
                   dialogs_request, difference_events, difference_request, merge_cursors)
from .tl import TLDecodeError
from .wire_log import WireLog


def write_status(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps({**state, 'updated_at': datetime.now(timezone.utc).isoformat()},
                                        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PacketProcessor:
    def __init__(self, store, account_id):
        self.store, self.account_id = store, account_id
        self.schema, self.peers = load_schema(), PeerCache()
        self.working_cursor = store.get_checkpoint()
        self.attempted = set()
        self.gaps = set()
        self.stats = {'decoded_packets': 0, 'decode_errors': {}, 'unknown_constructors': {}}

    def process(self, packet_id, raw):
        self.attempted.add(packet_id)
        try:
            if raw == b'TATOBSL1':
                cursor = merge_cursors(self.working_cursor, self.store.get_checkpoint())
                if cursor is None:
                    raise ValueError('baseline_missing')
                candidate = cursor | {'initial_dialogs_done': True}
                self.store.commit_packet(packet_id, [], candidate)
                self.working_cursor = candidate
                return True
            cid = None
            if raw.startswith(b'TATOCHN1'):
                cid = struct.unpack_from('<i', raw, 8)[0]
                raw = raw[12:]
            parsed, _ = self.schema.decode_prefix(raw)
            self.parsed = parsed
            cursor = merge_cursors(self.working_cursor, self.store.get_checkpoint())
            if cid is not None:
                if cursor is None:
                    raise ValueError('difference_requires_baseline')
                events, candidate, self.more = channel_events(parsed, cursor, cid, self.account_id, self.peers)
            elif parsed['_'].startswith('messages.dialogs'):
                if cursor is None:
                    raise ValueError('difference_requires_baseline')
                self.peers.observe(parsed.get('users', []), parsed.get('chats', []))
                candidate = dialogs_cursor(parsed, cursor)
                events = []  # Dialog top messages are history, not new events.
            elif parsed['_'].startswith('updates.difference'):
                if cursor is None:
                    raise ValueError('difference_requires_baseline')
                events, candidate, more = difference_events(parsed, cursor, self.account_id, self.peers)
                self.more = more
            else:
                definition = self.schema.definitions[struct.unpack_from('<I', raw)[0]]
                if definition.result != 'Updates':
                    raise ValueError('unexpected_update_root')
                events = normalize_updates(parsed, self.account_id, self.peers)
                candidate, gaps = advance(cursor, parsed) if cursor is not None else (None, set())
                self.gaps.update(gaps)
            self.store.commit_packet(packet_id, events, candidate)
            self.working_cursor = merge_cursors(cursor, candidate)
            self.stats['decoded_packets'] += 1
            return True
        except (TLDecodeError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, TLDecodeError) else 'packet_validation_failed'
            if type(exc) is ValueError and str(exc) in ('difference_too_long', 'channel_difference_too_long'):
                reason = str(exc)
                self.gaps.add('global' if cid is None else f'channel:{cid}')
            constructor = (f'{exc.constructor:08x}' if isinstance(exc, TLDecodeError)
                           and exc.constructor is not None else None)
            self.store.fail_packet(packet_id, reason, constructor)
            errors = self.stats['decode_errors']
            errors[reason] = errors.get(reason, 0) + 1
            if constructor:
                unknown = self.stats['unknown_constructors']
                unknown[constructor] = unknown.get(constructor, 0) + 1
            return False

    def replay(self):
        # One attempt per packet per process. Restarting after a schema upgrade
        # replays failed bytes; an unknown packet cannot create a busy loop.
        after_id = 0
        while True:
            batch = self.store.pending_wires(limit=100, after_id=after_id)
            if not batch:
                break
            for packet_id, raw in batch:
                after_id = packet_id
                if packet_id not in self.attempted:
                    self.process(packet_id, raw)


class _Diagnostics(logging.Handler):
    def __init__(self, queue, store):
        super().__init__()
        self.queue, self.store = queue, store

    def emit(self, record):
        # SDK log formatting may expose decoded content or raw bodies.
        if str(record.msg).startswith('Type %08x not found') and record.args:
            raw = record.args[1]
            if isinstance(raw, bytes):
                update = RawTatoUpdates(raw)
                update.sample_path = self.store.put_wire(raw)
                self.queue.put_nowait(update)


@contextmanager
def instance_lock(path):
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('collector_already_running') from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


async def _pause(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def delivery_loop(store, config_path, state, stop):
    config, signature = None, None
    next_check = 0
    while not stop.is_set():
        if time.monotonic() >= next_check:
            next_check = time.monotonic() + 5
            try:
                latest = config_path.stat().st_mtime_ns
                if latest != signature:
                    config = load_ingest_config(config_path)
                    signature = latest
                    if config is not None:
                        store.retry_auth_errors()
                    state['http_configured'] = config is not None
                    state.pop('http_config_error', None)
            except (OSError, ValueError, KeyError):
                config = None
                state['http_configured'] = False
                state['http_config_error'] = 'configuration_unavailable'
        item = store.next_delivery(time.time()) if config is not None else None
        if item is None:
            await _pause(stop, 0.5)
            continue
        key, payload, _ = item
        result = await asyncio.to_thread(deliver, payload, config)
        store.record_delivery(key, result, time.time())
        state['last_delivery'] = asdict(result)
        if result.kind != 'retry':
            print(json.dumps({'delivery': result.kind, 'status_code': result.status_code,
                              'receipt_id': result.receipt_id, 'reason': result.reason}), flush=True)


async def recover_global(connection, processor, state):
    cursor = processor.store.get_checkpoint()
    if cursor is None:
        return
    for _ in range(20):
        raw = await connection.call(difference_request(cursor))
        packet_id = processor.store.put_wire(raw)
        if not processor.process(packet_id, raw):
            processor.gaps.add('global')
            state['recovery_error'] = 'difference_not_decoded'
            return
        # A failed earlier raw packet can hold the durable checkpoint back.
        # The response's state is still safe for traversing this slice chain.
        parsed, _ = processor.schema.decode_prefix(raw, 'updates.Difference')
        _, cursor, more = difference_events(parsed, cursor, processor.account_id, processor.peers)
        if not more:
            processor.gaps.discard('global')
            state.pop('recovery_error', None)
            state['last_global_recovery'] = datetime.now(timezone.utc).isoformat()
            return
    state['recovery_error'] = 'difference_slice_limit'


async def refresh_dialogs(connection, processor, state):
    for folder in (0, 1):
        offset, seen, total = None, set(), 0
        for _ in range(100):
            raw = await connection.call(dialogs_request(offset, folder))
            packet_id = processor.store.put_wire(raw)
            if not processor.process(packet_id, raw):
                state['dialogs_error'] = 'dialog_baseline_not_decoded'
                return
            parsed = processor.parsed
            count = len(parsed.get('dialogs', []))
            total += count
            if not count or 'count' not in parsed or total >= parsed['count']:
                break
            offset = dialogs_offset(parsed)
            key = (offset['id'], offset['peer']) if offset else None
            if key is None or key in seen:
                state['dialogs_error'] = 'dialog_pagination_stalled'
                return
            seen.add(key)
        else:
            state['dialogs_error'] = 'dialog_page_limit'
            return
    state.pop('dialogs_error', None)
    if not processor.working_cursor.get('initial_dialogs_done'):
        # A local metadata record makes completion of the initial multi-page
        # baseline replayable and atomic with the same cursor frontier.
        packet_id = processor.store.put_wire(b'TATOBSL1')
        processor.process(packet_id, b'TATOBSL1')
    state['channel_baselines'] = len(processor.store.get_checkpoint().get('channels', {}))


async def recover_channel(connection, processor, cid, state):
    cursor = processor.store.get_checkpoint()
    for _ in range(20):
        entry = cursor.get('channels', {}).get(str(cid))
        if entry is None:
            return 30
        raw = await connection.call(channel_request(cid, entry))
        # Empty channel differences carry no channel identity. Persist the
        # read-only request's public ID with the raw response for safe replay.
        wire = b'TATOCHN1' + struct.pack('<i', cid) + raw
        packet_id = processor.store.put_wire(wire)
        if not processor.process(packet_id, wire):
            processor.gaps.add(f'channel:{cid}')
            state['channel_recovery_error'] = 'channel_difference_not_decoded'
            return 30
        parsed = processor.parsed
        _, cursor, more = channel_events(parsed, cursor, cid, processor.account_id, processor.peers)
        if not more:
            processor.gaps.discard(f'channel:{cid}')
            state.pop('channel_recovery_error', None)
            return max(1, min(parsed.get('timeout', 30), 300))
    state['channel_recovery_error'] = 'channel_difference_slice_limit'
    return 30


async def recover_channel_safely(connection, processor, cid, state):
    errors = state.setdefault('channel_errors', {})
    try:
        timeout = await recover_channel(connection, processor, cid, state)
    except Exception as exc:
        if not connection.sender.is_connected():
            raise
        previous = errors.get(str(cid), {}).get('attempts', 0)
        errors[str(cid)] = {'attempts': previous + 1, **safe_error(exc)}
        return min(300, 2 ** min(previous + 1, 9))
    else:
        errors.pop(str(cid), None)
        return timeout


async def connection_loop(args, store, processor, state, stop):
    session = load_session(args.session)
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    public_key = args.public_key.read_bytes()
    queue = asyncio.Queue()
    logger = logging.getLogger('tatocollector.transport')
    old = logger.handlers[:], logger.level, logger.propagate
    logger.handlers, logger.propagate = [_Diagnostics(queue, store)], False
    logger.setLevel(logging.INFO)
    delay = 1
    try:
        with TatoUpdatesRegistry(sink=store.put_wire):
            while not stop.is_set():
                try:
                    async with TatoConnection(profile, public_key, host=session['host'], port=session['port'],
                                              dc_id=session['dc_id'], auth_key=bytes.fromhex(session['auth_key_hex']),
                                              time_offset=session['time_offset'], updates_queue=queue) as connection:
                        raw_state = await connection.call(struct.pack('<I', 0xEDD4882A))
                        baseline, _ = processor.schema.decode_prefix(raw_state, 'updates.State')
                        processor.replay()
                        if store.get_checkpoint() is None:
                            store.set_initial_checkpoint({key: baseline[key] for key in ('pts', 'qts', 'seq', 'date')}
                                                         | {'channels': {}, 'captured_since': baseline['date']})
                        state.update(connected=True, session_verified=True, connection_error=None)
                        print('Tatotalk 已连接；独立采集和 HTTP 队列正在运行。', flush=True)
                        delay, next_ping, next_recovery = 1, time.monotonic() + 25, 0
                        next_dialogs, channel_due = 0, {}
                        while not stop.is_set():
                            if time.monotonic() >= next_dialogs:
                                try:
                                    await refresh_dialogs(connection, processor, state)
                                except (ValueError, TLDecodeError):
                                    state['dialogs_error'] = 'dialog_baseline_unavailable'
                                next_dialogs = time.monotonic() + 300
                            if time.monotonic() >= next_recovery:
                                await recover_global(connection, processor, state)
                                next_recovery = time.monotonic() + 30
                            cursor = store.get_checkpoint()
                            for channel in cursor.get('channels', {}):
                                if time.monotonic() >= channel_due.get(channel, 0):
                                    timeout = await recover_channel_safely(connection, processor, int(channel), state)
                                    channel_due[channel] = time.monotonic() + timeout
                                    processor.replay()
                                    if stop.is_set():
                                        break
                            try:
                                update = await asyncio.wait_for(queue.get(), 0.5)
                            except asyncio.TimeoutError:
                                update = None
                            if isinstance(update, RawTatoUpdates):
                                packet_id = getattr(update, 'sample_path', None) or store.put_wire(update.raw)
                                if packet_id not in processor.attempted:
                                    processor.process(packet_id, update.raw)
                                if 'global' in processor.gaps:
                                    next_recovery = min(next_recovery, time.monotonic() + 1)
                                for gap in processor.gaps:
                                    if gap.startswith('channel:'):
                                        channel_due[gap.split(':')[1]] = 0
                            if not connection.sender.is_connected():
                                raise ConnectionError('transport_disconnected')
                            if time.monotonic() >= next_ping:
                                await asyncio.wait_for(connection.sender.send(PingRequest(1)), 10)
                                next_ping = time.monotonic() + 25
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    state['connection_error'] = safe_error(exc)
                finally:
                    state['connected'] = False
                if not stop.is_set():
                    state['reconnect_delay'] = delay
                    await _pause(stop, delay)
                    delay = min(delay * 2, 60)
    finally:
        logger.handlers, logger.level, logger.propagate = old


async def run(args):
    from .outbox import Store
    session = load_session(args.session)
    state = {'running': True, 'connected': False, 'session_verified': False, 'pid': os.getpid(),
             'http_configured': False, 'started_at': datetime.now(timezone.utc).isoformat()}
    stop, tasks = asyncio.Event(), []
    with instance_lock(args.database.parent / 'collector.lock'):
        args.stop_file.unlink(missing_ok=True)
        store = Store(args.database, checkpoint_merger=merge_cursors)
        processor = PacketProcessor(store, session['user_id'])
        try:
            # Import prior diagnostic samples once by content hash. They include
            # the user's real reception test; no synthetic event is sent.
            if args.samples.exists():
                log = WireLog(args.samples)
                for path in sorted(args.samples.glob('*.dpapi')):
                    store.put_wire(log.read(path))
            processor.replay()
            tasks.append(asyncio.create_task(delivery_loop(store, args.config, state, stop)))
            if not args.drain_only:
                tasks.append(asyncio.create_task(connection_loop(args, store, processor, state, stop)))
            end = time.monotonic() + args.seconds if args.seconds else None
            while (end is None or time.monotonic() < end) and not args.stop_file.exists():
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError('collector_worker_stopped')
                state.update(processor.stats)
                state['counts'] = store.counts()
                state['recovery_required'] = sorted(processor.gaps)
                write_status(args.status, state)
                if args.drain_only and not state['counts']['pending']:
                    break
                await asyncio.sleep(0.5)
        finally:
            stop.set()
            # Stop receiving promptly; let a bounded in-flight HTTP attempt
            # finish so that its receipt is durably recorded before exit.
            if len(tasks) > 1:
                tasks[1].cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            state.update(running=False, connected=False, counts=store.counts(), **processor.stats)
            state['recovery_required'] = sorted(processor.gaps)
            write_status(args.status, state)
            store.close()
            args.stop_file.unlink(missing_ok=True)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=0, help='0 means run until stopped')
    parser.add_argument('--drain-only', action='store_true', help='Send saved events without connecting to Tatotalk')
    parser.add_argument('--session', type=Path, default=Path('sessions/default.dpapi'))
    parser.add_argument('--profile', type=Path, default=Path('config/client-profile.local.json'))
    parser.add_argument('--public-key', type=Path, default=Path('research/android-static/embedded-public-0.pem'))
    parser.add_argument('--config', type=Path, default=Path('config/collector.local.json'))
    parser.add_argument('--database', type=Path, default=Path('.data/collector.sqlite3'))
    parser.add_argument('--samples', type=Path, default=Path('.data/wire'))
    parser.add_argument('--status', type=Path, default=Path('.data/collector-status.json'))
    parser.add_argument('--stop-file', type=Path, default=Path('.data/stop.request'))
    args = parser.parse_args(argv)
    if args.seconds < 0:
        parser.error('seconds must not be negative')
    try:
        result = asyncio.run(run(args))
    except KeyboardInterrupt:
        print('采集器已停止；未发送的队列将在下次启动时继续。')
        return 0
    except Exception as exc:
        if type(exc) is RuntimeError and str(exc) == 'collector_already_running':
            print('采集器已在运行。可双击 show-status.cmd 查看状态，或用 stop-collector.cmd 停止。')
            return 2
        print(json.dumps(safe_error(exc), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
