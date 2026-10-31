"""Bounded passive live-reception verification; no HTTP forwarding in this probe."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import struct
import time

from telethon.tl.functions import PingRequest

from .client import TatoConnection, safe_error
from .events import PeerCache, normalize_updates
from .live_wire import RawTatoUpdates, TatoUpdatesRegistry
from .session_store import load_session
from .tl import Schema, TLDecodeError
from .wire_log import WireLog


def load_schema():
    paths = [Path('research') / name for name in (
        'message-wire-schema.tl', 'user-chat-schema.tl', 'message-dependencies-schema.tl',
        'message-metadata-updates-schema.tl', 'difference-schema.tl', 'dialog-schema.tl',
        'media-schema.tl', 'actions-schema.tl', 'channel-updates-schema.tl')]
    schema = Schema.from_files(path for path in paths if path.exists())
    schema.add('updatesTooLong#e317af7e = Updates;')
    return schema


def new_stats():
    return {'connected': False, 'session_verified': False, 'received': 0, 'decoded': 0,
            'messages': 0, 'text_messages': 0, 'unknown_constructors': {}, 'decode_errors': {},
            'root_constructors': {}, 'recovery_required': False}


def process_update(raw, schema, peers, account_id, log, stats, *, already_saved=False):
    if not already_saved:
        log.append(raw)
    stats['received'] += 1
    constructor = f'{struct.unpack_from("<I", raw)[0]:08x}'
    stats['root_constructors'][constructor] = stats['root_constructors'].get(constructor, 0) + 1
    try:
        parsed, _ = schema.decode_prefix(raw, expected_type='Updates')
        events = normalize_updates(parsed, account_id, peers)
        stats['decoded'] += 1
        stats['messages'] += len(events)
        stats['text_messages'] += sum(bool(event['text'].strip()) for event in events)
        if parsed.get('_') == 'updatesTooLong':
            stats['recovery_required'] = True
        return events
    except TLDecodeError as exc:
        stats['decode_errors'][exc.reason] = stats['decode_errors'].get(exc.reason, 0) + 1
        if exc.constructor is not None:
            key = f'{exc.constructor:08x}'
            stats['unknown_constructors'][key] = stats['unknown_constructors'].get(key, 0) + 1
        return []


class _ProtocolDiagnostics(logging.Handler):
    def __init__(self, queue, log):
        super().__init__()
        self.queue, self.log = queue, log

    def emit(self, record):
        # Never format a dependency log record: it may include raw message text.
        if str(record.msg).startswith('Type %08x not found') and record.args:
            raw = record.args[1]
            if isinstance(raw, bytes):
                event = RawTatoUpdates(raw)
                event.sample_path = self.log.append(raw)
                self.queue.put_nowait(event)


def _write_status(path, stats):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**stats, 'updated_at': datetime.now(timezone.utc).isoformat()},
                               ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


async def run(args):
    state = load_session(args.session)
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    key = args.public_key.read_bytes()
    log = WireLog(args.samples)
    schema, peers, queue, stats = load_schema(), PeerCache(), asyncio.Queue(), new_stats()
    transport_logger = logging.getLogger('tatocollector.transport')
    old_handlers, old_propagate, old_level = transport_logger.handlers[:], transport_logger.propagate, transport_logger.level
    transport_logger.handlers = [_ProtocolDiagnostics(queue, log)]
    transport_logger.propagate = False
    transport_logger.setLevel(logging.INFO)
    try:
        with TatoUpdatesRegistry(sink=log.append):
            async with TatoConnection(profile, key, host=state['host'], port=state['port'], dc_id=state['dc_id'],
                                      auth_key=bytes.fromhex(state['auth_key_hex']), time_offset=state['time_offset'],
                                      updates_queue=queue) as connection:
                stats['connected'] = True
                response = await connection.call(struct.pack('<I', 0xEDD4882A))
                current_state, _ = schema.decode_prefix(response, expected_type='updates.State')
                stats['session_verified'] = True
                stats['state_kind'] = current_state['_']
                _write_status(args.status, stats)
                end = time.monotonic() + args.seconds
                next_ping = time.monotonic() + 25
                while time.monotonic() < end:
                    try:
                        update = await asyncio.wait_for(queue.get(), min(1, end - time.monotonic()))
                    except asyncio.TimeoutError:
                        update = None
                    if update is not None and isinstance(update, RawTatoUpdates):
                        process_update(update.raw, schema, peers, state['user_id'], log, stats,
                                       already_saved=bool(getattr(update, 'sample_path', None)))
                    if time.monotonic() >= next_ping:
                        await asyncio.wait_for(connection.sender.send(PingRequest(1)), 10)
                        next_ping = time.monotonic() + 25
                    _write_status(args.status, stats)
    except Exception as exc:
        stats.update(safe_error(exc))
    finally:
        stats['connected'] = False
        transport_logger.handlers, transport_logger.propagate = old_handlers, old_propagate
        transport_logger.setLevel(old_level)
    _write_status(args.status, stats)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--session', type=Path, default=Path('sessions/default.dpapi'))
    parser.add_argument('--profile', type=Path, default=Path('config/client-profile.local.json'))
    parser.add_argument('--public-key', type=Path, default=Path('research/android-static/embedded-public-0.pem'))
    parser.add_argument('--samples', type=Path, default=Path('.data/wire'))
    parser.add_argument('--status', type=Path, default=Path('.data/capture-status.json'))
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error('seconds must be between 1 and 300')
    stats = asyncio.run(run(args))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if stats['session_verified'] and 'error_type' not in stats else 1


if __name__ == '__main__':
    raise SystemExit(main())
