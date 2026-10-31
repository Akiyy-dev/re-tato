"""Durable, current-user DPAPI inbox/outbox for the standalone collector.

Only identifiers, hashes and delivery counters/status are plaintext SQLite
metadata. Wire bodies, captured events, frozen payloads, checkpoints and failure
details are encrypted before SQLite sees them. No network or profile is used.
"""

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import hmac
import json
import math
from pathlib import Path
import sqlite3

from .ingest import DeliveryResult, make_payload
from .session_store import _dpapi


_MAX_WIRE = 1024 * 1024


def _check_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json(item)
        return
    raise ValueError('Outbox business data must contain only JSON values')


def _seal_object(value):
    if type(value) is not dict:
        raise ValueError('Outbox business data must be an object')
    try:
        _check_json(value)
        data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Outbox business data is not valid UTF-8 JSON') from None
    return _dpapi(data)


def _open_object(protected):
    try:
        data = _dpapi(protected, decrypt=True)
        value = json.loads(data.decode('utf-8'))
        if type(value) is not dict:
            raise ValueError('Not an object')
        _check_json(value)
        return value
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Encrypted outbox business data cannot be restored') from None


def _number(value, name):
    try:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Not finite')
        return float(value)
    except (ValueError, OverflowError):
        raise ValueError(name + ' must be a finite number') from None


