"""Synthetic Win32 snapshots and TCP tables; never starts or contacts a client."""

import ctypes
import socket
import struct
import unittest
from unittest.mock import patch

from tatocollector.discovery import Endpoint
from tatocollector import native_endpoints as native


def tcp_row(host='8.8.8.8', port=20334, *, pid=42, state=5):
    address = struct.unpack('<I', socket.inet_aton(host))[0]
    return struct.pack('<6I', state, 0, 0, address, socket.htons(port), pid)


def tcp_table(*rows):
    return struct.pack('<I', len(rows)) + b''.join(rows)


class Kernel32Fixture:
    """Only implements the read-only snapshot API used by the production code."""

    def __init__(self, entries=(), *, handle=123, final_error=18, fail_first=False):
        self.entries = iter(entries)
        self.handle = handle
        self.final_error = final_error
        self.fail_first = fail_first
        self.error = 0
        self.closed_handles = []

    def CreateToolhelp32Snapshot(self, flags, pid):
        if (flags, pid) != (2, 0):
            raise AssertionError('Only a process snapshot is permitted')
        return self.handle

    def Process32FirstW(self, handle, entry):
        if self.fail_first:
            raise OSError('synthetic inaccessible process snapshot')
        return self.Process32NextW(handle, entry)

    def Process32NextW(self, handle, entry):
        if handle != self.handle:
            raise AssertionError('Unknown snapshot handle')
        if entry._obj.dwSize != ctypes.sizeof(native._PROCESSENTRY32W):
            raise AssertionError('Invalid PROCESSENTRY32W size')
        try:
            pid, name = next(self.entries)
        except StopIteration:
            self.error = self.final_error
            return False
        entry._obj.th32ProcessID = pid
        entry._obj.szExeFile = name
        return True

    def CloseHandle(self, handle):
        self.closed_handles.append(handle)
        return True


class IpHelperFixture:
    def __init__(self, payload, *, error=0, initial_size=None, grow_forever=False):
        self.payload = payload
        self.error = error
        self.initial_size = initial_size
        self.grow_forever = grow_forever

    def GetExtendedTcpTable(self, buffer, size, ordered, family, table_class, reserved):
        if (bool(ordered), family, table_class, reserved) != (False, 2, 5, 0):
            raise AssertionError('Only the IPv4 owner-PID TCP table is permitted')
        if self.error:
            return self.error
        if buffer is None:
            size._obj.value = (len(self.payload) if self.initial_size is None
                               else self.initial_size)
            return 122
        capacity = size._obj.value
        if self.grow_forever:
            size._obj.value = capacity + 24
            return 122
        size._obj.value = len(self.payload)
        if capacity < len(self.payload):
            return 122
        ctypes.memmove(buffer, self.payload, len(self.payload))
        return 0


class TcpTableParsingTests(unittest.TestCase):
    def test_owner_pid_state_and_network_byte_order_select_the_real_remote(self):
        payload = tcp_table(
            tcp_row('1.1.1.1', 443, pid=99),
            tcp_row('8.8.8.8', 20334, pid=42),
            tcp_row('9.9.9.9', 80, pid=42, state=2),
            tcp_row('8.8.4.4', 65535, pid=43),
        )
        self.assertEqual(native._parse_tcp_table(payload, {42, 43}), [
            Endpoint('8.8.8.8', 20334, 'native'),
            Endpoint('8.8.4.4', 65535, 'native'),
        ])

    def test_rejects_every_non_public_address_and_zero_port(self):
        hosts = ('0.0.0.0', '10.1.2.3', '100.64.0.1', '127.0.0.1',
                 '169.254.1.1', '172.16.0.1', '192.168.0.1', '192.0.2.1',
                 '198.51.100.1', '203.0.113.1', '224.0.0.1', '255.255.255.255')
        rows = [tcp_row(host) for host in hosts]
        rows.append(tcp_row('8.8.8.8', 0))
        # Reject corrupt port DWORDs instead of truncating them to a usable port.
        rows.append(struct.pack('<6I', 5, 0, 0, 0x08080808, 0x10000, 42))
        self.assertEqual(native._parse_tcp_table(tcp_table(*rows), {42}), [])

    def test_deduplicates_sorts_business_then_https_and_truncates_at_eight(self):
        payload = tcp_table(
            *(tcp_row(f'8.8.4.{index}', 80) for index in range(1, 11)),
            tcp_row('1.1.1.1', 443),
            tcp_row('9.9.9.9', 20334),
            tcp_row('9.9.9.9', 20334, pid=43),
        )
        self.assertEqual(native._parse_tcp_table(payload, {42, 43}), [
            Endpoint('9.9.9.9', 20334, 'native'),
            Endpoint('1.1.1.1', 443, 'native'),
            *(Endpoint(f'8.8.4.{index}', 80, 'native') for index in range(1, 7)),
        ])

    def test_same_host_with_different_ports_remains_distinct(self):
        self.assertEqual(native._parse_tcp_table(tcp_table(
            tcp_row('8.8.8.8', 443), tcp_row('8.8.8.8', 20334)), {42}), [
                Endpoint('8.8.8.8', 20334, 'native'),
                Endpoint('8.8.8.8', 443, 'native'),
            ])

    def test_rejects_short_truncated_and_oversized_tables_before_parsing_rows(self):
        malformed = (b'', b'\0\0\0', struct.pack('<I', 1),
                     struct.pack('<I', 2) + tcp_row(), struct.pack('<I', 0xffffffff))
        for payload in malformed:
            with self.subTest(payload_length=len(payload)):
                self.assertEqual(native._parse_tcp_table(payload, {42}), [])
        with patch.object(native, '_MAX_TCP_ROWS', 1):
            self.assertEqual(native._parse_tcp_table(tcp_table(tcp_row(), tcp_row()), {42}), [])
        with patch.object(native, '_MAX_TABLE_BYTES', 8):
            self.assertEqual(native._parse_tcp_table(tcp_table(tcp_row()), {42}), [])


