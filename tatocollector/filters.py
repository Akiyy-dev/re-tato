"""Stable group identity rules and fail-closed local capture configuration."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile


MAX_FILTER_BYTES = 256 * 1024
MAX_FILTER_PEERS = 4096
MAX_CHAT_ID = 2**63 - 1
_KINDS = frozenset(('group', 'channel'))


def _valid_identity(kind, chat_id):
    return (type(kind) is str and kind in _KINDS and type(chat_id) is int
            and 0 < chat_id <= MAX_CHAT_ID)


@dataclass(frozen=True)
class CaptureFilter:
    enabled: bool = False
    peers: frozenset[tuple[str, int]] = field(default_factory=frozenset, repr=False)

    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.peers) is not frozenset
                or len(self.peers) > MAX_FILTER_PEERS):
            raise ValueError('Invalid capture filter rules')
        for peer in self.peers:
            if type(peer) is not tuple or len(peer) != 2 or not _valid_identity(*peer):
                raise ValueError('Invalid capture filter identity')

    def allows(self, event):
        if not self.enabled:
            return True
        if type(event) is not dict:
            return False
        kind, chat_id = event.get('chat_type'), event.get('chat_id')
        return _valid_identity(kind, chat_id) and (kind, chat_id) in self.peers


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate capture filter field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('Invalid capture filter number')


def _decode_config(data):
    if len(data) > MAX_FILTER_BYTES:
        raise ValueError('Capture filter configuration exceeds size limit')
    try:
        document = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid capture filter configuration') from None
    if (type(document) is not dict or set(document) != {'enabled', 'include'}
            or type(document['enabled']) is not bool
            or type(document['include']) is not list
            or len(document['include']) > MAX_FILTER_PEERS):
        raise ValueError('Invalid capture filter configuration')
    peers = set()
    for item in document['include']:
        if (type(item) is not dict or set(item) != {'chat_type', 'chat_id'}
                or not _valid_identity(item['chat_type'], item['chat_id'])):
            raise ValueError('Invalid capture filter configuration')
        peer = item['chat_type'], item['chat_id']
        if peer in peers:
            raise ValueError('Duplicate capture filter identity')
        peers.add(peer)
    return CaptureFilter(document['enabled'], frozenset(peers))


def _load_existing(path):
    # A manager that has already seen a configuration must never turn a
    # deletion between stat/open into the initial compatibility default.
    with Path(path).open('rb') as source:
        data = source.read(MAX_FILTER_BYTES + 1)
    return _decode_config(data)


def load_filter(path):
    """Load a bounded strict document; an initially absent file means all mode."""
    try:
        return _load_existing(path)
    except FileNotFoundError:
        return CaptureFilter()


def save_filter(path, policy):
    """Atomically publish only stable identities, never display names or keys."""
    if not isinstance(policy, CaptureFilter):
        raise ValueError('Capture filter rules are required')
    # Validate again before writing even if a caller modified a frozen object
    # through a low-level escape hatch.
    CaptureFilter(policy.enabled, policy.peers)
    document = {'enabled': policy.enabled, 'include': [
        {'chat_type': kind, 'chat_id': chat_id} for kind, chat_id in sorted(policy.peers)]}
    data = (json.dumps(document, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(data) > MAX_FILTER_BYTES:
        raise ValueError('Capture filter configuration exceeds size limit')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class FilterManager:
    """Reload changed local files and block capture while a seen file is invalid.

    An absent file on first use preserves existing deployments' all mode.
    Once a file has been observed, deletion or unreadability fails closed.
    Status values are fixed summaries; no identities, names or paths are logged.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._policy = CaptureFilter()
        self._signature = None
        self._seen = False
        self._error = None

    def _close(self, reason):
        self._policy = CaptureFilter(enabled=True)
        self._signature = None
        self._error = reason
        return self._policy

    def current(self):
        try:
            metadata = self.path.stat()
        except FileNotFoundError:
            if self._seen:
                return self._close('configuration_unavailable')
            return self._policy
        except OSError:
            self._seen = True
            return self._close('configuration_unavailable')
        self._seen = True
        signature = (metadata.st_mtime_ns, metadata.st_size, metadata.st_ino, metadata.st_ctime_ns)
        if self._signature == signature:
            # Windows ACL changes need not alter this signature. Validate that
            # a cached document remains readable without reparsing its body.
            try:
                with self.path.open('rb'):
                    pass
            except OSError:
                return self._close('configuration_unavailable')
            return self._policy
        try:
            policy = _load_existing(self.path)
        except OSError:
            return self._close('configuration_unavailable')
        except ValueError:
            return self._close('configuration_invalid')
        self._policy, self._signature, self._error = policy, signature, None
        return self._policy

    def status(self):
        policy = self.current()
        return {'enabled': policy.enabled, 'selected_count': len(policy.peers), 'error': self._error}
