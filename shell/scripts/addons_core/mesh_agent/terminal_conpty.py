# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Pseudo-terminal host for the embedded Open Grok terminal.

Windows path prefers ``pywinpty`` (ConPTY via winpty's backend), which is the
reliable host for full TUIs (``isatty()`` true, alt-screen, colors). Install
into Cadex/Blender's Python::

    <cadex>/python/bin/python.exe -m pip install pywinpty

A pure-ctypes ConPTY fallback is kept for environments without pywinpty; it
is enough for simple console I/O but may not satisfy every TUI.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time


if sys.platform != "win32":
    raise ImportError("terminal_conpty is Windows-only")


def list2cmdline(seq):
    return subprocess.list2cmdline(list(seq))


class ConPTY:
    """One pseudo-console + child process.

    Public API used by ``terminal_session``:
      read() -> bytes, write(data), resize(cols, rows), alive(), close()
      cols, rows
    """

    def __init__(self, command, cwd=None, env=None, cols=120, rows=40):
        self.cols = max(2, int(cols))
        self.rows = max(1, int(rows))
        self._closed = False
        self._impl = None
        self._backend = None

        if not isinstance(command, (list, tuple)):
            raise TypeError("command must be a list/tuple of argv tokens")
        argv = list(command)

        # Prefer pywinpty (battle-tested ConPTY).
        try:
            self._init_winpty(argv, cwd=cwd, env=env)
            return
        except Exception as winpty_err:
            self._winpty_error = winpty_err

        # Fallback: pure ctypes ConPTY (best-effort).
        try:
            self._init_ctypes(argv, cwd=cwd, env=env)
        except Exception as ctypes_err:
            raise OSError(
                "Could not create a pseudo-console. Install pywinpty into "
                "Cadex Python (`python -m pip install pywinpty`). "
                "winpty error: {!s}; ctypes error: {!s}".format(
                    getattr(self, "_winpty_error", None), ctypes_err)
            ) from ctypes_err

    # -- pywinpty backend ----------------------------------------------------

    def _init_winpty(self, argv, cwd=None, env=None):
        from winpty import PtyProcess

        # PtyProcess.spawn expects dimensions=(rows, cols).
        env_map = env if env is not None else os.environ.copy()
        # Ensure PATH can resolve bare names; spawn also does which().
        proc = PtyProcess.spawn(
            argv,
            cwd=cwd or os.getcwd(),
            env=env_map,
            dimensions=(self.rows, self.cols),
        )
        self._impl = proc
        self._backend = "winpty"
        self._out_buf = bytearray()
        self._out_lock = threading.Lock()
        self._reader_stop = threading.Event()
        self._reader = threading.Thread(
            target=self._winpty_read_loop, name="cadex-winpty-reader",
            daemon=True)
        self._reader.start()

    def _winpty_read_loop(self):
        proc = self._impl
        while not self._reader_stop.is_set():
            try:
                # Non-blocking-ish: pywinpty read may block; short chunks.
                chunk = proc.read(4096)
            except EOFError:
                break
            except Exception:
                # Closed / cancelled.
                if self._reader_stop.is_set() or not proc.isalive():
                    break
                time.sleep(0.02)
                continue
            if not chunk:
                if not proc.isalive():
                    break
                time.sleep(0.01)
                continue
            if isinstance(chunk, str):
                data = chunk.encode("utf-8", errors="replace")
            else:
                data = chunk
            with self._out_lock:
                self._out_buf.extend(data)

    # -- pure ctypes backend (fallback) --------------------------------------

    def _init_ctypes(self, argv, cwd=None, env=None):
        from . import terminal_conpty_ctypes as _ctypes_impl
        self._impl = _ctypes_impl.CtypesConPTY(
            argv, cwd=cwd, env=env, cols=self.cols, rows=self.rows)
        self._backend = "ctypes"
        # ctypes impl has its own buffer; wrap to same API.
        self._out_buf = None
        self._out_lock = None
        self._reader_stop = None
        self._reader = None

    # -- shared API ----------------------------------------------------------

    def read(self, max_bytes=65536):
        if self._backend == "winpty":
            with self._out_lock:
                if not self._out_buf:
                    return b""
                data = bytes(self._out_buf[:max_bytes])
                del self._out_buf[:max_bytes]
                return data
        return self._impl.read(max_bytes)

    def write(self, data):
        if self._closed or not data:
            return 0
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
            raw = data
        else:
            text = str(data)
            raw = text.encode("utf-8", errors="replace")
        if self._backend == "winpty":
            try:
                self._impl.write(text)
                return len(raw)
            except Exception:
                return 0
        return self._impl.write(raw)

    def resize(self, cols, rows):
        cols = max(2, int(cols))
        rows = max(1, int(rows))
        if cols == self.cols and rows == self.rows:
            return
        self.cols = cols
        self.rows = rows
        if self._closed:
            return
        if self._backend == "winpty":
            try:
                # winpty setwinsize(rows, cols)
                self._impl.setwinsize(rows, cols)
            except Exception:
                pass
            return
        self._impl.resize(cols, rows)

    def alive(self):
        if self._closed or self._impl is None:
            return False
        if self._backend == "winpty":
            try:
                return bool(self._impl.isalive())
            except Exception:
                return False
        return self._impl.alive()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reader_stop is not None:
            self._reader_stop.set()
        if self._backend == "winpty":
            try:
                if self._impl is not None and self._impl.isalive():
                    self._impl.terminate(force=True)
            except Exception:
                pass
            try:
                if self._impl is not None:
                    self._impl.close(force=True)
            except Exception:
                pass
        else:
            try:
                self._impl.close()
            except Exception:
                pass
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1.0)


# Back-compat name.
subprocess_list2cmdline = list2cmdline