class ProcessSnapshotTests(unittest.TestCase):
    def process_ids(self, api):
        with patch.object(native, '_get_last_error', lambda: api.error):
            return native._process_ids(api)

    def test_exact_case_insensitive_basename_and_unique_positive_pids(self):
        api = Kernel32Fixture([
            (42, 'Tatotalk.exe'), (42, 'Tatotalk.exe'), (43, 'TATOTALK.EXE'),
            (44, 'Tatotalk.exe.old'), (45, 'OtherTatotalk.exe'),
            (46, 'Tatotalk'), (47, r'relative\Tatotalk.exe'), (0, 'Tatotalk.exe'),
        ])
        self.assertEqual(self.process_ids(api), {42, 43, 47})
        self.assertEqual(api.closed_handles, [123])

    def test_empty_snapshot_returns_empty_and_closes_handle(self):
        api = Kernel32Fixture()
        self.assertEqual(self.process_ids(api), set())
        self.assertEqual(api.closed_handles, [123])

    def test_first_entry_failure_still_closes_the_snapshot(self):
        api = Kernel32Fixture(fail_first=True)
        with self.assertRaises(OSError):
            self.process_ids(api)
        self.assertEqual(api.closed_handles, [123])

    def test_access_denied_after_a_matching_pid_discards_the_partial_snapshot(self):
        api = Kernel32Fixture([(42, 'Tatotalk.exe')], final_error=5)
        with self.assertRaises(OSError):
            self.process_ids(api)
        self.assertEqual(api.closed_handles, [123])

    def test_invalid_snapshot_handles_are_not_used_or_closed(self):
        for handle in (None, 0, ctypes.c_void_p(-1).value):
            with self.subTest(handle=handle):
                api = Kernel32Fixture(handle=handle)
                with self.assertRaises(OSError):
                    self.process_ids(api)
                self.assertEqual(api.closed_handles, [])

    def test_snapshot_enumeration_is_bounded_even_with_repeated_pids(self):
        api = Kernel32Fixture([(42, 'Tatotalk.exe')] * 3)
        with patch.object(native, '_MAX_PROCESSES', 2):
            with self.assertRaises(OSError):
                self.process_ids(api)
        self.assertEqual(api.closed_handles, [123])


class NativeEndpointsTests(unittest.TestCase):
    def discover(self, kernel, iphelper):
        with patch.object(native.sys, 'platform', 'win32'), \
                patch.object(native, '_load_windows_libraries', return_value=(kernel, iphelper)), \
                patch.object(native, '_get_last_error', lambda: kernel.error):
            return native.native_endpoints()

    def test_native_enumeration_connects_owner_snapshot_to_real_table_parsing(self):
        kernel = Kernel32Fixture([(42, 'Tatotalk.exe'), (99, 'unrelated.exe')])
        table = tcp_table(tcp_row('1.1.1.1', 443, pid=99), tcp_row('8.8.8.8', 20334))
        self.assertEqual(self.discover(kernel, IpHelperFixture(table)), [
            Endpoint('8.8.8.8', 20334, 'native')])

    def test_absent_client_returns_empty_without_querying_the_tcp_table(self):
        kernel = Kernel32Fixture([(99, 'unrelated.exe')])
        self.assertEqual(self.discover(kernel, object()), [])

    def test_non_windows_returns_empty_without_loading_windows_dlls(self):
        with patch.object(native.sys, 'platform', 'linux'), \
                patch.object(native, '_load_windows_libraries', side_effect=AssertionError):
            self.assertEqual(native.native_endpoints(), [])

    def test_snapshot_access_denied_returns_empty_without_exposing_details(self):
        kernel = Kernel32Fixture([(42, 'Tatotalk.exe')], final_error=5)
        self.assertEqual(self.discover(kernel, object()), [])
        self.assertEqual(kernel.closed_handles, [123])

    def test_tcp_query_access_denied_returns_empty(self):
        kernel = Kernel32Fixture([(42, 'Tatotalk.exe')])
        self.assertEqual(self.discover(kernel, IpHelperFixture(b'', error=5)), [])

    def test_tcp_table_growth_is_retried_with_the_new_bounded_size(self):
        payload = tcp_table(tcp_row())
        kernel = Kernel32Fixture([(42, 'Tatotalk.exe')])
        self.assertEqual(self.discover(kernel, IpHelperFixture(payload, initial_size=4)), [
            Endpoint('8.8.8.8', 20334, 'native')])

    def test_forever_growing_table_and_oversized_allocations_return_empty(self):
        for options in ({'grow_forever': True}, {'initial_size': 0},
                        {'initial_size': 0xffffffff}):
            with self.subTest(options=options):
                kernel = Kernel32Fixture([(42, 'Tatotalk.exe')])
                self.assertEqual(self.discover(kernel, IpHelperFixture(tcp_table(tcp_row()), **options)), [])


if __name__ == '__main__':
    unittest.main()
