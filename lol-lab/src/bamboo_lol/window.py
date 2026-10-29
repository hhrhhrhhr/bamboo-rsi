"""Physical client coordinates for the bounded Windows input adapter."""
import ctypes
from ctypes import wintypes
import os
import sys

from bamboo_delta.protocol import ProbeError
from bamboo_delta.windows import Window


def process_integrity(pid):
    """Read a process token's integrity RID; never requests or changes privileges."""
    if sys.platform != "win32":
        raise ProbeError("windows_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.IsValidSid.argtypes = [wintypes.LPVOID]
    security.GetSidSubAuthorityCount.argtypes = [wintypes.LPVOID]
    security.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    security.GetSidSubAuthority.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    security.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    process = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    token, needed = wintypes.HANDLE(), wintypes.DWORD()
    try:
        if not process or not security.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise ProbeError("process_integrity_unavailable")
        security.GetTokenInformation(token, 25, None, 0, ctypes.byref(needed))
        if not ctypes.sizeof(ctypes.c_void_p) <= needed.value <= 65536:
            raise ProbeError("process_integrity_unavailable")
        buffer = ctypes.create_string_buffer(needed.value)
        if not security.GetTokenInformation(token, 25, buffer, needed.value, ctypes.byref(needed)):
            raise ProbeError("process_integrity_unavailable")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not sid or not security.IsValidSid(sid):
            raise ProbeError("process_integrity_unavailable")
        count = security.GetSidSubAuthorityCount(sid)
        if not count or not count.contents.value:
            raise ProbeError("process_integrity_unavailable")
        rid = security.GetSidSubAuthority(sid, count.contents.value - 1)
        if not rid:
            raise ProbeError("process_integrity_unavailable")
        return rid.contents.value
    finally:
        if token:
            kernel.CloseHandle(token)
        if process:
            kernel.CloseHandle(process)


def check_input_privilege(pid):
    if process_integrity(pid) > process_integrity(os.getpid()):
        raise ProbeError("game_input_privilege_mismatch")


def physical_coordinates():
    if sys.platform != "win32":
        raise ProbeError("windows_required")
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if not user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
        raise ProbeError("physical_client_coordinates_unavailable")


class GameWindow(Window):
    def __init__(self, executable):
        physical_coordinates()
        super().__init__(executable)
        check_input_privilege(self.pid)

    def validate(self):
        # The recorder watchdog calls this from another thread. DPI awareness
        # is thread-local: initialize every caller before reading client pixels.
        physical_coordinates()
        super().validate()
