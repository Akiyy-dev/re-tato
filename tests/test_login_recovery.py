import asyncio
import importlib.util
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == 'win32' and importlib.util.find_spec('telethon'), 'Windows and login dependencies required')
class LoginRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pending = Path(self.temp.name) / 'pending.dpapi'
        self.session = Path(self.temp.name) / 'session.dpapi'
        self.state = {'auth_key_hex': bytes(range(256)).hex(), 'host': '127.0.0.1',
                      'port': 20334, 'dc_id': 1, 'time_offset': 0}

    def test_success_promotes_real_identity_and_removes_pending(self):
        from tatocollector.login_recovery import submit_email_signin
        from tatocollector.pending_login import load_pending
        from tatocollector.session_store import load_session
        state, pending = self.state, self.pending
        wire = bytes.fromhex('160905cd 00004070 4482524e 00000000 00000000 7b000000')

        class Connection:
            def transport_state(self):
                return state.copy()

            async def call(self, request):
                # The actual encrypted checkpoint must precede the external call.
                if load_pending(pending) != state:
                    raise AssertionError('transport was not durably checkpointed before signin')
                return wire

        result = asyncio.run(submit_email_signin(Connection(), 'test@example.invalid', 'hash', '123456', self.session, self.pending))
        self.assertEqual(result['user_id'], 123)
        self.assertEqual(load_session(self.session)['user_id'], 123)
        self.assertFalse(self.pending.exists())

    def test_unsupported_result_remains_encrypted_and_can_be_promoted_after_decoder_fix(self):
        from tatocollector.authwire import AuthProtocolError
        from tatocollector.login_recovery import submit_email_signin, promote_pending
        from tatocollector.pending_login import load_pending, save_pending
        from tatocollector.session_store import load_session
        state = self.state
        bad_wire = bytes.fromhex('160905cd 02000000') + b'not-printed-user-data'

        class Connection:
            def transport_state(self):
                return state.copy()

            async def call(self, request):
                return bad_wire

        with self.assertRaises(AuthProtocolError):
            asyncio.run(submit_email_signin(Connection(), 'test@example.invalid', 'secret-hash', 'secret-code', self.session, self.pending))
        checkpoint = load_pending(self.pending)
        self.assertEqual(checkpoint['auth_result_hex'], bad_wire.hex())
        self.assertNotIn('user_id', checkpoint)
        self.assertNotIn('secret-code', str(checkpoint))
        self.assertNotIn('secret-hash', str(checkpoint))
        self.assertFalse(self.session.exists())
        self.assertNotIn(b'not-printed-user-data', self.pending.read_bytes())
        # Simulate a later verified decoder-compatible response, without another RPC.
        checkpoint['auth_result_hex'] = bytes.fromhex('160905cd 00004070 c126b6bc 00000000 00000000 7b000000').hex()
        save_pending(self.pending, checkpoint)
        self.assertEqual(promote_pending(self.pending, self.session)['user_id'], 123)
        self.assertEqual(load_session(self.session)['user_id'], 123)

    def test_timeout_preserves_transport_without_claiming_identity(self):
        from tatocollector.login_recovery import submit_email_signin
        from tatocollector.pending_login import load_pending
        state = self.state

        class Connection:
            def transport_state(self):
                return state.copy()

            async def call(self, request):
                raise TimeoutError()

        with self.assertRaises(TimeoutError):
            asyncio.run(submit_email_signin(Connection(), 'test@example.invalid', 'h', '123456', self.session, self.pending))
        self.assertEqual(load_pending(self.pending), state)
        self.assertFalse(self.session.exists())

    def test_definitively_invalid_otp_does_not_leave_an_unrecoverable_pending_loop(self):
        from telethon.errors import PhoneCodeInvalidError
        from tatocollector.login_recovery import submit_email_signin
        state = self.state

        class Connection:
            def transport_state(self):
                return state.copy()

            async def call(self, request):
                raise PhoneCodeInvalidError(request=None)

        with self.assertRaises(PhoneCodeInvalidError):
            asyncio.run(submit_email_signin(Connection(), 'test@example.invalid', 'h', 'wrong', self.session, self.pending))
        self.assertFalse(self.pending.exists())
        self.assertFalse(self.session.exists())

    def test_login_entry_restores_saved_result_without_prompt_or_network(self):
        import json
        from tatocollector.login import recover_login
        from tatocollector.pending_login import save_pending
        from tatocollector.session_store import load_session
        checkpoint = {**self.state, 'auth_result_hex': bytes.fromhex(
            '160905cd 00004070 4482524e 00000000 00000000 7b000000').hex()}
        save_pending(self.pending, checkpoint)
        args = SimpleNamespace(pending=self.pending, session=self.session,
                               status=Path(self.temp.name) / 'status.json')
        with (patch('builtins.input', side_effect=AssertionError('recovery must not ask for OTP')),
              patch('tatocollector.login.TatoConnection', side_effect=AssertionError('saved result must not need network'))):
            self.assertEqual(asyncio.run(recover_login(args)), 0)
        self.assertEqual(load_session(self.session)['user_id'], 123)
        status = json.loads(args.status.read_text(encoding='utf-8'))
        self.assertTrue(status['authenticated'])
        self.assertTrue(status['recovered_pending'])

    def test_explicit_restart_archives_encrypted_checkpoint_instead_of_deleting_it(self):
        from tatocollector.login import archive_pending
        from tatocollector.pending_login import load_pending, save_pending
        save_pending(self.pending, self.state)
        archived = archive_pending(self.pending)
        self.assertFalse(self.pending.exists())
        self.assertEqual(load_pending(archived), self.state)

    def test_unknown_rpc_refusal_keeps_uncertain_transport_for_recovery(self):
        from telethon.errors import BadRequestError
        from tatocollector.login_recovery import submit_email_signin
        from tatocollector.pending_login import load_pending
        state = self.state

        class Connection:
            def transport_state(self):
                return state.copy()

            async def call(self, request):
                raise BadRequestError(request=None, message='NEW_UNKNOWN_AUTH_ERROR', code=400)

        with self.assertRaises(BadRequestError):
            asyncio.run(submit_email_signin(Connection(), 'test@example.invalid', 'h', 'x', self.session, self.pending))
        self.assertEqual(load_pending(self.pending), state)


if __name__ == '__main__':
    unittest.main()
