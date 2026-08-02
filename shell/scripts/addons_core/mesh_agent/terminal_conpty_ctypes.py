# SPDX-FileCopyrightText: 2026 Mesh Authors
# SPDX-License-Identifier: GPL-2.0-or-later
"""Best-effort pure-ctypes ConPTY (fallback when pywinpty is missing)."""
from __future__ import annotations
import ctypes, subprocess, threading, sys
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("Windows only")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
HRESULT = ctypes.c_long
SIZE_T = ctypes.c_size_t
HPCON = wintypes.HANDLE
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
HANDLE_FLAG_INHERIT = 0x00000001
STILL_ACTIVE = 259
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]

class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", wintypes.LPVOID)]

class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]

def _pack_coord(cols, rows):
    return wintypes.DWORD((int(rows) << 16) | (int(cols) & 0xFFFF))

def _env_block(env):
    if env is None:
        return None
    parts = ["{}={}".format(k, v) for k, v in env.items() if k is not None and v is not None]
    return ctypes.create_unicode_buffer("\0".join(parts) + "\0\0")

class CtypesConPTY:
    def __init__(self, command, cwd=None, env=None, cols=120, rows=40):
        self.cols = max(2, int(cols)); self.rows = max(1, int(rows))
        self._hpc = HPCON(); self._pi = PROCESS_INFORMATION()
        self._closed = False; self._write_lock = threading.Lock()
        self._out_buf = bytearray(); self._out_lock = threading.Lock()
        self._reader_stop = threading.Event()
        cmdline = subprocess.list2cmdline(list(command))
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, False)
        in_r, in_w = wintypes.HANDLE(), wintypes.HANDLE()
        out_r, out_w = wintypes.HANDLE(), wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(in_r), ctypes.byref(in_w), ctypes.byref(sa), 0):
            raise OSError("CreatePipe in")
        if not kernel32.CreatePipe(ctypes.byref(out_r), ctypes.byref(out_w), ctypes.byref(sa), 0):
            raise OSError("CreatePipe out")
        kernel32.SetHandleInformation(in_r, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        kernel32.SetHandleInformation(out_w, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT)
        kernel32.CreatePseudoConsole.restype = HRESULT
        hr = kernel32.CreatePseudoConsole(_pack_coord(self.cols, self.rows), in_r, out_w, 0, ctypes.byref(self._hpc))
        kernel32.CloseHandle(in_r); kernel32.CloseHandle(out_w)
        if hr != 0:
            raise OSError(hr, "CreatePseudoConsole")
        self._input_write, self._output_read = in_w, out_r
        size = SIZE_T(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        self._attr_buf = ctypes.create_string_buffer(size.value)
        if not kernel32.InitializeProcThreadAttributeList(self._attr_buf, 1, 0, ctypes.byref(size)):
            raise OSError("Init attr list")
        hpc_value = ctypes.c_void_p(self._hpc.value)
        if not kernel32.UpdateProcThreadAttribute(self._attr_buf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc_value, ctypes.sizeof(ctypes.c_void_p), None, None):
            raise OSError("UpdateProcThreadAttribute")
        siex = STARTUPINFOEXW(); siex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        siex.lpAttributeList = ctypes.cast(self._attr_buf, wintypes.LPVOID)
        pi = PROCESS_INFORMATION(); cmd_buf = ctypes.create_unicode_buffer(cmdline)
        env_block = _env_block(env)
        flags = EXTENDED_STARTUPINFO_PRESENT | (CREATE_UNICODE_ENVIRONMENT if env_block else 0)
        if not kernel32.CreateProcessW(None, cmd_buf, None, None, False, flags, env_block, cwd, ctypes.byref(siex), ctypes.byref(pi)):
            raise OSError(ctypes.get_last_error(), "CreateProcessW")
        self._pi = pi; kernel32.CloseHandle(pi.hThread)
        kernel32.DeleteProcThreadAttributeList(self._attr_buf); self._attr_buf = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True); self._reader.start()

    def _read_loop(self):
        buf = ctypes.create_string_buffer(16384); n = wintypes.DWORD(0)
        while not self._reader_stop.is_set():
            ok = kernel32.ReadFile(self._output_read, buf, len(buf), ctypes.byref(n), None)
            if not ok or n.value == 0: break
            with self._out_lock: self._out_buf.extend(buf.raw[:n.value])

    def read(self, max_bytes=65536):
        with self._out_lock:
            data = bytes(self._out_buf[:max_bytes]); del self._out_buf[:max_bytes]; return data

    def write(self, data):
        if self._closed or not data: return 0
        if isinstance(data, str): data = data.encode("utf-8", errors="replace")
        with self._write_lock:
            n = wintypes.DWORD(0)
            kernel32.WriteFile(self._input_write, data, len(data), ctypes.byref(n), None)
            return n.value

    def resize(self, cols, rows):
        self.cols, self.rows = max(2,int(cols)), max(1,int(rows))
        if not self._closed: kernel32.ResizePseudoConsole(self._hpc, _pack_coord(self.cols, self.rows))

    def alive(self):
        if self._closed or not self._pi.hProcess: return False
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code))
        return code.value == STILL_ACTIVE

    def close(self):
        if self._closed: return
        self._closed = True; self._reader_stop.set()
        if self._pi.hProcess:
            if self.alive(): kernel32.TerminateProcess(self._pi.hProcess, 1)
            kernel32.CloseHandle(self._pi.hProcess)
        try: kernel32.ClosePseudoConsole(self._hpc)
        except Exception: pass
        for h in (self._input_write, self._output_read):
            try: kernel32.CloseHandle(h)
            except Exception: pass
