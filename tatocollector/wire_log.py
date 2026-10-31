"""Current-user DPAPI storage for captured protocol samples, never plaintext."""

from pathlib import Path
import secrets
import time

from .session_store import _dpapi

MAGIC = b'TATOWIRE\x00\x01'
MAX_RAW = 1_048_576


class WireLog:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def append(self, raw):
        if not isinstance(raw, bytes) or not 4 <= len(raw) <= MAX_RAW:
            raise ValueError('wire sample exceeds bounded size')
        encrypted = MAGIC + _dpapi(raw)
        path = self.directory / f'{time.time_ns()}-{secrets.token_hex(4)}.dpapi'
        temporary = path.with_suffix('.tmp')
        try:
            temporary.write_bytes(encrypted)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def read(self, path):
        with Path(path).open('rb') as source:
            encrypted = source.read(MAX_RAW + 65537)
        if len(encrypted) > MAX_RAW + 65536 or not encrypted.startswith(MAGIC):
            raise ValueError('invalid encrypted wire sample')
        raw = _dpapi(encrypted[len(MAGIC):], decrypt=True)
        if not 4 <= len(raw) <= MAX_RAW:
            raise ValueError('invalid decrypted wire sample size')
        return raw
