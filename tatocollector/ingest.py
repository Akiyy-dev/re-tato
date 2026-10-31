"""Text-only adapter for TG-forwarder /api/ingest/v1/messages.

Payload creation freezes source identity, time and optional display metadata.
Delivery performs one request and returns a safe classification; the caller owns
the durable queue, backoff, authentication pause and retry of the same payload.
An accepted receipt means queued at the receiver, not published downstream.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import ipaddress
import json
import math
import re
import ssl
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (HTTPSHandler, HTTPRedirectHandler, ProxyHandler,
                            Request, build_opener)


_FIELDS = frozenset(('message_id', 'conversation_id', 'conversation_name',
                     'sender', 'text', 'timestamp'))
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class IngestConfig:
    url: str
    token: str = field(repr=False)
    timeout: float = 15

    def __post_init__(self):
        if (type(self.url) is not str or not self.url
                or any(character.isspace() or ord(character) < 33
                       or ord(character) == 127 for character in self.url)):
            raise ValueError('Ingest URL must be an absolute HTTP(S) URL')
        try:
            parsed = urlsplit(self.url)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            raise ValueError('Ingest URL is invalid') from None
        if (parsed.scheme not in ('http', 'https') or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.query
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError('Ingest URL must not contain credentials, query or fragment')
        if parsed.scheme == 'http':
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host.lower() == 'localhost'
            if not local:
                raise ValueError('Ingest requires HTTPS except for loopback tests')
        if (type(self.token) is not str
                or re.fullmatch(r'tgfi_[A-Za-z0-9_-]+', self.token) is None):
            raise ValueError('A valid tgfi_ ingest token is required')
        try:
            valid_timeout = (type(self.timeout) in (int, float)
                             and math.isfinite(self.timeout) and self.timeout > 0)
        except OverflowError:
            valid_timeout = False
        if not valid_timeout:
            raise ValueError('Ingest timeout must be a positive finite number')


@dataclass(frozen=True)
class DeliveryResult:
    kind: Literal['accepted', 'retry', 'auth_error', 'rejected']
    receipt_id: int | None = None
    status_code: int | None = None
    retry_after: float | None = None
    reason: str = ''


def _string(value, field_name, limit, *, nonblank=False):
    if (type(value) is not str or len(value) > limit
            or (nonblank and not value.strip())):
        raise ValueError(field_name + ' has an invalid type, length or empty value')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError(field_name + ' must be valid UTF-8 text') from None


def _validate_payload(payload):
    if type(payload) is not dict or payload.keys() - _FIELDS:
        raise ValueError('Ingest payload must contain only supported text fields')
    for name, limit in (('message_id', 128), ('conversation_id', 128), ('text', 65536)):
        if name not in payload:
            raise ValueError('Ingest payload is missing ' + name)
        _string(payload[name], name, limit, nonblank=True)
    for name in ('conversation_name', 'sender'):
        if name in payload:
            _string(payload[name], name, 256)
    if 'timestamp' in payload:
        timestamp = payload['timestamp']
        _string(timestamp, 'timestamp', 128, nonblank=True)
        try:
            parsed = datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError('timestamp must be ISO 8601 with a timezone') from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timestamp must include a timezone')


def make_payload(event: dict) -> dict | None:
    """Freeze one normalized Tatotalk text event; skip absent/blank text.

    Malformed identities, timestamps or overlong fields raise ValueError. No
    field is truncated and no binary/media field is copied into the payload.
    """
    if type(event) is not dict:
        raise ValueError('Message event must be an object')
    text = event.get('text')
    if text is None:
        return None
    _string(text, 'text', 65536)
    if not text.strip():
        return None
    for name in ('account_id', 'chat_id', 'message_id', 'date'):
        if name not in event or type(event[name]) is not int:
            raise ValueError('Message event requires integer ' + name)
    if event['account_id'] <= 0 or event['message_id'] <= 0 or event['chat_id'] == 0:
        raise ValueError('Message event contains an invalid identity')
    if event.get('chat_type') not in ('private', 'group', 'channel'):
        raise ValueError('Message event has an unsupported chat_type')
    try:
        timestamp = datetime.fromtimestamp(event['date'], timezone.utc).isoformat(
            timespec='seconds')
        payload = {
            'message_id': f"tato:{event['account_id']}:{event['message_id']}",
            'conversation_id': (f"tato:{event['account_id']}:{event['chat_type']}:"
                                f"{event['chat_id']}"),
            'text': text,
            'timestamp': timestamp,
        }
    except (ValueError, OverflowError, OSError):
        raise ValueError('Message identity or timestamp is out of range') from None
    conversation_name = event.get('conversation_name')
    if conversation_name is not None:
        _string(conversation_name, 'conversation_name', 256)
        payload['conversation_name'] = conversation_name
    sender_name = event.get('sender_name')
    if sender_name is not None:
        _string(sender_name, 'sender_name', 256)
    sender_id = event.get('sender_id')
    if sender_id is not None and type(sender_id) is not int:
        raise ValueError('sender_id must be an integer when present')
    if sender_name is not None and sender_name.strip():
        payload['sender'] = sender_name
    elif sender_id is not None:
        try:
            payload['sender'] = f'user:{sender_id}'
        except ValueError:
            raise ValueError('sender_id is out of range') from None
    _validate_payload(payload)
    return payload


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                return None
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except (ValueError, TypeError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate JSON field')
        result[name] = value
    return result


def _reject_constant(_value):
    raise ValueError('Non-finite JSON number')


def _response_result(response, payload):
    status = response.getcode()
    if status in (401, 403):
        return DeliveryResult('auth_error', status_code=status, reason='unauthorized')
    if status == 429:
        return DeliveryResult('retry', status_code=status,
                              retry_after=_retry_after(response.headers.get('Retry-After')),
                              reason='rate_limited')
    if 500 <= status <= 599 or status == 408:
        return DeliveryResult('retry', status_code=status,
                              retry_after=_retry_after(response.headers.get('Retry-After')),
                              reason='server_error' if status >= 500 else 'request_timeout')
    if 300 <= status <= 399:
        return DeliveryResult('rejected', status_code=status, reason='redirect_blocked')
    if 400 <= status <= 499:
        reason = {409: 'conflict', 422: 'validation_error'}.get(status, 'client_error')
        return DeliveryResult('rejected', status_code=status, reason=reason)
    if status not in (200, 202):
        return DeliveryResult('retry', status_code=status, reason='unexpected_status')
    if response.headers.get_content_type() != 'application/json':
        return DeliveryResult('retry', status_code=status, reason='invalid_response')
    body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        return DeliveryResult('retry', status_code=status, reason='response_too_large')
    try:
        document = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        return DeliveryResult('retry', status_code=status, reason='invalid_response')
    data = document.get('data') if type(document) is dict else None
    if (type(document) is not dict or document.get('ok') is not True
            or type(data) is not dict
            or type(data.get('receipt_id')) is not int or data['receipt_id'] <= 0
            or type(data.get('source_id')) is not int or data['source_id'] <= 0
            or data.get('message_id') != payload['message_id']
            or data.get('conversation_id') != payload['conversation_id']
            or data.get('status') != 'queued'
            or data.get('duplicate') is not (status == 200)):
        return DeliveryResult('retry', status_code=status, reason='invalid_receipt')
    return DeliveryResult('accepted', receipt_id=data['receipt_id'], status_code=status,
                          reason='duplicate' if status == 200 else 'queued')


def deliver(payload: dict, config: IngestConfig) -> DeliveryResult:
    """Attempt exactly one delivery without changing payload or following redirects.

    TLS uses the normal trusted-certificate/hostname checks. No environment proxy
    is inherited and no cookies or credentials are read. Errors expose only fixed
    reason codes, never server error bodies, request text or the ingest token.
    """
    _validate_payload(payload)
    if not isinstance(config, IngestConfig):
        raise ValueError('Ingest configuration is required')
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = Request(config.url, data=body, method='POST', headers={
        'Authorization': 'Bearer ' + config.token,
        'Content-Type': 'application/json; charset=utf-8',
        'Accept': 'application/json',
    })
    opener = build_opener(ProxyHandler({}), _NoRedirect(),
                          HTTPSHandler(context=ssl.create_default_context()))
    try:
        try:
            response = opener.open(request, timeout=config.timeout)
        except HTTPError as error:
            response = error
        with response:
            return _response_result(response, payload)
    except TimeoutError:
        return DeliveryResult('retry', reason='timeout')
    except URLError as error:
        return DeliveryResult('retry', reason=('timeout' if isinstance(error.reason, TimeoutError)
                                              else 'network_error'))
    except (OSError, HTTPException):
        return DeliveryResult('retry', reason='network_error')
