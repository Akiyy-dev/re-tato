import ctypes
from ctypes import wintypes
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


def make_state():
    # Only disposable, randomly generated test material: never an account key.
    return {
        'auth_key_hex': secrets.token_hex(256),
        'user_id': 123456,
        'host': 'session-test.invalid',
        'port': 443,
        'dc_id': 2,
        'time_offset': -17,
    }


def protect_fixture(plaintext):
    """Produce a real DPAPI fixture without calling the module under test."""
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD),
                    ('data', ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(plaintext)).from_buffer_copy(plaintext)
    source = Blob(len(plaintext), buffer)
    result = Blob()
    if not crypt32.CryptProtectData(
            ctypes.byref(source), None, None, None, None, 1,
            ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        blob = ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))
    return (b'TATOSESSION\x00\x01' + struct.pack('<I', len(blob))
            + hashlib.sha256(blob).digest() + blob)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec('tatocollector.session_store'),
            'The DPAPI session store is not implemented',
        )
        self.store = importlib.import_module('tatocollector.session_store')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'new-session.dpapi'

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_real_dpapi_round_trip_restores_every_field(self):
        state = make_state()
        state['test_note'] = 'test metadata \u6d4b\u8bd5'
        self.store.save_session(self.path, state)
        # Avoid printing even synthetic keys if an assertion fails.
        self.assertTrue(self.store.load_session(self.path) == state,
                        'Restored session differs from the input')

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_same_windows_account_can_restore_in_a_new_process(self):
        state = make_state()
        self.store.save_session(self.path, state)
        expected = hashlib.sha256(json.dumps(
            state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result = subprocess.run([
            sys.executable, '-B', '-c',
            'import hashlib,json,sys; from pathlib import Path; '
            'from tatocollector.session_store import load_session; '
            'state=load_session(Path(sys.argv[1])); '
            'print(hashlib.sha256(json.dumps(state,sort_keys=True,'
            'separators=(",",":")).encode()).hexdigest())',
            str(self.path),
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_session_file_contains_neither_key_nor_plaintext_metadata(self):
        state = make_state()
        state['test_note'] = 'metadata-that-must-also-be-encrypted'
        self.store.save_session(self.path, state)
        contents = self.path.read_bytes()
        for sensitive in (state['auth_key_hex'].encode(),
                          bytes.fromhex(state['auth_key_hex']),
                          state['host'].encode(), state['test_note'].encode()):
            self.assertFalse(sensitive in contents,
                             'Session file contains plaintext material')

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_corruption_truncation_and_appended_bytes_are_rejected(self):
        self.store.save_session(self.path, make_state())
        original = self.path.read_bytes()
        changed = bytearray(original)
        changed[len(changed) // 2] ^= 1
        changed_end = bytearray(original)
        changed_end[-1] ^= 1
        for label, data in (
            ('modified ciphertext', bytes(changed)),
            ('modified final byte', bytes(changed_end)),
            ('truncated', original[:-1]),
            ('appended', original + b'extra'),
            ('modified header', b'X' + original[1:]),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.store.load_session(self.path)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_non_dpapi_files_are_rejected(self):
        for label, data in (
            ('empty', b''), ('random', secrets.token_bytes(300)),
            ('plaintext', json.dumps(make_state()).encode()),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.store.load_session(self.path)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_valid_frame_does_not_allow_non_dpapi_payload(self):
        payload = json.dumps(make_state()).encode()
        self.path.write_bytes(
            b'TATOSESSION\x00\x01' + struct.pack('<I', len(payload))
            + hashlib.sha256(payload).digest() + payload)
        with self.assertRaises(ValueError):
            self.store.load_session(self.path)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_decrypted_invalid_json_and_invalid_state_are_rejected(self):
        invalid_state = make_state()
        invalid_state['user_id'] = False
        duplicate_keys = (json.dumps(make_state())[:-1]
                          + ',"user_id":654321}').encode()
        for label, payload in (
            ('invalid JSON', b'not JSON'),
            ('wrong JSON type', b'[]'),
            ('invalid state', json.dumps(invalid_state).encode()),
            ('duplicate keys', duplicate_keys),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(protect_fixture(payload))
                with self.assertRaises(ValueError):
                    self.store.load_session(self.path)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_invalid_state_is_rejected_without_replacing_existing_file(self):
        valid = make_state()
        invalid = [([], 'not an object')]
        for field in valid:
            missing = valid.copy()
            del missing[field]
            invalid.append((missing, 'missing ' + field))
        for field, values in (
            ('auth_key_hex', (None, '', 'a' * 510, 'a' * 514, 'g' * 512,
                              ' ' + 'a' * 511)),
            ('user_id', (True, 0, -1, '123', 1.5)),
            ('host', (None, '', ' ', 'host with space', 'host\nname')),
            ('port', (True, 0, -1, 65536, '443')),
            ('dc_id', (False, 0, -1, '2')),
            ('time_offset', (True, '0', float('inf'), float('nan'))),
        ):
            for index, value in enumerate(values):
                candidate = valid.copy()
                candidate[field] = value
                invalid.append((candidate, f'invalid {field} {index}'))
        original = b'previous session must survive validation errors'
        self.path.write_bytes(original)
        for candidate, label in invalid:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.store.save_session(self.path, candidate)
                self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_non_json_extra_fields_are_rejected(self):
        for label, extra in (
            ('non-string object key', {1: 'value'}),
            ('tuple', (1, 2)), ('object', object()),
            ('nested infinity', [float('inf')]),
        ):
            with self.subTest(case=label):
                state = make_state()
                state['extra'] = extra
                with self.assertRaises(ValueError):
                    self.store.save_session(self.path, state)
                self.assertFalse(self.path.exists())

    @unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
    def test_failed_atomic_replace_keeps_old_session_and_removes_temporary_file(self):
        state = make_state()
        self.store.save_session(self.path, state)
        previous = self.path.read_bytes()
        with patch('tatocollector.session_store.os.replace',
                   side_effect=OSError('simulated filesystem replace failure')):
            with self.assertRaises(OSError):
                self.store.save_session(self.path, make_state())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertTrue(self.store.load_session(self.path) == state,
                        'Previous session is no longer recoverable')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_non_windows_calls_fail_explicitly_before_file_access(self):
        state = make_state()
        with patch.object(sys, 'platform', 'linux'):
            for operation, args in (
                (self.store.save_session, (self.path, state)),
                (self.store.load_session, (self.path,)),
            ):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(RuntimeError, 'Windows'):
                        operation(*args)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
