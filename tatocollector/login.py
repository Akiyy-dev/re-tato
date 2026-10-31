"""Interactive local email login verification. No password/code is written to logs."""

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import json
import logging
from pathlib import Path
import struct
import sys
import time

from .authwire import encode_email_code_request, parse_sent_code as parse_sent_code_wire, parse_self_user
from .client import TatoConnection, safe_error
from .login_recovery import promote_pending, submit_email_signin
from .pending_login import load_pending
from .session_store import save_session


def parse_sent_code(data):
    # The pinned Telethon version's RpcResult.body may include MTProto padding
    # or container siblings. Consume the recovered self-delimiting TL result;
    # preserve strict full-buffer validation for standalone wire fixtures.
    return parse_sent_code_wire(data, allow_trailing=True)


async def solve_captcha(challenge):
    # Tk runs in a dedicated main-thread process rather than an asyncio worker.
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'tatocollector.captcha', stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(challenge).encode('utf-8')), 300)
    except BaseException:
        if process.returncode is None:
            process.terminate()
        await process.wait()
        raise
    if process.returncode == 4:
        raise NotImplementedError('official web captcha bridge is not implemented')
    if process.returncode:
        raise ValueError('captcha window could not complete')
    return json.loads(stdout)


def write_status(path, **status):
    path.parent.mkdir(parents=True, exist_ok=True)
    status['updated_at'] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def set_phase(args, phase, **metadata):
    args.last_phase = phase
    write_status(args.status, authenticated=bool(getattr(args, 'identity_verified', False)), stage=phase, **metadata)


def archive_pending(path):
    """Explicit user restart preserves the old encrypted checkpoint as a backup."""
    path = Path(path)
    load_pending(path)  # Refuse to move an unrelated or corrupt arbitrary file.
    backup = path.with_name(path.name + f'.restarted-{time.time_ns()}.bak')
    path.rename(backup)
    return backup


async def recover_login(args):
    """Recover only this program's encrypted checkpoint, never request an OTP."""
    set_phase(args, 'load_pending')
    checkpoint = load_pending(args.pending)
    phase = lambda stage: set_phase(args, stage)
    args.session.parent.mkdir(parents=True, exist_ok=True)
    if 'auth_result_hex' in checkpoint:
        authorization = promote_pending(args.pending, args.session, phase=phase)
    else:
        profile = json.loads(args.profile.read_text(encoding='utf-8'))
        public_key = args.public_key.read_bytes()
        set_phase(args, 'reconnect_pending')
        async with TatoConnection(profile, public_key, host=checkpoint['host'], port=checkpoint['port'],
                                  dc_id=checkpoint['dc_id'], auth_key=bytes.fromhex(checkpoint['auth_key_hex']),
                                  time_offset=checkpoint['time_offset']) as connection:
            set_phase(args, 'verify_pending_identity')
            # Read-only self lookup, with no new code request or sendMessage.
            self_request = bytes.fromhex('48a5910d 15c4b51c 01000000 3fb1c1f7')
            authorization = await connection.call(self_request, parse_self_user)
            set_phase(args, 'save_session')
            save_session(args.session, connection.session_state(authorization['user_id']))
            args.identity_verified = True
            set_phase(args, 'login_verified', user_id=authorization['user_id'], session_saved=True,
                      session_encrypted=True, recovered_pending=True, message_capture_verified=False)
        try:
            args.pending.unlink(missing_ok=True)
        except OSError:
            authorization['pending_cleanup_required'] = True
    args.identity_verified = True
    set_phase(args, 'login_verified', user_id=authorization['user_id'], session_encrypted=True,
              session_saved=True, recovered_pending=True, message_capture_verified=False)
    print('已恢复本程序的新登录会话，无需重新请求验证码。')
    return 0


