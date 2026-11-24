"""Lazy Windows native helpers. Imported only on Windows; uses Python ctypes.

API reference: https://learn.microsoft.com/windows/win32/procthread/job-objects
https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-createmutexw
"""

import ctypes as C
from ctypes import wintypes as W
import hashlib
import os


def api(dll, name, result, args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


def kernel():
    return C.WinDLL("kernel32", use_last_error=True)


class NamedLock:
    def __init__(self, resource):
        dll = kernel()
        name = "Global\\PyCoder-" + hashlib.sha256(resource.encode("utf-8")).hexdigest()
        create = api(dll, "CreateMutexW", W.HANDLE, [W.LPVOID, W.BOOL, W.LPCWSTR])
        self.close_handle = api(dll, "CloseHandle", W.BOOL, [W.HANDLE])
        C.set_last_error(0)
        self.handle = create(None, False, name)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        if C.get_last_error() == 183:  # ERROR_ALREADY_EXISTS; use handle lifetime, not thread ownership.
            self.close()
            raise ValueError("В этой папке или сессии уже запущен PyCoder")

    def close(self):
        if self.handle:
            self.close_handle(self.handle)
            self.handle = None


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BASIC_LIMITS(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_longlong), ("PerJobUserTimeLimit", C.c_longlong),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class EXTENDED_LIMITS(C.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMITS), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", C.c_size_t), ("JobMemoryLimit", C.c_size_t),
                ("PeakProcessMemoryUsed", C.c_size_t), ("PeakJobMemoryUsed", C.c_size_t)]


class Job:
    """Kill all descendants on close. Caller gates the launcher until assignment."""
    def __init__(self, process):
        dll = kernel()
        self.close_handle = api(dll, "CloseHandle", W.BOOL, [W.HANDLE])
        self.handle = api(dll, "CreateJobObjectW", W.HANDLE, [W.LPVOID, W.LPCWSTR])(None, None)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        configure = api(dll, "SetInformationJobObject", W.BOOL, [W.HANDLE, C.c_int, W.LPVOID, W.DWORD])
        assign = api(dll, "AssignProcessToJobObject", W.BOOL, [W.HANDLE, W.HANDLE])
        try:
            if not configure(self.handle, 9, C.byref(limits), C.sizeof(limits)) or not assign(self.handle, int(process._handle)):
                raise C.WinError(C.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.close_handle(self.handle)
            self.handle = None


def read_clipboard():
    user = C.WinDLL("user32", use_last_error=True)
    dll = kernel()
    opened = api(user, "OpenClipboard", W.BOOL, [W.HWND])(None)
    if not opened:
        return None
    try:
        handle = api(user, "GetClipboardData", W.HANDLE, [W.UINT])(13)
        if not handle:
            return ""
        pointer = api(dll, "GlobalLock", W.LPVOID, [W.HGLOBAL])(handle)
        if not pointer:
            return None
        try:
            return C.wstring_at(pointer)
        finally:
            api(dll, "GlobalUnlock", W.BOOL, [W.HGLOBAL])(handle)
    finally:
        api(user, "CloseClipboard", W.BOOL, [])()


def clipboard(text):
    user = C.WinDLL("user32", use_last_error=True)
    dll = kernel()
    alloc = api(dll, "GlobalAlloc", W.HGLOBAL, [W.UINT, C.c_size_t])
    lock = api(dll, "GlobalLock", W.LPVOID, [W.HGLOBAL])
    unlock = api(dll, "GlobalUnlock", W.BOOL, [W.HGLOBAL])
    free = api(dll, "GlobalFree", W.HGLOBAL, [W.HGLOBAL])
    open_clipboard = api(user, "OpenClipboard", W.BOOL, [W.HWND])
    close_clipboard = api(user, "CloseClipboard", W.BOOL, [])
    empty = api(user, "EmptyClipboard", W.BOOL, [])
    set_data = api(user, "SetClipboardData", W.HANDLE, [W.UINT, W.HANDLE])
    raw = (text + "\0").encode("utf-16-le")
    owner = api(dll, "GetConsoleWindow", W.HWND, [])()
    if not owner:
        return False
    handle = alloc(0x0002, len(raw))
    if not handle:
        return False
    opened = False
    try:
        pointer = lock(handle)
        if not pointer:
            return False
        C.memmove(pointer, raw, len(raw))
        unlock(handle)
        opened = bool(open_clipboard(owner))
        if not opened or not empty() or not set_data(13, handle):  # CF_UNICODETEXT
            return False
        handle = None  # Clipboard owns the allocation now.
        return True
    finally:
        if opened:
            close_clipboard()
        if handle:
            free(handle)
