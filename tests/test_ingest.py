from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import importlib.util
import io
import json
import socket
import threading
import time
import unittest


TOKEN = 'tgfi_synthetic_local_test_only'


def make_event():
    return {'account_id': 123, 'chat_type': 'group', 'chat_id': -456,
            'message_id': 7, 'date': 0, 'text': '合成消息 🌏\nsecond line',
            'sender_name': '合成发送者', 'sender_id': 99,
            'conversation_name': '测试会话'}


def receipt(payload, duplicate=False):
    return {'ok': True, 'data': {
        'receipt_id': 42, 'source_id': 7,
        'message_id': payload['message_id'],
        'conversation_id': payload['conversation_id'],
        'status': 'queued', 'duplicate': duplicate,
    }, 'meta': {}}


@contextmanager
def local_server(plans):
    """Only loopback; retain synthetic requests without logging any body/header."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            payload = json.loads(raw.decode('utf-8'))
            self.server.requests.append({
                'path': self.path, 'headers': self.headers, 'raw': raw,
                'payload': payload,
            })
            plan = self.server.plans.pop(0) if self.server.plans else {}
            if plan.get('disconnect'):
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if plan.get('delay'):
                time.sleep(plan['delay'])
            status = plan.get('status', 202)
            body = plan.get('body', receipt(payload, duplicate=status == 200))
            if callable(body):
                body = body(payload)
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                for name, value in plan.get('headers', {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.requests = []
    server.plans = list(plans)
    server.url = f'http://127.0.0.1:{server.server_port}/api/ingest/v1/messages'
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01),
                              daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tatocollector.ingest'),
                             'The HTTP ingest adapter is not implemented')
        self.ingest = importlib.import_module('tatocollector.ingest')

    def config(self, server, **kwargs):
        return self.ingest.IngestConfig(server.url, TOKEN, **kwargs)

    def test_event_maps_to_only_frozen_text_contract_fields(self):
        event = make_event()
        payload = self.ingest.make_payload(event)
        self.assertEqual(payload, {
            'message_id': 'tato:123:7',
            'conversation_id': 'tato:123:group:-456',
            'conversation_name': '测试会话', 'sender': '合成发送者',
            'text': '合成消息 🌏\nsecond line',
            'timestamp': '1970-01-01T00:00:00+00:00',
        })
        event['sender_name'] = 'changed later'
        event['conversation_name'] = 'changed later'
        event['date'] = 86400
        self.assertEqual(payload['sender'], '合成发送者')
        self.assertEqual(payload['conversation_name'], '测试会话')
        self.assertEqual(payload['timestamp'], '1970-01-01T00:00:00+00:00')

    def test_sender_falls_back_to_numeric_identity_or_is_omitted(self):
        for name, identifier, expected in (
            (None, 99, 'user:99'), ('', 99, 'user:99'),
            ('  ', 99, 'user:99'), (None, -99, 'user:-99'),
            ('named channel', -99, 'named channel'), (None, None, None),
        ):
            with self.subTest(case=expected):
                event = make_event()
                event.update(sender_name=name, sender_id=identifier)
                event.pop('conversation_name')
                try:
                    payload = self.ingest.make_payload(event)
                except ValueError:
                    self.fail('Valid integer sender identities must not reject channel messages')
                self.assertEqual(payload.get('sender'), expected)
                self.assertNotIn('conversation_name', payload)

    def test_missing_nontext_and_blank_text_are_skipped(self):
        for text in (None, '', ' \r\n\t'):
            with self.subTest(case=repr(text)):
                self.assertIsNone(self.ingest.make_payload({'text': text}))
        self.assertIsNone(self.ingest.make_payload({}))

    def test_missing_identity_invalid_types_and_out_of_range_dates_are_errors(self):
        valid = make_event()
        invalid = []
        for field in ('account_id', 'chat_type', 'chat_id', 'message_id', 'date'):
            candidate = valid.copy()
            del candidate[field]
            invalid.append((field + ' missing', candidate))
        for field, value in (
            ('account_id', True), ('account_id', 0), ('message_id', -1),
            ('chat_id', False), ('chat_id', 0), ('chat_type', 'unknown'),
            ('date', 1.5), ('date', True), ('date', 10 ** 30),
            ('sender_id', '99'), ('sender_name', 123),
            ('conversation_name', False), ('text', 123),
        ):
            candidate = valid.copy()
            candidate[field] = value
            invalid.append((field + ' invalid', candidate))
        for label, event in invalid:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.ingest.make_payload(event)

    def test_oversized_fields_are_rejected_and_never_truncated(self):
        for field, value in (
            ('text', 'x' * 65537), ('sender_name', 's' * 257),
            ('conversation_name', 'c' * 257), ('account_id', 10 ** 128),
        ):
            with self.subTest(field=field):
                event = make_event()
                event[field] = value
                with self.assertRaises(ValueError):
                    self.ingest.make_payload(event)
        event = make_event()
        event['text'] = '界' * 65536
        self.assertEqual(len(self.ingest.make_payload(event)['text']), 65536)

    def test_config_validates_credentials_transport_timeout_and_hides_token(self):
        config = self.ingest.IngestConfig(
            'https://example.invalid:8080/api/ingest/v1/messages', TOKEN)
        self.assertFalse(TOKEN in repr(config), 'Token is exposed in config repr')
        for url, token, timeout in (
            ('http://example.invalid/api/ingest/v1/messages', TOKEN, 15),
            ('https://user:password@example.invalid/path', TOKEN, 15),
            ('https://example.invalid/path#fragment', TOKEN, 15),
            ('https://example.invalid/\npath', TOKEN, 15),
            ('https://example.invalid/path', 'tgf_wrong_kind', 15),
            ('https://example.invalid/path', 'admin-password', 15),
            ('https://example.invalid/path', 'tgfi_bad\r\nHeader: x', 15),
            ('https://example.invalid/path', 'tgfi_', 15),
            ('https://example.invalid/path', TOKEN, 0),
            ('https://example.invalid/path', TOKEN, float('inf')),
            ('https://example.invalid/path', TOKEN, True),
        ):
            with self.subTest(case='invalid configuration'):
                with self.assertRaises(ValueError):
                    self.ingest.IngestConfig(url, token, timeout)

    def test_202_posts_exact_utf8_payload_and_accepts_valid_receipt(self):
        payload = self.ingest.make_payload(make_event())
        with local_server([{}]) as server:
            result = self.ingest.deliver(payload, self.config(server))
            self.assertEqual(result.kind, 'accepted')
            self.assertEqual(result.receipt_id, 42)
            self.assertEqual(result.status_code, 202)
            self.assertEqual(result.reason, 'queued')
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]
            self.assertEqual(request['payload'], payload)
            self.assertEqual(request['path'], '/api/ingest/v1/messages')
            self.assertEqual(request['headers']['Authorization'], 'Bearer ' + TOKEN)
            self.assertTrue(request['headers']['Content-Type'].startswith('application/json'))
            self.assertIn('合成消息'.encode(), request['raw'])

    def test_200_duplicate_accepts_original_receipt(self):
        with local_server([{'status': 200}]) as server:
            result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                         self.config(server))
            self.assertEqual((result.kind, result.receipt_id, result.reason),
                             ('accepted', 42, 'duplicate'))

    def test_conflicts_and_validation_errors_are_permanent_rejections(self):
        for status, reason in ((409, 'conflict'), (422, 'validation_error'),
                               (400, 'client_error'), (404, 'client_error')):
            with self.subTest(status=status), local_server([{'status': status}]) as server:
                result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                             self.config(server))
                self.assertEqual((result.kind, result.status_code, result.reason),
                                 ('rejected', status, reason))
                self.assertIsNone(result.receipt_id)

    def test_401_and_403_stop_delivery_for_authentication_fix(self):
        for status in (401, 403):
            with self.subTest(status=status), local_server([{'status': status}]) as server:
                result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                             self.config(server))
                self.assertEqual((result.kind, result.reason),
                                 ('auth_error', 'unauthorized'))
                self.assertEqual(len(server.requests), 1)

    def test_503_other_5xx_and_408_are_retryable(self):
        for status in (503, 500, 502, 599, 408):
            with self.subTest(status=status), local_server([{'status': status}]) as server:
                result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                             self.config(server))
                self.assertEqual(result.kind, 'retry')
                self.assertEqual(result.status_code, status)

    def test_429_honors_numeric_and_http_date_retry_after(self):
        future = format_datetime(datetime.now(timezone.utc)
                                 + timedelta(seconds=120), usegmt=True)
        for header, expected in (('7', 7), ('1.5', 1.5), ('invalid', None),
                                 ('-3', None), (future, 'date')):
            with self.subTest(case=header), local_server([
                {'status': 429, 'headers': {'Retry-After': header}},
            ]) as server:
                result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                             self.config(server))
                self.assertEqual((result.kind, result.reason), ('retry', 'rate_limited'))
                if expected == 'date':
                    self.assertGreater(result.retry_after, 110)
                    self.assertLessEqual(result.retry_after, 120)
                else:
                    self.assertEqual(result.retry_after, expected)

    def test_fake_success_statuses_and_invalid_receipts_are_never_accepted(self):
        def alter(field, value):
            def response(payload):
                body = receipt(payload)
                body['data'][field] = value
                return body
            return response

        plans = [
            {'status': 201}, {'status': 204},
            {'status': 202, 'body': {'ok': False, 'error': {'message': 'echo text'}}},
            {'status': 202, 'body': {'ok': True}},
            {'status': 202, 'body': b'<html>not JSON</html>'},
            {'status': 202, 'body': alter('receipt_id', True)},
            {'status': 202, 'body': alter('receipt_id', 0)},
            {'status': 202, 'body': alter('source_id', '7')},
            {'status': 202, 'body': alter('message_id', 'another-message')},
            {'status': 202, 'body': alter('conversation_id', 'another-chat')},
            {'status': 202, 'body': alter('status', 'published')},
            {'status': 202, 'body': alter('duplicate', True)},
            {'status': 200, 'body': lambda payload: receipt(payload, False)},
        ]
        with local_server(plans) as server:
            for index in range(len(plans)):
                with self.subTest(case=index):
                    result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                                 self.config(server))
                    self.assertEqual(result.kind, 'retry')
                    self.assertIsNone(result.receipt_id)

    def test_redirects_are_not_followed_and_do_not_leak_authorization(self):
        with local_server([]) as destination:
            plans = [{'status': status, 'headers': {'Location': destination.url}}
                     for status in (301, 302, 303, 307, 308)]
            with local_server(plans) as source:
                for status in (301, 302, 303, 307, 308):
                    with self.subTest(status=status):
                        result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                                     self.config(source))
                        self.assertEqual((result.kind, result.status_code, result.reason),
                                         ('rejected', status, 'redirect_blocked'))
            self.assertEqual(len(destination.requests), 0)

    def test_timeouts_and_lost_responses_retry_without_exposing_exception_text(self):
        for plan, expected in (({'delay': .15}, 'timeout'),
                               ({'disconnect': True}, 'network_error')):
            with self.subTest(case=expected), local_server([plan]) as server:
                result = self.ingest.deliver(self.ingest.make_payload(make_event()),
                                             self.config(server, timeout=.03))
                self.assertEqual((result.kind, result.reason), ('retry', expected))
                self.assertIsNone(result.status_code)

    def test_retry_uses_original_payload_byte_for_byte_and_does_not_mutate_it(self):
        event = make_event()
        payload = self.ingest.make_payload(event)
        original = payload.copy()
        with local_server([{'status': 503}, {'status': 200}]) as server:
            first = self.ingest.deliver(payload, self.config(server))
            event.update(date=86400, sender_name='changed', conversation_name='changed')
            second = self.ingest.deliver(payload, self.config(server))
            self.assertEqual((first.kind, second.kind), ('retry', 'accepted'))
            self.assertEqual(payload, original)
            self.assertEqual(server.requests[0]['raw'], server.requests[1]['raw'])

    def test_invalid_payload_is_rejected_locally_before_any_request(self):
        valid = self.ingest.make_payload(make_event())
        invalid = []
        for field in ('message_id', 'conversation_id', 'text'):
            payload = valid.copy()
            del payload[field]
            invalid.append(payload)
        for field, value in (
            ('text', ' '), ('text', 'x' * 65537), ('sender', 's' * 257),
            ('conversation_name', 123), ('message_id', ''),
            ('conversation_id', 'c' * 129), ('timestamp', '2026-01-01T00:00:00'),
            ('media', 'not allowed'), ('source_id', 1),
        ):
            payload = valid.copy()
            payload[field] = value
            invalid.append(payload)
        with local_server([]) as server:
            for index, payload in enumerate(invalid):
                with self.subTest(case=index):
                    with self.assertRaises(ValueError):
                        self.ingest.deliver(payload, self.config(server))
            self.assertEqual(len(server.requests), 0)

    def test_response_body_is_bounded_and_never_logged(self):
        payload = self.ingest.make_payload(make_event())
        stdout, stderr = io.StringIO(), io.StringIO()
        with local_server([{'status': 202, 'body': b'x' * (64 * 1024 + 1)}]) as server:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = self.ingest.deliver(payload, self.config(server))
        self.assertEqual((result.kind, result.reason), ('retry', 'response_too_large'))
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
