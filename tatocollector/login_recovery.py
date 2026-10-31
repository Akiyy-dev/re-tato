"""Durable checkpoints for this client's new transport; no existing app sessions."""

from pathlib import Path

from telethon.errors import (PhoneCodeEmptyError, PhoneCodeExpiredError,
                             PhoneCodeHashEmptyError, PhoneCodeInvalidError,
                             PhoneHashExpiredError)

from .authwire import encode_email_signin, parse_authorization
from .pending_login import load_pending, save_pending
from .session_store import save_session


def _definitive_code_refusal(exc):
    if isinstance(exc, (PhoneCodeEmptyError, PhoneCodeExpiredError, PhoneCodeHashEmptyError,
                        PhoneCodeInvalidError, PhoneHashExpiredError)):
        return True
    return getattr(exc, 'code', None) == 400 and getattr(exc, 'message', None) in {
        'EMAIL_CODE_INVALID', 'EMAIL_CODE_EXPIRED', 'EMAIL_CODE_EMPTY', 'EMAIL_CODE_HASH_EMPTY',
        'PHONE_CODE_INVALID', 'PHONE_CODE_EXPIRED', 'PHONE_CODE_EMPTY', 'PHONE_CODE_HASH_EMPTY',
    }


def _promote(state, session_path, pending_path, phase):
    if 'auth_result_hex' not in state:
        raise ValueError('pending authorization response is not available')
    phase('decode_authorization')
    authorization = parse_authorization(bytes.fromhex(state['auth_result_hex']))
    transport = {key: value for key, value in state.items() if key != 'auth_result_hex'}
    phase('save_session')
    save_session(Path(session_path), {**transport, 'user_id': authorization['user_id']})
    try:
        Path(pending_path).unlink(missing_ok=True)
    except OSError:
        # The verified, atomically written session is still valid if a leftover
        # diagnostic checkpoint cannot be removed.
        authorization['pending_cleanup_required'] = True
    return authorization


def promote_pending(pending_path, session_path, *, phase=lambda _: None):
    """Recover a saved response locally, without another OTP or network request."""
    return _promote(load_pending(Path(pending_path)), Path(session_path), Path(pending_path), phase)


async def submit_email_signin(connection, email, code_hash, code, session_path, pending_path,
                              *, phase=lambda _: None):
    session_path, pending_path = Path(session_path), Path(pending_path)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    state = connection.transport_state()
    phase('checkpoint_transport')
    # Persist this new key before the external operation: a timeout may happen
    # after the server has accepted login. Do not save the OTP/hash/request.
    save_pending(pending_path, state)
    phase('signin_rpc')
    try:
        data = await connection.call(encode_email_signin(email, code_hash, code))
    except Exception as exc:
        if _definitive_code_refusal(exc):
            # A definite credential rejection is different from an uncertain
            # timeout. Do not trap the next attempt in an unregistered-key loop.
            pending_path.unlink(missing_ok=True)
        raise
    state['auth_result_hex'] = data.hex()
    phase('checkpoint_authorization')
    save_pending(pending_path, state)
    return _promote(state, session_path, pending_path, phase)