async def run_login(args):
    if args.pending.exists():
        return await recover_login(args)
    args.last_phase = 'load_profile'
    profile = json.loads(args.profile.read_text(encoding='utf-8'))
    public_key = args.public_key.read_bytes()
    email = (await asyncio.to_thread(input, 'Tatotalk email / 邮箱: ')).strip()
    if not email or '@' not in email:
        print('邮箱格式无效，未发送请求。')
        return 2
    set_phase(args, 'connecting')
    async with TatoConnection(profile, public_key) as connection:
        print('独立加密连接已建立。正在请求登录验证码……')
        set_phase(args, 'request_email_code')
        response = await connection.call(encode_email_code_request(email, profile['api_id'], profile['api_hash']), parse_sent_code)
        if response['kind'] == 'captcha':
            set_phase(args, 'captcha_required', captcha_type=response['type'])
            print(f"服务端要求人工图形校验，类型 {response['type']}。尚未发送邮箱验证码。")
            try:
                answer = await solve_captcha(response)
            except NotImplementedError:
                print('该账号触发网页验证码；此验证入口尚未支持官方网页桥，不能继续登录。')
                return 3
            if answer is None:
                print('人工校验已取消，未尝试跳过。')
                return 3
            set_phase(args, 'submit_captcha')
            response = await connection.call(encode_email_code_request(
                email, profile['api_id'], profile['api_hash'], captcha_token=response['token'],
                captcha_answer=answer), parse_sent_code)
            if response['kind'] != 'sent_code':
                write_status(args.status, authenticated=False, stage='captcha_repeated', captcha_type=response['type'])
                print('服务端仍要求校验，本次停止；没有重复自动提交。')
                return 3
        delivery = {'email': '邮箱', 'app': '已登录的 Tatotalk 应用', 'sms': '短信', 'call': '电话'}.get(response.get('delivery', 'email'), '验证渠道')
        set_phase(args, 'waiting_for_code', delivery=response.get('delivery', 'email'))
        print(f"验证码已请求，请检查{delivery}。验证码仅在本机输入，不会显示或保存。")
        code = (await asyncio.to_thread(getpass.getpass, 'Verification code / 验证码: ')).strip()
        if not code:
            return 3
        authorization = await submit_email_signin(connection, email, response['code_hash'], code, args.session, args.pending,
                                                 phase=lambda stage: set_phase(args, stage))
        code = None
        args.identity_verified = True
        set_phase(args, 'login_verified', user_id=authorization['user_id'], session_encrypted=True,
                  session_saved=True, message_capture_verified=False)
        # A successful authorization response proves login. A state query is a
        # separate diagnostic; unsupported sync schema must not erase that fact.
        sync_probe = {}
        try:
            state = await connection.call(struct.pack('<I', 0xEDD4882A))
            if len(state) >= 4:
                sync_probe = {'sync_state_constructor': f'{struct.unpack_from("<I", state)[0]:08x}',
                              'sync_state_bytes': len(state)}
        except Exception as exc:
            sync_probe = {'sync_probe_error': type(exc).__name__}
        set_phase(args, 'login_verified', user_id=authorization['user_id'],
                  session_encrypted=True, session_saved=True, message_capture_verified=False, **sync_probe)
        print('独立账号登录成功，新会话已由 Windows DPAPI 加密保存。')
        print('此入口只验证登录；尚未开始采集消息或 HTTP 推送。')
        print(f'状态文件: {args.status.resolve()}')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('config/client-profile.local.json'))
    parser.add_argument('--public-key', type=Path, default=Path('research/android-static/embedded-public-0.pem'))
    parser.add_argument('--session', type=Path, default=Path('sessions/default.dpapi'))
    parser.add_argument('--status', type=Path, default=Path('sessions/login-status.local.json'))
    parser.add_argument('--pending', type=Path, default=Path('sessions/pending-login.dpapi'))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--recover-only', action='store_true', help='recover this client checkpoint without asking for OTP')
    mode.add_argument('--restart-login', action='store_true', help='archive a pending checkpoint and explicitly start a fresh login')
    args = parser.parse_args(argv)
    if not args.recover_only and not sys.stdin.isatty():
        print('An interactive terminal is required; no login code has been requested.', file=sys.stderr)
        return 2
    if args.session.exists():
        print('指定的新客户端会话文件已存在。为避免覆盖，未发出登录请求。')
        return 2
    if args.recover_only and not args.pending.exists():
        print('No encrypted pending login is available; no login request was sent.', file=sys.stderr)
        return 2
    logging.disable(logging.CRITICAL)  # Dependency diagnostics may contain raw RPC payloads.
    print('Tatotalk 独立登录验证 / 本机交互')
    print('仅在此窗口输入你自己的邮箱和验证码。输入 Ctrl+C 可取消。')
    try:
        if args.restart_login and args.pending.exists():
            backup = archive_pending(args.pending)
            print(f'旧临时检查点已保留为 {backup.name}，将开始新的登录。')
        return asyncio.run(recover_login(args) if args.recover_only else run_login(args))
    except KeyboardInterrupt:
        write_status(args.status, authenticated=bool(getattr(args, 'identity_verified', False)), stage='cancelled',
                     phase=getattr(args, 'last_phase', 'startup'), session_saved=args.session.exists())
        print('\n已取消。')
        return 3
    except Exception as exc:
        details = safe_error(exc)
        write_status(args.status, authenticated=bool(getattr(args, 'identity_verified', False)), stage='failed',
                     phase=getattr(args, 'last_phase', 'startup'), session_saved=args.session.exists(),
                     pending_saved=args.pending.exists(), **details)
        print('登录验证未完成:', json.dumps(details, ensure_ascii=False))
        print(f'诊断状态保存在 {args.status.resolve()}；不包含验证码或会话密钥。')
        if args.pending.exists():
            print('加密临时会话已保留；默认重开会先恢复。若确认需要重新登录，可运行 run-login.cmd --restart-login。')
        else:
            print('可以关闭此窗口后重新运行登录入口。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
