"""Bounded overlapped Windows named-pipe IPC, with an independent stop pipe."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import re
import struct
import sys
import time
import uuid

from .contracts import ActionOption, ActionResult, Observation

MAX_PAYLOAD_BYTES = 16 * 1024 * 1024


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class _WindowsPipeConnection:
    def __init__(self, name, timeout):
        if sys.platform != "win32":
            raise OSError("The native game connection requires Windows")
        self.deadline = time.monotonic() + timeout
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self.kernel.WaitNamedPipeW.restype = wintypes.BOOL
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        for operation in (self.kernel.ReadFile, self.kernel.WriteFile):
            operation.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)]
            operation.restype = wintypes.BOOL
        self.kernel.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped),
                                                   ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        self.kernel.GetOverlappedResult.restype = wintypes.BOOL
        self.kernel.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Overlapped)]
        self.kernel.CancelIoEx.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        path = "\\\\.\\pipe\\" + name
        while True:
            # FILE_FLAG_OVERLAPPED permits bounded reads and cancellation.
            handle = self.kernel.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != ctypes.c_void_p(-1).value:
                self.handle = handle
                break
            error = ctypes.get_last_error()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out connecting to the game execution host")
            if error not in (2, 231):
                raise ConnectionError(f"Windows pipe connection failed ({error})")
            self.kernel.WaitNamedPipeW(path, max(1, min(50, int(remaining * 1000))))
            if error == 2:
                time.sleep(min(0.01, remaining))

    def _transfer(self, buffer, size, *, write):
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise ConnectionError("Could not create a pipe completion event")
        overlapped = _Overlapped(hEvent=event)
        transferred = wintypes.DWORD()
        try:
            operation = self.kernel.WriteFile if write else self.kernel.ReadFile
            completed = operation(self.handle, buffer, size, ctypes.byref(transferred), ctypes.byref(overlapped))
            if not completed:
                error = ctypes.get_last_error()
                if error != 997:  # ERROR_IO_PENDING
                    raise ConnectionError(f"Windows pipe I/O failed ({error})")
                remaining = max(0, int((self.deadline - time.monotonic()) * 1000))
                waited = self.kernel.WaitForSingleObject(event, remaining)
                if waited != 0:
                    self.kernel.CancelIoEx(self.handle, ctypes.byref(overlapped))
                    # Drain completion before freeing the OVERLAPPED or its buffer.
                    self.kernel.GetOverlappedResult(self.handle, ctypes.byref(overlapped),
                                                    ctypes.byref(transferred), True)
                    raise TimeoutError("Timed out communicating with the game execution host")
                if not self.kernel.GetOverlappedResult(self.handle, ctypes.byref(overlapped),
                                                       ctypes.byref(transferred), False):
                    raise ConnectionError("Windows pipe operation did not complete")
            if transferred.value == 0:
                raise ConnectionError("Game execution host closed its pipe")
            return transferred.value
        finally:
            self.kernel.CloseHandle(event)

    def write_all(self, raw):
        offset = 0
        while offset < len(raw):
            buffer = ctypes.create_string_buffer(raw[offset:])
            offset += self._transfer(buffer, len(raw) - offset, write=True)

    def read_exact(self, count):
        chunks = []
        while count:
            buffer = ctypes.create_string_buffer(count)
            received = self._transfer(buffer, count, write=False)
            chunks.append(buffer.raw[:received])
            count -= received
        return b"".join(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)


class WindowsPipeSession:
    def __init__(self, name="startlux-genshin", *, timeout=5.0, connector=None):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", name) or timeout <= 0:
            raise ValueError("invalid pipe name or request timeout")
        self.name = name
        self.timeout = timeout
        self.connector = connector or _WindowsPipeConnection

    def _request(self, operation, data=None):
        identity = uuid.uuid4().hex
        payload = json.dumps({"version": 1, "request_id": identity, "operation": operation,
                              "data": data or {}}, ensure_ascii=False, allow_nan=False).encode()
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError("agent request exceeds the IPC size budget")
        control = operation == "stop"
        with self.connector(self.name + (".control" if control else ""),
                            min(self.timeout, 0.2) if control else self.timeout) as pipe:
            pipe.write_all(struct.pack("<I", len(payload)) + payload)
            length = struct.unpack("<I", pipe.read_exact(4))[0]
            if not 0 < length <= MAX_PAYLOAD_BYTES:
                raise ValueError("invalid agent response frame length")
            response = json.loads(pipe.read_exact(length))
        if (not isinstance(response, dict) or type(response.get("version")) is not int
                or response["version"] != 1 or response.get("request_id") != identity):
            raise ValueError("agent response is not bound to this request")
        if response.get("success") is not True:
            raise RuntimeError("game execution host rejected the request")
        return response.get("data")

    def describe(self):
        result = self._request("describe")
        if not isinstance(result, dict):
            raise ValueError("invalid execution host descriptor")
        return result

    def arm(self):
        self._request("arm")

    def observe(self):
        return Observation.model_validate(self._request("observe"))

    def candidates(self, observation):
        result = self._request("candidates", {"frame_id": observation.frame_id})
        if not isinstance(result, list):
            raise ValueError("invalid execution host catalog")
        return [ActionOption.model_validate(option) for option in result]

    def start(self, command):
        return ActionResult.model_validate(self._request("start", command.model_dump()))

    def reconcile(self, command_id):
        return ActionResult.model_validate(self._request("reconcile", {"command_id": command_id}))

    def cancel(self, command_id):
        return ActionResult.model_validate(self._request("cancel", {"command_id": command_id}))

    def checkpoint(self, command_id, *, review_frame_id=None):
        data = {"command_id": command_id}
        if review_frame_id is not None:
            data["review_frame_id"] = review_frame_id
        return ActionResult.model_validate(self._request("checkpoint", data))

    def stop(self):
        self._request("stop")

    def close(self):
        self.stop()
