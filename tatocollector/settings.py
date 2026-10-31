"""Local ingest settings. Tokens are DPAPI protected and never written to JSON."""

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile

from .ingest import IngestConfig
from .session_store import _dpapi

TOKEN_MAGIC = b'TATOINGEST\x00\x01'


def configure_ingest(url, token, path):
    config = IngestConfig(url, token)
    protected_token = TOKEN_MAGIC + _dpapi(token.encode('utf-8'))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, token_name = tempfile.mkstemp(
        prefix='ingest-token-', suffix='.dpapi', dir=path.parent)
    token_path = Path(token_name)
    temporary_config = None
    published = False
    try:
        # The document does not reference this unique immutable file until it is
        # fully written. Keep previously published files for in-flight readers.
        with os.fdopen(descriptor, 'wb') as output:
            output.write(protected_token)
            output.flush()
            os.fsync(output.fileno())
        document = {'ingest_url': config.url, 'token_file': token_path.name,
                    'timeout': config.timeout, 'enabled': True}
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        temporary_config = Path(temporary_name)
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        temporary_config.replace(path)
        published = True
    finally:
        if temporary_config is not None:
            temporary_config.unlink(missing_ok=True)
        if not published:
            token_path.unlink(missing_ok=True)


def load_ingest_config(path):
    path = Path(path)
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('enabled') is not True:
        return None
    name = document['token_file']
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError('token_file must be a local filename')
    with (path.parent / name).open('rb') as source:
        encrypted = source.read(65537)
    if len(encrypted) > 65536 or not encrypted.startswith(TOKEN_MAGIC):
        raise ValueError('invalid local token file')
    token = _dpapi(encrypted[len(TOKEN_MAGIC):], decrypt=True).decode('utf-8')
    return IngestConfig(document['ingest_url'], token, document.get('timeout', 15))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--config', type=Path, default=Path('config/collector.local.json'))
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print('An interactive terminal is required to configure the ingest token.', file=sys.stderr)
        return 2
    print('TG-forwarder 接入地址:', args.url)
    print('请使用消息接入来源的 tgfi_ Token；输入不显示，保存时使用 Windows DPAPI 加密。')
    try:
        token = getpass.getpass('Ingest Token: ').strip()
        configure_ingest(args.url, token, args.config)
        print('接入配置已加密保存。尚未在此步骤发送任何消息。')
        return 0
    except (ValueError, OSError):
        print('配置无效或无法保存，请检查 URL、tgfi_ Token 和本机文件权限。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
