"""Recovered Tatotalk email-auth wire subset, independently checked on two apps.

See docs/protocol.md for the recovered scope and limitations.
This is not a full message TL schema.
"""

import json
import struct


class AuthProtocolError(ValueError):
    """Protocol diagnostics limited to non-account structural metadata."""

    def __init__(self, reason_code, **metadata):
        super().__init__(reason_code)
        self.diagnostic = {'reason_code': reason_code}
        hex_fields = {'auth_constructor', 'authorization_flags', 'unknown_flags', 'user_constructor'}
        count_fields = {'decode_offset', 'response_bytes'}
        for key, value in metadata.items():
            if key not in hex_fields | count_fields or type(value) is not int:
                raise ValueError('unsafe protocol diagnostic field')
            self.diagnostic[key] = f'{value:08x}' if key in hex_fields else value


def tl_string(value: str) -> bytes:
    data = value.encode('utf-8')
    if len(data) > 65536:
        raise ValueError('auth string exceeds 64 KiB')
    prefix = bytes([len(data)]) if len(data) < 254 else b'\xfe' + len(data).to_bytes(3, 'little')
    return prefix + data + b'\0' * (-(len(prefix) + len(data)) % 4)


def encode_email_code_request(email, api_id, api_hash, *, captcha_token=None, captcha_answer=None):
    if not email or not api_hash or not 0 < api_id < 2**31:
        raise ValueError('email and application profile are required')
    if (captcha_token is None) != (captcha_answer is None):
        raise ValueError('captcha token and answer must be supplied together')
    flags = 8 | (2 if captcha_token is not None else 0)
    wire = struct.pack('<II', 0x20575BAD, flags) + tl_string(email)
    wire += struct.pack('<i', api_id) + tl_string(api_hash)
    if captcha_token is not None:
        wire += tl_string(captcha_token) + struct.pack('<I', 0x7D748D04)
        # Android's callback assigns raw answer text to DataJSON.data. Slider
        # answers are JSON objects; image-text answers must not gain JSON quotes.
        answer_data = captcha_answer if isinstance(captcha_answer, str) else json.dumps(captcha_answer, ensure_ascii=False, separators=(',', ':'))
        wire += tl_string(answer_data)
    return wire


def encode_email_signin(email, code_hash, code):
    if not all((email, code_hash, code)):
        raise ValueError('email, code hash and code are required')
    return struct.pack('<I', 0x405C0445) + b''.join(tl_string(v) for v in (email, code_hash, code))


def wrap_init(query: bytes, profile: dict) -> bytes:
    wire = struct.pack('<IIIi', 0xDA9B0D0D, profile['layer'], 0xC7481DA6, profile['api_id'])
    for value in ['PC', 'Windows', profile['app_version'], 'en', '', 'en']:
        wire += tl_string(value)
    return wire + query


class Reader:
    def __init__(self, data):
        if len(data) > 65536:
            raise ValueError('auth result exceeds bounded size')
        self.data = data
        self.offset = 0

    def read(self, count):
        end = self.offset + count
        if count < 0 or end > len(self.data):
            raise ValueError('truncated auth result')
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def uint(self):
        return struct.unpack('<I', self.read(4))[0]

    def integer(self):
        return struct.unpack('<i', self.read(4))[0]

    def string(self):
        size = self.read(1)[0]
        prefix_length = 1
        if size == 254:
            size = int.from_bytes(self.read(3), 'little')
            prefix_length = 4
        elif size == 255:
            raise ValueError('invalid TL string prefix')
        data = self.read(size)
        self.read(-(prefix_length + size) % 4)
        return data.decode('utf-8')


def parse_sent_code(data, *, allow_trailing=False):
    reader = Reader(data)
    constructor = reader.uint()
    if constructor == 0xB594A934:
        token, kind = reader.string(), reader.integer()
        if reader.uint() != 0x7D748D04:
            raise ValueError('captcha parameters must be boxed DataJSON')
        result = {'kind': 'captcha', 'token': token, 'type': kind,
                  'parameters': json.loads(reader.string())}
    elif constructor == 0x5E002502:
        flags = reader.uint()
        if flags & ~7:
            raise ValueError('unsupported auth.sentCode flags')
        code_type = reader.uint()
        delivery_types = {0x1B06F1DD: 'email', 0x3DBB5986: 'app', 0xC000BBA2: 'sms', 0x5353E5A7: 'call'}
        if code_type not in delivery_types:
            raise ValueError(f'unsupported sentCodeType constructor {code_type:08x}')
        length, code_hash = reader.integer(), reader.string()
        if not 1 <= length <= 32:
            raise ValueError('invalid email code length')
        if flags & 2:
            next_type = reader.uint()
            if next_type == 0x143DBBF1:
                reader.integer()
            elif next_type not in (0x72A3158C, 0x741CD3E3, 0x226CCEFB):
                raise ValueError('unsupported next CodeType')
        timeout = reader.integer() if flags & 4 else None
        result = {'kind': 'sent_code', 'code_hash': code_hash, 'length': length, 'timeout': timeout}
        if delivery_types[code_type] != 'email':
            result['delivery'] = delivery_types[code_type]
    else:
        raise ValueError(f'unsupported auth.SentCode constructor {constructor:08x}')
    if not allow_trailing and reader.offset != len(data):
        raise ValueError('trailing auth.SentCode data')
    return result


def parse_authorization(data):
    reader = Reader(data)
    context = {'response_bytes': len(data)}

    def fail(reason):
        raise AuthProtocolError(reason, decode_offset=reader.offset, **context)

    try:
        constructor = reader.uint()
        context['auth_constructor'] = constructor
        if constructor != 0xCD050916:
            fail('authorization_constructor_unsupported')
        flags = reader.uint()
        context['authorization_flags'] = flags
        # Mirror the complete official reader for this fixed constructor:
        # only bit0 consumes an additional field. Remaining flags (including
        # bits 24..26 observed live) do not change the following User prefix.
        # Do not invent an allowlist that rejects a valid authorization.
        if flags & 1:
            reader.integer()
        user_constructor = reader.uint()
        context['user_constructor'] = user_constructor
        if user_constructor not in (0x4E528244, 0xBCB626C1):
            fail('authorization_user_constructor_unsupported')
        reader.uint()  # User.flags
        reader.uint()  # User.flags2 (unconditional in both verified variants)
        user_id = reader.integer()
        if user_id <= 0:
            fail('authorization_user_id_invalid')
    except AuthProtocolError:
        raise
    except ValueError:
        fail('authorization_truncated')
    # Deliberately parse only the independently recovered identity prefix. Other
    # User fields are not represented as a complete schema or message support.
    return {'user_id': user_id, 'authorization_prefix_verified': True}


def parse_self_user(data):
    """Identity-only response to users.getUsers([inputUserSelf]); no User tail."""
    reader = Reader(data)
    context = {'response_bytes': len(data)}
    try:
        if reader.uint() != 0x1CB5C415 or reader.integer() != 1:
            raise AuthProtocolError('self_user_vector_invalid', **context)
        constructor = reader.uint()
        context['user_constructor'] = constructor
        if constructor not in (0x4E528244, 0xBCB626C1):
            raise AuthProtocolError('self_user_constructor_unsupported', **context)
        reader.uint()
        reader.uint()
        user_id = reader.integer()
        if user_id <= 0:
            raise AuthProtocolError('self_user_id_invalid', **context)
        return {'user_id': user_id, 'self_identity_verified': True}
    except AuthProtocolError:
        raise
    except ValueError:
        raise AuthProtocolError('self_user_truncated', decode_offset=reader.offset, **context) from None