def _positive_integer(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(name + ' must be a positive integer')


def _event_key(event):
    if type(event) is not dict:
        raise ValueError('Captured event must be an object')
    for name in ('account_id', 'message_id'):
        _positive_integer(event.get(name), name)
    if type(event.get('chat_id')) is not int or event['chat_id'] == 0:
        raise ValueError('Captured event requires a nonzero integer chat_id')
    if event.get('chat_type') not in ('private', 'group', 'channel'):
        raise ValueError('Captured event requires a supported chat_type')
    try:
        key = (f"tato:{event['account_id']}:{event['chat_type']}:"
               f"{event['chat_id']}:{event['message_id']}")
    except ValueError:
        raise ValueError('Captured event identity is out of range') from None
    return key


class Store:
    """One synchronous SQLite connection; use it from its creating thread."""

    def __init__(self, path: Path, checkpoint_merger=None):
        if checkpoint_merger is not None and not callable(checkpoint_merger):
            raise ValueError('checkpoint_merger must be callable or null')
        self._checkpoint_merger = checkpoint_merger
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=5, isolation_level=None)
        self._closed = False
        try:
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('PRAGMA synchronous=FULL')
            self._conn.execute('PRAGMA foreign_keys=ON')
            self._conn.executescript('''
                CREATE TABLE IF NOT EXISTS wires (
                    id INTEGER PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    protected BLOB NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending','failed','processed')),
                    diagnostic BLOB,
                    checkpoint BLOB
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    event_key TEXT NOT NULL UNIQUE,
                    packet_id INTEGER NOT NULL REFERENCES wires(id),
                    protected_event BLOB NOT NULL,
                    protected_payload BLOB,
                    state TEXT NOT NULL
                        CHECK(state IN ('pending','delivered','auth_error','rejected','no_text')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    receipt_id INTEGER,
                    last_result BLOB
                );
                CREATE INDEX IF NOT EXISTS delivery_due ON events(state,next_attempt,id);
                CREATE INDEX IF NOT EXISTS wire_state ON wires(state,id);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    packet_id INTEGER NOT NULL,
                    protected BLOB NOT NULL
                );
            ''')
        except BaseException:
            self._conn.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self):
        self._conn.execute('BEGIN IMMEDIATE')
        try:
            yield
            self._conn.execute('COMMIT')
        except BaseException:
            if self._conn.in_transaction:
                self._conn.execute('ROLLBACK')
            raise

    def put_wire(self, raw: bytes) -> int:
        """Synchronously encrypt and FULL-commit before the caller may acknowledge."""
        if type(raw) is not bytes or len(raw) > _MAX_WIRE:
            raise ValueError('Raw wire must be bytes no larger than 1 MiB')
        digest = hashlib.sha256(raw).hexdigest()
        with self._transaction():
            existing = self._conn.execute(
                'SELECT id FROM wires WHERE sha256=?', (digest,)).fetchone()
            if existing:
                packet_id = existing[0]
            else:
                protected = _dpapi(raw)
                packet_id = self._conn.execute(
                    'INSERT INTO wires(sha256,protected) VALUES (?,?)',
                    (digest, protected)).lastrowid
        return packet_id

    def pending_wires(self, limit=100, after_id=0) -> list[tuple[int, bytes]]:
        """Page pending/failed packets in ID order without starving newer work."""
        if type(limit) is not int or limit <= 0:
            raise ValueError('Pending wire limit must be a positive integer')
        if type(after_id) is not int or after_id < 0:
            raise ValueError('Pending wire cursor must be a nonnegative integer')
        rows = self._conn.execute(
            "SELECT id,protected,sha256 FROM wires WHERE state!='processed' AND id>? ORDER BY id LIMIT ?",
            (after_id, limit)).fetchall()
        result = []
        for packet_id, protected, digest in rows:
            try:
                raw = _dpapi(protected, decrypt=True)
            except OSError:
                raise ValueError('Encrypted raw wire cannot be restored') from None
            if len(raw) > _MAX_WIRE or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest):
                raise ValueError('Encrypted raw wire failed integrity validation')
            result.append((packet_id, raw))
        return result

    def _packet_state(self, packet_id):
        _positive_integer(packet_id, 'packet_id')
        row = self._conn.execute('SELECT state FROM wires WHERE id=?', (packet_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown packet_id')
        return row[0]

    def commit_packet(self, packet_id: int, events: list[dict],
                      checkpoint: dict | None = None) -> None:
        """Atomically capture all events, cursor candidate and processed packet state.

        Existing event identities retain their original event/payload. A cursor
        is promoted only through the contiguous processed prefix of wire IDs.
        Decode the complete packet before calling: partial parsing is a failure.
        """
        if type(events) is not list or (checkpoint is not None and type(checkpoint) is not dict):
            raise ValueError('Packet events must be a list and checkpoint an object or null')
        with self._transaction():
            if self._packet_state(packet_id) == 'processed':
                return
            for event in events:
                key = _event_key(event)
                if self._conn.execute('SELECT 1 FROM events WHERE event_key=?', (key,)).fetchone():
                    continue
                protected_event = _seal_object(event)
                protected_result = None
                try:
                    payload = make_payload(event)
                except ValueError:
                    payload = None
                    state = 'rejected'
                    protected_result = _seal_object(asdict(DeliveryResult(
                        'rejected', reason='payload_validation_error')))
                else:
                    state = 'pending' if payload is not None else 'no_text'
                protected_payload = _seal_object(payload) if payload is not None else None
                self._conn.execute('''
                    INSERT INTO events(event_key,packet_id,protected_event,protected_payload,state,last_result)
                    VALUES (?,?,?,?,?,?)
                ''', (key, packet_id, protected_event, protected_payload,
                      state, protected_result))
            protected_checkpoint = _seal_object(checkpoint) if checkpoint is not None else None
            self._conn.execute(
                "UPDATE wires SET state='processed',checkpoint=?,diagnostic=NULL WHERE id=?",
                (protected_checkpoint, packet_id))
            self._promote_checkpoint()

    def _promote_checkpoint(self):
        gap = self._conn.execute("SELECT MIN(id) FROM wires WHERE state!='processed'").fetchone()[0]
        if self._checkpoint_merger is not None:
            current = self._conn.execute(
                'SELECT packet_id,protected FROM checkpoints WHERE singleton=1').fetchone()
            after_id = current[0] if current is not None else 0
            candidates = self._conn.execute('''
                SELECT id,checkpoint FROM wires
                WHERE state='processed' AND checkpoint IS NOT NULL
                    AND id>? AND (? IS NULL OR id<?)
                ORDER BY id
            ''', (after_id, gap, gap))
            merged = _open_object(current[1]) if current is not None else {}
            last_id = None
            for packet_id, protected in candidates:
                merged = self._checkpoint_merger(merged, _open_object(protected))
                if type(merged) is not dict:
                    raise ValueError('Checkpoint merger must return an object')
                last_id = packet_id
            if last_id is None:
                return
            protected = _seal_object(merged)
            self._conn.execute('''
                INSERT INTO checkpoints(singleton,packet_id,protected) VALUES (1,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    packet_id=excluded.packet_id,protected=excluded.protected
            ''', (last_id, protected))
            return
        candidate = self._conn.execute('''
            SELECT id,checkpoint FROM wires
            WHERE state='processed' AND checkpoint IS NOT NULL AND (? IS NULL OR id<?)
            ORDER BY id DESC LIMIT 1
        ''', (gap, gap)).fetchone()
        if candidate is None:
            return
        current = self._conn.execute('SELECT packet_id FROM checkpoints WHERE singleton=1').fetchone()
        if current is None or candidate[0] > current[0]:
            self._conn.execute('''
                INSERT INTO checkpoints(singleton,packet_id,protected) VALUES (1,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                    packet_id=excluded.packet_id,protected=excluded.protected
            ''', candidate)

    def fail_packet(self, packet_id: int, reason: str, constructor: str | None) -> None:
        """Retain encrypted raw bytes and diagnostics for a later decoder."""
        if (type(reason) is not str or not reason or len(reason) > 256
                or (constructor is not None and (type(constructor) is not str or len(constructor) > 128))):
            raise ValueError('Packet failure metadata has an invalid type or length')
        with self._transaction():
            if self._packet_state(packet_id) == 'processed':
                raise ValueError('An already processed packet cannot be marked failed')
            diagnostic = _seal_object({'reason': reason, 'constructor': constructor})
            self._conn.execute("UPDATE wires SET state='failed',diagnostic=? WHERE id=?",
                               (diagnostic, packet_id))

    def get_checkpoint(self) -> dict | None:
        row = self._conn.execute('SELECT protected FROM checkpoints WHERE singleton=1').fetchone()
        return _open_object(row[0]) if row is not None else None

    def set_initial_checkpoint(self, checkpoint: dict) -> None:
        """Set a first baseline only; never overwrite a cursor or skip buffered work."""
        with self._transaction():
            if self._conn.execute('SELECT 1 FROM checkpoints WHERE singleton=1').fetchone():
                return
            if self._conn.execute("SELECT 1 FROM wires WHERE state!='processed' LIMIT 1").fetchone():
                raise ValueError('Initial checkpoint cannot skip unprocessed packets')
            protected = _seal_object(checkpoint)
            self._conn.execute('INSERT INTO checkpoints VALUES (1,0,?)', (protected,))

    def next_delivery(self, now: float, *, accepts=None) -> tuple[str, dict, int] | None:
        now = _number(now, 'now')
        if accepts is not None and not callable(accepts):
            raise ValueError('Delivery filter must be callable or null')
        rows = self._conn.execute('''
            SELECT event_key,protected_payload,attempts,protected_event FROM events
            WHERE state='pending' AND next_attempt<=?
                AND NOT EXISTS (SELECT 1 FROM events WHERE state='auth_error')
            ORDER BY id
        ''' + (' LIMIT 1' if accepts is None else ''), (now,))
        # Check the encrypted event identity, preserving both the frozen payload
        # and delivery state for excluded old pending items. An excluded first
        # row must not prevent a later selected message from being sent.
        for row in rows:
            if accepts is None or accepts(_open_object(row[3])):
                return row[0], _open_object(row[1]), row[2]
        return None

    def record_delivery(self, event_key: str, result: DeliveryResult, now: float) -> None:
        now = _number(now, 'now')
        if (type(event_key) is not str or not event_key or not isinstance(result, DeliveryResult)
                or result.kind not in ('accepted', 'retry', 'auth_error', 'rejected')
                or type(result.reason) is not str or len(result.reason) > 256):
            raise ValueError('Invalid delivery result or event identity')
        if (result.status_code is not None and
                (type(result.status_code) is not int or not 100 <= result.status_code <= 599)):
            raise ValueError('Delivery status code is invalid')
        if result.receipt_id is not None:
            _positive_integer(result.receipt_id, 'receipt_id')
        if result.kind == 'accepted' and (result.status_code not in (200, 202) or result.receipt_id is None):
            raise ValueError('Accepted delivery requires a valid 200/202 receipt')
        retry_after = None
        if result.retry_after is not None:
            retry_after = _number(result.retry_after, 'retry_after')
            if retry_after < 0:
                raise ValueError('retry_after must not be negative')
        with self._transaction():
            row = self._conn.execute('SELECT state,attempts FROM events WHERE event_key=?',
                                     (event_key,)).fetchone()
            if row is None:
                raise ValueError('Unknown event identity')
            if row[0] != 'pending':
                return
            state = {'accepted': 'delivered', 'retry': 'pending',
                     'auth_error': 'auth_error', 'rejected': 'rejected'}[result.kind]
            next_attempt = 0.0
            if result.kind == 'retry':
                delay = max(min(2 ** min(row[1], 9), 300), retry_after or 0)
                next_attempt = _number(now + delay, 'next_attempt')
            protected_result = _seal_object(asdict(result))
            self._conn.execute('''
                UPDATE events SET state=?,attempts=attempts+1,next_attempt=?,
                    receipt_id=?,last_result=? WHERE event_key=?
            ''', (state, next_attempt, result.receipt_id, protected_result, event_key))

    def retry_auth_errors(self) -> None:
        with self._transaction():
            self._conn.execute("UPDATE events SET state='pending',next_attempt=0 WHERE state='auth_error'")

    def counts(self, *, accepts=None) -> dict:
        names = ('captured', 'pending', 'delivered', 'rejected', 'no_text', 'undecoded', 'auth_error')
        row = self._conn.execute('''
            SELECT (SELECT COUNT(*) FROM events),
                (SELECT COUNT(*) FROM events WHERE state='pending'),
                (SELECT COUNT(*) FROM events WHERE state='delivered'),
                (SELECT COUNT(*) FROM events WHERE state='rejected'),
                (SELECT COUNT(*) FROM events WHERE state='no_text'),
                (SELECT COUNT(*) FROM wires WHERE state!='processed'),
                (SELECT COUNT(*) FROM events WHERE state='auth_error')
        ''').fetchone()
        counts = dict(zip(names, row))
        if accepts is not None:
            if not callable(accepts):
                raise ValueError('Delivery filter must be callable or null')
            pending = self._conn.execute("SELECT protected_event FROM events WHERE state='pending'")
            counts['filter_held'] = sum(not accepts(_open_object(item[0])) for item in pending)
        return counts

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True
