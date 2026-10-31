"""Preserve this collector's new transport while login parsing is incomplete.

This is deliberately separate from an authenticated session: user_id is not an
accepted field. Version 1 stores zlib-compressed UTF-8 JSON inside current-user
Windows DPAPI, preceded by a version marker, blob length and SHA-256 checksum.
Compression keeps a 64 KiB authorization response represented as hex below the
128 KiB ciphertext limit. Both file reads and decompression are bounded.
"""

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import zlib

from .session_store import _dpapi


_MAGIC = b'TATOPENDING\x00\x01'
_HEADER_SIZE = len(_MAGIC) + 4 + 32
_MAX_AUTH_RESULT_HEX = 64 * 1024 * 2
_MAX_CIPHERTEXT_BYTES = 128 * 1024
_MAX_JSON_BYTES = 144 * 1024
_REQUIRED_FIELDS = frozenset(('auth_key_hex', 'host', 'port', 'dc_id', 'time_offset'))
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {'auth_result_hex'}


def _require_windows():
    if sys.platform != 'win32':
        raise RuntimeError('Pending login storage requires Windows DPAPI')


def _validate_state(state):
    if type(state) is not dict:
        raise ValueError('Pending login state must be a JSON object')
    if any(type(field) is not str for field in state):
        raise ValueError('Pending login field names must be strings')
    if state.keys() - _ALLOWED_FIELDS:
        raise ValueError('Pending login state contains unsupported fields')
    if _REQUIRED_FIELDS - state.keys():
        raise ValueError('Pending login state is missing required transport fields')
    key = state['auth_key_hex']
    if (type(key) is not str or len(key) != 512
            or re.fullmatch(r'[0-9a-fA-F]{512}', key) is None):
        raise ValueError('auth_key_hex must encode exactly 256 bytes as hex')
    host = state['host']
    if (type(host) is not str or not 1 <= len(host) <= 253
            or any(character.isspace() or ord(character) < 33
                   or ord(character) == 127 for character in host)):
        raise ValueError('host must contain 1 to 253 characters without whitespace')
    port = state['port']
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('port must be an integer from 1 to 65535')
    dc_id = state['dc_id']
    if type(dc_id) is not int or dc_id <= 0:
        raise ValueError('dc_id must be a positive integer')
    offset = state['time_offset']
    if (type(offset) not in (int, float)
            or (type(offset) is float and not math.isfinite(offset))):
        raise ValueError('time_offset must be a finite number')
    if 'auth_result_hex' in state:
        result = state['auth_result_hex']
        if (type(result) is not str or len(result) > _MAX_AUTH_RESULT_HEX
                or len(result) % 2 or re.fullmatch(r'[0-9a-fA-F]*', result) is None):
            raise ValueError('auth_result_hex must encode at most 64 KiB as hex')


def save_pending(path: Path, state: dict) -> None:
    """Encrypt validated transport state and atomically replace an existing file.

    The caller creates the parent directory. Validation and encryption happen
    before any file is changed; temporary files contain only encrypted data.
    Invalid input raises ValueError, while filesystem/DPAPI failures are OSError.
    """
    _require_windows()
    _validate_state(state)
    try:
        plaintext = json.dumps(state, ensure_ascii=False, allow_nan=False,
                               separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Pending login state cannot be encoded as UTF-8 JSON') from None
    if len(plaintext) > _MAX_JSON_BYTES:
        raise ValueError('Pending login JSON exceeds the storage limit')
    protected = _dpapi(zlib.compress(plaintext))
    if not 0 < len(protected) <= _MAX_CIPHERTEXT_BYTES:
        raise ValueError('Pending login ciphertext exceeds the storage limit')
    contents = (_MAGIC + struct.pack('<I', len(protected))
                + hashlib.sha256(protected).digest() + protected)
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_ciphertext(path):
    with Path(path).open('rb') as source:
        header = source.read(_HEADER_SIZE)
        if len(header) != _HEADER_SIZE or not header.startswith(_MAGIC):
            raise ValueError('Not a supported pending-login DPAPI file')
        length = struct.unpack_from('<I', header, len(_MAGIC))[0]
        if not 0 < length <= _MAX_CIPHERTEXT_BYTES:
            raise ValueError('Pending login ciphertext exceeds the storage limit')
        protected = source.read(length + 1)
    if (len(protected) != length
            or not hmac.compare_digest(header[len(_MAGIC) + 4:],
                                       hashlib.sha256(protected).digest())):
        raise ValueError('Pending login file is damaged')
    return protected


def _decompress(compressed):
    decoder = zlib.decompressobj()
    try:
        plaintext = decoder.decompress(compressed, _MAX_JSON_BYTES + 1)
    except zlib.error:
        raise ValueError('Pending login payload is not valid compressed JSON') from None
    if (len(plaintext) > _MAX_JSON_BYTES or decoder.unconsumed_tail
            or not decoder.eof or decoder.unused_data):
        raise ValueError('Pending login payload is incomplete or exceeds its limit')
    return plaintext


def _unique_object(pairs):
    result = {}
    for field, value in pairs:
        if field in result:
            raise ValueError('Pending login JSON contains duplicate fields')
        result[field] = value
    return result


def _reject_constant(_value):
    raise ValueError('Pending login JSON numbers must be finite')


def load_pending(path: Path) -> dict:
    """Restore this collector's pending transport without asserting login success.

    Invalid files, other users' DPAPI blobs and invalid decrypted state raise
    ValueError. No raw response or key is included in error messages. Missing or
    unreadable files retain the usual OSError; non-Windows calls are RuntimeError.
    """
    _require_windows()
    protected = _read_ciphertext(path)
    try:
        compressed = _dpapi(protected, decrypt=True)
    except OSError:
        raise ValueError('Pending login cannot be decrypted by this Windows user') from None
    plaintext = _decompress(compressed)
    try:
        state = json.loads(plaintext.decode('utf-8'),
                           object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Pending login payload is not valid UTF-8 JSON') from None
    _validate_state(state)
    return state
