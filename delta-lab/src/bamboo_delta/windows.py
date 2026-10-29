"""Read-only Win32 target identity. Never changes focus, sends input or elevates."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys

from .protocol import ProbeError


class Window:
    def __init__(self, executable):
        if sys.platform != "win32":
            raise ProbeError("windows_required")
        import psutil
        self.psutil = psutil
        self.executable = os.path.normcase(str(Path(executable).resolve()))
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        candidates = []

        @callback_type
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if self.user32.IsWindowVisible(hwnd) and pid.value:
                try:
                    process = psutil.Process(pid.value)
                    if os.path.normcase(str(Path(process.exe()).resolve())) == self.executable:
                        rect = wintypes.RECT()
                        if self.user32.GetClientRect(hwnd, ctypes.byref(rect)) and rect.right >= 640 and rect.bottom >= 360:
                            candidates.append((int(hwnd), pid.value, process.create_time(), (rect.right, rect.bottom)))
                except (psutil.Error, OSError):
                    pass
            return True

        self.user32.EnumWindows(visit, 0)
        if len(candidates) != 1:
            raise ProbeError("game_window_missing_or_ambiguous")
        self.hwnd, self.pid, self.born, self.dimensions = candidates[0]
        self.validate()

    def validate(self):
        try:
            process = self.psutil.Process(self.pid)
            if (process.create_time() != self.born
                    or os.path.normcase(str(Path(process.exe()).resolve())) != self.executable):
                raise ProbeError("game_process_changed")
        except self.psutil.Error:
            raise ProbeError("game_process_unavailable") from None
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        if (pid.value != self.pid or self.user32.GetForegroundWindow() != self.hwnd
                or self.user32.IsIconic(self.hwnd)):
            raise ProbeError("game_not_foreground")
        if (not self.user32.GetClientRect(self.hwnd, ctypes.byref(rect))
                or (rect.right, rect.bottom) != self.dimensions):
            raise ProbeError("game_window_dimensions_changed")
