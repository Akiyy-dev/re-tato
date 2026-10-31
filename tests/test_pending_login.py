from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


def make_state():
    # Disposable synthetic transport key; not any account's session material.
    return {
        'auth_key_hex': secrets.token_hex(256),
        'host': 'pending-test.invalid',
        'port': 443,
        'dc_id': 2,
        'time_offset': -15,
    }


def frame(ciphertext):
    return (b'TATOPENDING\x00\x01' + struct.pack('<I', len(ciphertext))
            + hashlib.sha256(ciphertext).digest() + ciphertext)


@unittest.skipUnless(sys.platform == 'win32', 'real Windows DPAPI required')
class PendingLoginTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec('tatocollector.pending_login'),
            'The independent pending-login store is not implemented',
        )
        self.store = importlib.import_module('tatocollector.pending_login')
        self.dpapi = importlib.import_module('tatocollector.session_store')._dpapi
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'pending-test.dpapi'

    def protect_payload(self, plaintext):
        return frame(self.dpapi(zlib.compress(plaintext)))

    def test_real_dpapi_restores_transport_before_authorization_without_user_id(self):
        state = make_state()
        self.store.save_pending(self.path, state)
        restored = self.store.load_pending(self.path)
        self.assertTrue(restored == state, 'Pending transport did not round-trip')
        self.assertNotIn('user_id', restored)
        self.assertNotIn('auth_result_hex', restored)

    def test_authorization_response_can_be_added_without_inventing_user_identity(self):
        state = make_state()
        self.store.save_pending(self.path, state)
        state['auth_result_hex'] = b'fake-authorization-response-for-testing'.hex()
        self.store.save_pending(self.path, state)
        restored = self.store.load_pending(self.path)
        self.assertTrue(restored == state, 'Pending authorization was not retained')
        self.assertNotIn('user_id', restored)

    def test_maximum_random_authorization_fits_ciphertext_limit_and_round_trips(self):
        state = make_state()
        state['auth_result_hex'] = secrets.token_hex(64 * 1024)
        self.store.save_pending(self.path, state)
        self.assertLessEqual(self.path.stat().st_size, 128 * 1024 + 49)
        self.assertTrue(self.store.load_pending(self.path) == state,
                        'Maximum authorization response did not round-trip')

    def test_same_windows_account_recovers_pending_in_another_process(self):
        state = make_state()
        state['auth_result_hex'] = b'fake-authorization-response'.hex()
        self.store.save_pending(self.path, state)
        expected = hashlib.sha256(json.dumps(
            state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        child = subprocess.run([
            sys.executable, '-B', '-c',
            'import hashlib,json,sys; from pathlib import Path; '
            'from tatocollector.pending_login import load_pending; '
            'state=load_pending(Path(sys.argv[1])); '
            'print(hashlib.sha256(json.dumps(state,sort_keys=True,'
            'separators=(",",":")).encode()).hexdigest())',
            str(self.path),
        ], cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), expected)

    def test_disk_and_output_do_not_expose_key_authorization_or_metadata(self):
        state = make_state()
        authorization = b'fake-authorization-sensitive-payload-for-test'
        state['auth_result_hex'] = authorization.hex()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.store.save_pending(self.path, state)
            self.store.load_pending(self.path)
        contents = self.path.read_bytes()
        for sensitive in (state['auth_key_hex'].encode(),
                          bytes.fromhex(state['auth_key_hex']),
                          state['auth_result_hex'].encode(), authorization,
                          state['host'].encode()):
            self.assertFalse(sensitive in contents,
                             'Pending file exposes plaintext material')
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')

    def test_invalid_state_is_rejected_before_encryption_and_does_not_overwrite(self):
        state = make_state()
        invalid = [([], 'not an object')]
        for field in state:
            candidate = state.copy()
            del candidate[field]
            invalid.append((candidate, 'missing ' + field))
        for field, values in (
            ('auth_key_hex', (None, '', 'a' * 510, 'a' * 514, 'g' * 512,
                              ' ' + 'a' * 511)),
            ('host', (None, '', ' ', 'host with space', 'host\x00name', 'h' * 254)),
            ('port', (True, 0, -1, 65536, '443')),
            ('dc_id', (False, 0, -1, '2')),
            ('time_offset', (True, '0', float('inf'), float('nan'))),
            ('auth_result_hex', (None, 123, 'a', 'gg', '00 ff',
                                 'a' * (64 * 1024 * 2 + 2))),
            ('user_id', (0, 123456)),
            ('unexpected', ('value',)),
        ):
            for index, value in enumerate(values):
                candidate = state.copy()
                candidate[field] = value
                invalid.append((candidate, f'invalid {field} {index}'))
        previous = b'previous pending file must remain unchanged'
        self.path.write_bytes(previous)
        with patch('tatocollector.pending_login._dpapi',
                   side_effect=AssertionError('Encryption reached before validation')):
            for candidate, label in invalid:
                with self.subTest(case=label):
                    with self.assertRaises(ValueError):
                        self.store.save_pending(self.path, candidate)
                    self.assertEqual(self.path.read_bytes(), previous)

    def test_corruption_truncation_and_appended_data_are_rejected(self):
        self.store.save_pending(self.path, make_state())
        original = self.path.read_bytes()
        flipped = bytearray(original)
        flipped[len(flipped) // 2] ^= 1
        for label, data in (
            ('changed ciphertext', bytes(flipped)),
            ('changed version', original[:12] + b'\x02' + original[13:]),
            ('truncated', original[:-1]),
            ('appended', original + b'extra'),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.store.load_pending(self.path)

    def test_plaintext_foreign_magic_and_framed_non_dpapi_data_are_rejected(self):
        plaintext = json.dumps(make_state()).encode()
        for label, contents in (
            ('empty', b''), ('plain JSON', plaintext),
            ('foreign session version', b'TATOSESSION\x00\x01' + b'x' * 300),
            ('framed plaintext', frame(plaintext)),
            ('framed compressed plaintext', frame(zlib.compress(plaintext))),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(contents)
                with self.assertRaises(ValueError):
                    self.store.load_pending(self.path)

    def test_decrypted_invalid_json_invalid_state_and_duplicate_keys_are_rejected(self):
        invalid = make_state()
        invalid['user_id'] = 1
        duplicate = (json.dumps(make_state())[:-1] + ',"port":80}').encode()
        bad_offset = make_state()
        bad_offset['time_offset'] = float('nan')
        for label, plaintext in (
            ('invalid JSON', b'not JSON'), ('not an object', b'[]'),
            ('unexpected verified identity', json.dumps(invalid).encode()),
            ('duplicate key', duplicate),
            ('NaN', json.dumps(bad_offset).encode()),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(self.protect_payload(plaintext))
                with self.assertRaises(ValueError):
                    self.store.load_pending(self.path)

    def test_decompression_is_bounded_and_requires_one_complete_stream(self):
        valid = zlib.compress(json.dumps(make_state()).encode())
        oversized_json = (b' ' * (256 * 1024)
                          + json.dumps(make_state()).encode())
        for label, compressed in (
            ('not zlib', b'not a compressed payload'),
            ('truncated', valid[:-1]), ('trailing data', valid + b'extra'),
            ('multiple streams', valid + valid),
            ('decompression bomb', zlib.compress(oversized_json)),
        ):
            with self.subTest(case=label):
                self.path.write_bytes(frame(self.dpapi(compressed)))
                with self.assertRaises(ValueError):
                    self.store.load_pending(self.path)

    def test_oversized_ciphertext_is_rejected_before_dpapi(self):
        previous = b'previous pending remains intact'
        self.path.write_bytes(previous)
        with patch('tatocollector.pending_login._dpapi',
                   return_value=b'x' * (128 * 1024 + 1)):
            with self.assertRaises(ValueError):
                self.store.save_pending(self.path, make_state())
        self.assertEqual(self.path.read_bytes(), previous)
        self.path.write_bytes(
            b'TATOPENDING\x00\x01' + struct.pack('<I', 128 * 1024 + 1)
            + b'\x00' * 32)
        with patch('tatocollector.pending_login._dpapi',
                   side_effect=AssertionError('Oversized ciphertext reached DPAPI')):
            with self.assertRaises(ValueError):
                self.store.load_pending(self.path)

    def test_failed_atomic_replace_keeps_previous_pending_and_cleans_temp_file(self):
        state = make_state()
        self.store.save_pending(self.path, state)
        previous = self.path.read_bytes()
        with patch('tatocollector.pending_login.os.replace',
                   side_effect=OSError('simulated replace failure')):
            with self.assertRaises(OSError):
                self.store.save_pending(self.path, make_state())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertTrue(self.store.load_pending(self.path) == state,
                        'Prior pending state can no longer be restored')
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_parent_directory_is_not_created_implicitly(self):
        absent_parent = self.path.parent / 'not-created'
        with self.assertRaises(FileNotFoundError):
            self.store.save_pending(absent_parent / 'pending.dpapi', make_state())
        self.assertFalse(absent_parent.exists())

    def test_non_windows_calls_fail_before_accessing_disk(self):
        with patch.object(sys, 'platform', 'linux'):
            for operation, args in (
                (self.store.save_pending, (self.path, make_state())),
                (self.store.load_pending, (self.path,)),
            ):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaisesRegex(RuntimeError, 'Windows'):
                        operation(*args)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
