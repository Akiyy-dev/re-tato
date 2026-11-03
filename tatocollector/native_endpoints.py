"""Read public IPv4 TCP remotes from an already running Tatotalk client.

Only Toolhelp process metadata and the IP Helper TCP table are queried. No
process is opened, started, or contacted, and no client files are accessed.
"""

import ctypes
import ntpath
import socket
import struct
import sys

from .discovery import Endpoint, valid_endpoint


_MAX_PROCESSES = 65536
_MAX_TCP_ROWS = 65536
_TCP_ROW_SIZE = 24
_MAX_TABLE_BYTES = 4 + _MAX_TCP_ROWS * _TCP_ROW_SIZE
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_get_last_error = getattr(ctypes, 'get_last_error', lambda: 0)
_set_last_error = getattr(ctypes, 'set_last_error', lambda value: None)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', ctypes.c_uint32),
        ('cntUsage', ctypes.c_uint32),
        ('th32ProcessID', ctypes.c_uint32),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', ctypes.c_uint32),
        ('cntThreads', ctypes.c_uint32),
        ('th32ParentProcessID', ctypes.c_uint32),
        ('pcPriClassBase', ctypes.c_int32),
        ('dwFlags', ctypes.c_uint32),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


def _load_windows_libraries():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    iphelper = ctypes.WinDLL('iphlpapi', use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel32, name)
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        function.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    iphelper.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
        ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32,
    ]
    iphelper.GetExtendedTcpTable.restype = ctypes.c_uint32
    return kernel32, iphelper


def _process_ids(kernel32) -> set[int]:
    snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, 0, _INVALID_HANDLE_VALUE):
        raise OSError
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        pids = set()
        count = 0
        _set_last_error(0)
        present = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            count += 1
            if count > _MAX_PROCESSES:
                raise OSError
            if (entry.th32ProcessID > 0
                    and ntpath.basename(entry.szExeFile).casefold() == 'tatotalk.exe'):
                pids.add(int(entry.th32ProcessID))
            _set_last_error(0)
            present = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        if _get_last_error() != _ERROR_NO_MORE_FILES:
            raise OSError
        return pids
    finally:
        if not kernel32.CloseHandle(snapshot):
            raise OSError


def _tcp_table(iphelper) -> bytes:
    size = ctypes.c_uint32()
    # AF_INET and TCP_TABLE_OWNER_PID_ALL include ownership and connection state.
    result = iphelper.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 5, 0)
    if result not in (0, _ERROR_INSUFFICIENT_BUFFER):
        raise OSError
    for _ in range(3):
        if not 4 <= size.value <= _MAX_TABLE_BYTES:
            raise OSError
        capacity = size.value
        buffer = ctypes.create_string_buffer(capacity)
        result = iphelper.GetExtendedTcpTable(buffer, ctypes.byref(size), False, 2, 5, 0)
        if result == _ERROR_INSUFFICIENT_BUFFER:
            continue
        if result or not 4 <= size.value <= capacity:
            raise OSError
        return buffer.raw[:size.value]
    raise OSError


def _parse_tcp_table(data: bytes, pids: set[int]) -> list[Endpoint]:
    """Parse little-endian Windows DWORD rows with network-order IPv4/ports."""
    if len(data) < 4 or len(data) > _MAX_TABLE_BYTES:
        return []
    count, = struct.unpack_from('<I', data)
    if count > _MAX_TCP_ROWS or count > (len(data) - 4) // _TCP_ROW_SIZE:
        return []
    candidates = []
    seen = set()
    for index in range(count):
        state, _, _, address, port_word, pid = struct.unpack_from(
            '<6I', data, 4 + index * _TCP_ROW_SIZE)
        if pid not in pids or state != 5 or port_word > 0xffff:  # ESTABLISHED
            continue
        host = socket.inet_ntoa(struct.pack('<I', address))
        port = socket.ntohs(port_word)
        if not valid_endpoint(host, port) or (host, port) in seen:
            continue
        seen.add((host, port))
        candidates.append(Endpoint(host, port, 'native'))
    candidates.sort(key=lambda item: 0 if item.port == 20334 else 1 if item.port == 443 else 2)
    return candidates[:8]


def native_endpoints() -> list[Endpoint]:
    """Return at most eight remotes, or nothing when native discovery is unavailable."""
    if sys.platform != 'win32':
        return []
    try:
        kernel32, iphelper = _load_windows_libraries()
        pids = _process_ids(kernel32)
        if not pids:
            return []
        return _parse_tcp_table(_tcp_table(iphelper), pids)
    except (OSError, ValueError, AttributeError, OverflowError, MemoryError, ctypes.ArgumentError):
        # Discovery is optional. Do not expose process details or raw OS exceptions.
        return []
