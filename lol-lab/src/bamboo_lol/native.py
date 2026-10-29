"""Marked, bounded Win32 inputs and a passive human-intervention watchdog.

No focus changes, process injection, elevation, memory reads or anti-cheat workarounds.
Hooks count unowned input only; key values and typed text are never retained.
"""
import ctypes
from ctypes import wintypes
import secrets
import sys
import threading
import time

from bamboo_delta.protocol import ProbeError
from .protocol import GRID, SKILLS

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t


class MouseInput(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


class KeyInput(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


class InputUnion(ctypes.Union):
    _fields_ = [("mouse", MouseInput), ("key", KeyInput)]


class Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", InputUnion)]


class KeyHook(ctypes.Structure):
    _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


class MouseHook(ctypes.Structure):
    _fields_ = [("point", wintypes.POINT), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", ULONG_PTR)]


class InputMonitor:
    def __init__(self, token):
        self.token, self.count, self.error = token, 0, None
        self.ready = threading.Event()
        self.thread = None
        self.thread_id = None

    def start(self):
        if sys.platform != "win32":
            raise ProbeError("windows_required")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            self.stop()
            raise ProbeError("input_monitor_unavailable")

    def _run(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        callback = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, callback, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user.CallNextHookEx.restype = LRESULT
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel.GetModuleHandleW.restype = wintypes.HMODULE
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.thread_id = kernel.GetCurrentThreadId()
        hooks = []

        def make_hook(structure):
            @callback
            def handle(code, message, pointer):
                if code >= 0 and ctypes.cast(pointer, ctypes.POINTER(structure)).contents.extra != self.token:
                    self.count += 1
                return user.CallNextHookEx(None, code, message, pointer)
            return handle

        callbacks = [make_hook(KeyHook), make_hook(MouseHook)]
        try:
            for kind, handler in zip((13, 14), callbacks):
                hook = user.SetWindowsHookExW(kind, handler, kernel.GetModuleHandleW(None), 0)
                if not hook:
                    raise OSError("hook unavailable")
                hooks.append(hook)
            message = wintypes.MSG()
            # Ensure the message queue exists before stop() can post WM_QUIT.
            user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self.ready.set()
            while True:
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    if result < 0:
                        self.error = "input_monitor_failed"
                    break
        except Exception:
            self.error = "input_monitor_failed"
            self.ready.set()
        finally:
            for hook in hooks:
                user.UnhookWindowsHookEx(hook)

    def check(self):
        if self.error or not self.thread or not self.thread.is_alive():
            raise ProbeError("input_monitor_failed")
        if self.count:
            raise ProbeError("external_input_detected")

    def stop(self):
        if self.thread_id and self.thread and self.thread.is_alive():
            ctypes.WinDLL("user32").PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
            self.thread.join(timeout=2)
        return bool(self.thread is None or not self.thread.is_alive())


class Executor:
    """Every action ends with stop; no movement continues while the model is queried."""
    def __init__(self, target):
        if sys.platform != "win32":
            raise ProbeError("windows_required")
        self.target, self.token = target, secrets.randbits(32) or 1
        self.monitor = InputMonitor(self.token)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        self.user.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self.user.MapVirtualKeyW.restype = wintypes.UINT
        self.user.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self.held = set()
        self.mouse_held = set()
        self.released = False

    def start(self):
        self.target.validate()
        self.monitor.start()

    def check(self):
        self.target.validate()
        self.monitor.check()

    def _send(self, item):
        if self.user.SendInput(1, ctypes.byref(item), ctypes.sizeof(Input)) != 1:
            raise ProbeError("game_input_rejected")

    def _key(self, key, up=False):
        scan = self.user.MapVirtualKeyW(key, 0)
        if not scan:
            raise ProbeError("key_scan_code_unavailable")
        flags = 0x0008 | (2 if up else 0) | (1 if scan & 0xFF00 else 0)
        self._send(Input(type=1, key=KeyInput(vk=0, scan=scan & 0xFF, flags=flags, extra=self.token)))

    def tap(self, key):
        self.check()
        self.held.add(key)
        try:
            self._key(key)
            # Keep down/up across render frames; an immediate pair can disappear
            # from a game's polled keyboard state even when SendInput accepted it.
            time.sleep(.05)
            self.check()
        finally:
            self._key(key, True)
            self.held.discard(key)

    def _mouse(self, flags, x=0, y=0):
        self._send(Input(type=0, mouse=MouseInput(dx=x, dy=y, flags=flags, extra=self.token)))

    def point(self, cell):
        x, y = GRID[cell]
        self.point_normalized(x, y)

    def point_normalized(self, x, y):
        self.check()
        if not 0 < x < 1 or not 0 < y < 1:
            raise ProbeError("invalid_executor_target")
        width, height = self.target.dimensions
        p = wintypes.POINT(round(x * (width - 1)), round(y * (height - 1)))
        if not self.user.ClientToScreen(self.target.hwnd, ctypes.byref(p)):
            raise ProbeError("client_coordinates_unavailable")
        left, top, sw, sh = [self.user.GetSystemMetrics(i) for i in (76, 77, 78, 79)]
        if not left <= p.x < left + sw or not top <= p.y < top + sh or min(sw, sh) < 2:
            raise ProbeError("target_outside_screen")
        self._mouse(0x8000 | 0x4000 | 1, round((p.x-left)*65535/(sw-1)), round((p.y-top)*65535/(sh-1)))

    def click(self, right=False):
        self.check()
        down, up = (8, 16) if right else (2, 4)
        self.mouse_held.add(up)
        try:
            self._mouse(down)
            time.sleep(.05)
            self.check()
        finally:
            self._mouse(up)
            self.mouse_held.discard(up)

    def perform(self, skill, target, milliseconds, check):
        if skill not in SKILLS or milliseconds not in (250, 400, 600):
            raise ProbeError("invalid_executor_action")
        check(); self.check()
        if skill in ("move", "attack_move", "r", "advance_lane", "melee", "farm_wave"):
            if isinstance(target, dict):
                from .protocol import visual_target
                if skill != 'farm_wave' or not visual_target(target):
                    raise ProbeError('invalid_executor_target')
                self.point_normalized(*target['point'])
            else:
                if target not in GRID:
                    raise ProbeError("invalid_executor_target")
                self.point(target)
        try:
            if skill in ("move", "advance_lane"):
                self.click(right=True)
            elif skill == "attack_move":
                self.tap(ord("A")); self.click()
            elif skill in ("q", "w", "e", "r"):
                self.tap(ord(skill.upper()))
                if skill == "r":
                    self.click()  # Fixed scenario uses default normal casting.
            elif skill == "recall":
                self.tap(ord("B"))
            elif skill == "center_camera":
                self.tap(0x20)
            elif skill == "dismiss_idle":
                # Only the frozen, model-confirmed in-game idle notice has this action.
                self.point_normalized(.5, .453)
                self.click()
            elif skill in ('join_top_lane', 'return_to_base'):
                width, height = self.target.dimensions
                if abs(width / height - 16 / 9) > .01:
                    raise ProbeError('unsupported_fixed_minimap_layout')
                # Frozen default-HUD Map11 ORDER waypoints, never hidden enemy coordinates.
                self.point_normalized(*((.832, .765) if skill == 'join_top_lane' else (.825, .965)))
                self.click(right=True)
                # Keep the champion in the captured frame throughout movement.
                # A tap centers once, then the champion can walk off camera.
                self.held.add(0x20)
                self._key(0x20)
            elif skill == 'melee':
                self.tap(ord('Q'))
                self.tap(ord('A')); self.click()
            elif skill == 'farm_wave':
                self.tap(ord('A')); self.click()
            elif skill.startswith("level_"):
                self.held.add(0x11)
                try:
                    self._key(0x11)
                    time.sleep(.05)
                    self.tap(ord(skill[-1].upper()))
                finally:
                    self._key(0x11, True)
                    self.held.discard(0x11)
            # Apply screen coordinates against the observed camera first. Centering
            # before the click would move the target out from under those coordinates.
            if skill in ('advance_lane', 'melee', 'farm_wave'):
                self.held.add(0x20)
                self._key(0x20)
            duration = 8.2 if skill == "recall" else milliseconds / 1000
            if skill in ('join_top_lane', 'advance_lane', 'return_to_base'):
                duration *= 5  # 1.25..3 seconds, checked every 40 ms and always stopped.
            elif skill == 'melee':
                duration *= 2  # Allow the empowered attack wind-up before stopping.
            elif skill == 'farm_wave':
                duration *= 3  # 0.75..1.8 seconds to approach the visible wave.
            end = time.monotonic() + duration
            while time.monotonic() < end:
                check(); self.check()
                time.sleep(.04)
        finally:
            # A dead/lost game may prevent stop, but key/button-up is always attempted.
            self.release()
            self.check()
            self.tap(ord("S"))

    def release(self):
        errors = []
        for key in list(self.held):
            try:
                self._key(key, True); self.held.remove(key)
            except Exception:
                errors.append(key)
        for up in list(self.mouse_held):
            try:
                self._mouse(up); self.mouse_held.remove(up)
            except Exception:
                errors.append(up)
        self.released = not errors
        return self.released

    def stop(self):
        self.release()
        # Do not interfere with a user who has taken control or switched windows.
        try:
            self.check(); self.tap(ord("S"))
        except ProbeError:
            pass
        finally:
            released = self.release()
            stopped = self.monitor.stop()
        return released and stopped
