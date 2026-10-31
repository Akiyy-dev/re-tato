"""Current-user Windows DPAPI storage for a newly created collector session.

The file contains a version marker, little-endian blob length, SHA-256 checksum,
and a DPAPI blob. The checksum detects file corruption, including metadata DPAPI
may ignore; confidentiality and authentication are supplied by DPAPI itself.
No existing application's profile or session directory is consulted.
"""

import ctypes
from ctypes import wintypes
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


_MAGIC = b'TATOSESSION\x00\x01'
_HEADER_SIZE = len(_MAGIC) + 4 + 32
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _require_windows():
    if sys.platform != 'win32':
        raise RuntimeError('Session storage requires Windows DPAPI')


def _validate_json(value, ancestors=None):
    """Reject values JSON would silently coerce or cannot round-trip."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError('Session JSON numbers must be finite')
        return
    if type(value) not in (dict, list):
        raise ValueError('Session state must contain only JSON values')
    ancestors = set() if ancestors is None else ancestors
    if id(value) in ancestors:
        raise ValueError('Session state must not contain circular references')
    ancestors.add(id(value))
    try:
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise ValueError('Session JSON object keys must be strings')
            values = value.values()
        else:
            values = value
        for item in values:
            _validate_json(item, ancestors)
    finally:
        ancestors.remove(id(value))


def _validate_state(state):
    if type(state) is not dict:
        raise ValueError('Session state must be a JSON object')
    required = ('auth_key_hex', 'user_id', 'host', 'port', 'dc_id', 'time_offset')
    for field in required:
        if field not in state:
            raise ValueError('Session state is missing ' + field)
    key = state['auth_key_hex']
    if type(key) is not str or re.fullmatch(r'[0-9a-fA-F]{512}', key) is None:
        raise ValueError('auth_key_hex must encode exactly 256 bytes as hex')
    for field in ('user_id', 'dc_id'):
        if type(state[field]) is not int or state[field] <= 0:
            raise ValueError(field + ' must be a positive integer')
    host = state['host']
    if (type(host) is not str or not host
            or any(character.isspace() or ord(character) < 33
                   or ord(character) == 127 for character in host)):
        raise ValueError('host must be a nonempty host without whitespace')
    port = state['port']
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('port must be an integer from 1 to 65535')
    offset = state['time_offset']
    if (type(offset) not in (int, float)
            or (type(offset) is float and not math.isfinite(offset))):
        raise ValueError('time_offset must be a finite number')
    try:
        _validate_json(state)
    except RecursionError:
        raise ValueError('Session JSON nesting is too deep') from None


def _dpapi(data, *, decrypt=False):
    """Protect/unprotect without UI or machine-wide sharing, then free buffers."""
    _require_windows()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = (crypt32.CryptUnprotectData if decrypt
                else crypt32.CryptProtectData)
    description_type = (ctypes.POINTER(wintypes.LPWSTR) if decrypt
                        else wintypes.LPCWSTR)
    function.argtypes = [
        ctypes.POINTER(_DataBlob), description_type,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _DataBlob(len(data), source_buffer)
    result = _DataBlob()
    try:
        if not function(ctypes.byref(source), None, None, None, None,
                        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        if not decrypt:
            ctypes.memset(source_buffer, 0, len(data))
        if result.pbData:
            if decrypt:
                ctypes.memset(result.pbData, 0, result.cbData)
            kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save_session(path: Path, state: dict) -> None:
    """Validate and encrypt the complete state, then atomically replace path.

    Only ciphertext is ever written to disk. Validation/encryption/replacement
    errors leave a previously saved session intact. The parent directory must
    already exist; filesystem and DPAPI failures are reported as OSError.
    """
    _require_windows()
    _validate_state(state)
    try:
        plaintext = json.dumps(state, ensure_ascii=False, allow_nan=False,
                               separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Session state cannot be encoded as UTF-8 JSON') from None
    protected = _dpapi(plaintext)
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


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Session JSON contains duplicate object keys')
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise ValueError('Session JSON numbers must be finite')


def load_session(path: Path) -> dict:
    """Restore a current-user DPAPI session or reject invalid/corrupt content.

    Missing/unreadable files retain their usual OSError. Invalid formats,
    decryption failures (including another user's blob), and invalid decrypted
    state raise ValueError without including any session data in the error.
    """
    _require_windows()
    contents = Path(path).read_bytes()
    if len(contents) <= _HEADER_SIZE or not contents.startswith(_MAGIC):
        raise ValueError('Not a supported DPAPI session file')
    length_offset = len(_MAGIC)
    length = struct.unpack_from('<I', contents, length_offset)[0]
    digest = contents[length_offset + 4:_HEADER_SIZE]
    protected = contents[_HEADER_SIZE:]
    if (length != len(protected)
            or not hmac.compare_digest(digest, hashlib.sha256(protected).digest())):
        raise ValueError('DPAPI session file is damaged')
    try:
        plaintext = _dpapi(protected, decrypt=True)
    except OSError:
        raise ValueError('Session cannot be decrypted by this Windows user') from None
    try:
        state = json.loads(plaintext.decode('utf-8'),
                           object_pairs_hook=_unique_object,
                           parse_constant=_reject_json_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Decrypted session is not valid UTF-8 JSON') from None
    _validate_state(state)
    return state
