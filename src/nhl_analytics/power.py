"""Scoped Windows idle-sleep protection for the local API, with the display free to turn off."""
import ctypes
import logging
import os
from contextlib import contextmanager
from ctypes import wintypes

LOG = logging.getLogger(__name__)


class DetailedReason(ctypes.Structure):
    _fields_ = [('module', wintypes.HMODULE), ('reason_id', wintypes.ULONG),
                ('count', wintypes.ULONG), ('strings', ctypes.POINTER(wintypes.LPWSTR))]


class Reason(ctypes.Union):
    _fields_ = [('simple', wintypes.LPWSTR), ('detailed', DetailedReason)]


class ReasonContext(ctypes.Structure):
    _fields_ = [('version', wintypes.ULONG), ('flags', wintypes.DWORD), ('reason', Reason)]


def windows_kernel():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.PowerCreateRequest.argtypes = [ctypes.POINTER(ReasonContext)]
    kernel.PowerCreateRequest.restype = wintypes.HANDLE
    for name in ('PowerSetRequest', 'PowerClearRequest'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.c_int]
        function.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@contextmanager
def keep_awake(kernel=None):
    if kernel is None and (os.name != 'nt' or os.environ.get('NHL_KEEP_AWAKE', '1') == '0'):
        yield False
        return
    handle = None
    applied = []
    try:
        kernel = kernel or windows_kernel()
        context = ReasonContext(0, 1, Reason(simple='NHL Analytics: pregame forecasts and live game collection'))
        handle = kernel.PowerCreateRequest(ctypes.byref(context))
        if handle in (None, 0, ctypes.c_void_p(-1).value):
            handle = None
            raise OSError('PowerCreateRequest failed')
        # SystemRequired + ExecutionRequired, never DisplayRequired or AwayMode.
        for request in (1, 3):
            if not kernel.PowerSetRequest(handle, request):
                raise OSError('PowerSetRequest failed')
            applied.append(request)
    except OSError:
        LOG.warning('Idle-sleep protection unavailable; keep Windows awake manually')
        for request in reversed(applied):
            kernel.PowerClearRequest(handle, request)
        applied.clear()
        if handle:
            kernel.CloseHandle(handle)
            handle = None
    try:
        if handle:
            LOG.warning('Idle-sleep protection active while API runs; display may turn off. Keep laptop plugged in.')
        yield bool(handle)
    finally:
        for request in reversed(applied):
            kernel.PowerClearRequest(handle, request)
        if handle:
            kernel.CloseHandle(handle)
