# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Minimal VT100/xterm screen buffer for rendering Open Grok's TUI.

Handles the CSI/SGR/OSC subset ratatui-style apps emit: cursor motion, erase,
colors (16 + 256 + truecolor), alternate screen, scroll region, DEC private
modes we care about (cursor, mouse report flags). Not a full terminal; enough
to host interactive open-grok inside Cadex.
"""

from __future__ import annotations

from dataclasses import dataclass


# Default palette (xterm-ish): index 0-15, then approximate 256-color cube.
_ANSI16 = [
    (12, 12, 12), (205, 49, 49), (13, 188, 121), (229, 229, 16),
    (36, 114, 200), (188, 63, 188), (17, 168, 205), (204, 204, 204),
    (118, 118, 118), (241, 76, 76), (35, 209, 139), (245, 245, 67),
    (59, 142, 234), (214, 112, 214), (41, 184, 219), (242, 242, 242),
]


def _color_256(n):
    n = int(n) & 0xFF
    if n < 16:
        return _ANSI16[n]
    if n < 232:
        n -= 16
        r = n // 36
        g = (n % 36) // 6
        b = n % 6
        scale = (0, 95, 135, 175, 215, 255)
        return (scale[r], scale[g], scale[b])
    v = 8 + (n - 232) * 10
    return (v, v, v)


def _norm_rgb(c):
    if c is None:
        return None
    return (c[0] / 255.0, c[1] / 255.0, c[2] / 255.0, 1.0)


@dataclass
class Cell:
    ch: str = " "
    fg: tuple | None = None  # 0-255 RGB or None = default
    bg: tuple | None = None
    bold: bool = False
    inverse: bool = False
    underline: bool = False

    def copy(self):
        return Cell(self.ch, self.fg, self.bg, self.bold, self.inverse,
                    self.underline)


class TerminalScreen:
    """Character-cell screen + VT parser."""

    def __init__(self, cols=120, rows=40):
        self.cols = max(2, cols)
        self.rows = max(1, rows)
        self.cursor_x = 0
        self.cursor_y = 0
        self.cursor_visible = True
        self.mouse_mode = 0  # bitflags: 1000, 1002, 1006 (SGR)
        self.title = ""
        self._fg = None
        self._bg = None
        self._bold = False
        self._inverse = False
        self._underline = False
        self._charset = "G0"
        self._scroll_top = 0
        self._scroll_bot = self.rows - 1
        self._origin_mode = False
        self._auto_wrap = True
        self._insert_mode = False
        self._main = self._blank_grid()
        self._alt = self._blank_grid()
        self._using_alt = False
        self._saved_cursor = (0, 0)
        self._parser_buf = bytearray()
        self._utf8_hold = bytearray()
        self._osc_buf = bytearray()
        self._in_osc = False
        self._in_csi = False
        self._in_esc = False
        self._csi_buf = bytearray()
        self.dirty = True
        self.bell = False
        # OSC 52 clipboard payload from the guest app (e.g. open-grok).
        # Host reads via take_clipboard() and writes the OS clipboard.
        self.pending_clipboard = None

    def _blank_grid(self):
        return [[Cell() for _ in range(self.cols)] for _ in range(self.rows)]

    @property
    def grid(self):
        return self._alt if self._using_alt else self._main

    def resize(self, cols, rows):
        cols = max(2, int(cols))
        rows = max(1, int(rows))
        if cols == self.cols and rows == self.rows:
            return
        old_main, old_alt = self._main, self._alt
        old_cols, old_rows = self.cols, self.rows
        self.cols, self.rows = cols, rows
        self._scroll_top = 0
        self._scroll_bot = rows - 1

        def migrate(old):
            new = self._blank_grid()
            for y in range(min(old_rows, rows)):
                for x in range(min(old_cols, cols)):
                    new[y][x] = old[y][x].copy()
            return new

        self._main = migrate(old_main)
        self._alt = migrate(old_alt)
        self.cursor_x = min(self.cursor_x, cols - 1)
        self.cursor_y = min(self.cursor_y, rows - 1)
        self.dirty = True

    def feed(self, data: bytes):
        if not data:
            return
        for byte in data:
            self._feed_byte(byte)
        self.dirty = True

    def _feed_byte(self, byte):
        # OSC intermediate (ESC ] ... BEL or ST)
        if self._in_osc:
            if byte in (0x07,):  # BEL ends OSC
                self._handle_osc(bytes(self._osc_buf))
                self._osc_buf.clear()
                self._in_osc = False
            elif byte == 0x1B:
                # maybe ST = ESC \
                self._osc_buf.append(byte)
            elif (self._osc_buf and self._osc_buf[-1] == 0x1B
                  and byte == 0x5C):  # ST
                self._osc_buf.pop()
                self._handle_osc(bytes(self._osc_buf))
                self._osc_buf.clear()
                self._in_osc = False
            else:
                if len(self._osc_buf) < 8192:
                    self._osc_buf.append(byte)
            return

        if self._in_csi:
            self._csi_buf.append(byte)
            # CSI ends on final byte 0x40-0x7E
            if 0x40 <= byte <= 0x7E:
                self._handle_csi(bytes(self._csi_buf))
                self._csi_buf.clear()
                self._in_csi = False
            elif len(self._csi_buf) > 256:
                self._csi_buf.clear()
                self._in_csi = False
            return

        if self._in_esc:
            self._in_esc = False
            if byte == ord("["):
                self._in_csi = True
                self._csi_buf.clear()
                return
            if byte == ord("]"):
                self._in_osc = True
                self._osc_buf.clear()
                return
            if byte == ord("7"):
                self._saved_cursor = (self.cursor_x, self.cursor_y)
                return
            if byte == ord("8"):
                self.cursor_x, self.cursor_y = self._saved_cursor
                return
            if byte == ord("c"):
                self._reset()
                return
            if byte == ord("D"):  # IND
                self._index()
                return
            if byte == ord("M"):  # RI
                self._reverse_index()
                return
            if byte == ord("E"):  # NEL
                self.cursor_x = 0
                self._index()
                return
            if byte == ord("(") or byte == ord(")"):
                # charset designate — swallow next (handled loosely)
                self._in_esc = True  # wait, need one more
                self._esc_extra = byte
                return
            return

        if byte == 0x1B:
            self._in_esc = True
            return

        # UTF-8 assembly for printable
        if self._utf8_hold or byte >= 0x80:
            self._utf8_hold.append(byte)
            try:
                ch = self._utf8_hold.decode("utf-8")
            except UnicodeDecodeError as ex:
                if "unexpected end" in str(ex) or len(self._utf8_hold) < 4:
                    return
                ch = "\ufffd"
                self._utf8_hold.clear()
            else:
                self._utf8_hold.clear()
            for c in ch:
                self._put_char(c)
            return

        if byte == 0x07:
            self.bell = True
            return
        if byte == 0x08:  # BS
            self.cursor_x = max(0, self.cursor_x - 1)
            return
        if byte == 0x09:  # TAB
            self.cursor_x = min(self.cols - 1, (self.cursor_x + 8) & ~7)
            return
        if byte == 0x0A:  # LF
            self._index()
            return
        if byte == 0x0B or byte == 0x0C:
            self._index()
            return
        if byte == 0x0D:  # CR
            self.cursor_x = 0
            return
        if byte == 0x0E or byte == 0x0F:
            return
        if byte < 0x20:
            return
        self._put_char(chr(byte))

    def _reset(self):
        self.cursor_x = self.cursor_y = 0
        self._fg = self._bg = None
        self._bold = self._inverse = self._underline = False
        self._scroll_top = 0
        self._scroll_bot = self.rows - 1
        self._using_alt = False
        self._main = self._blank_grid()
        self._alt = self._blank_grid()
        self.cursor_visible = True
        self.mouse_mode = 0

    def _style_cell(self, ch):
        return Cell(
            ch=ch,
            fg=self._fg,
            bg=self._bg,
            bold=self._bold,
            inverse=self._inverse,
            underline=self._underline,
        )

    def _put_char(self, ch):
        if ch == "\x7f":
            return
        # Wide char: treat as single cell for simplicity.
        if self.cursor_x >= self.cols:
            if self._auto_wrap:
                self.cursor_x = 0
                self._index()
            else:
                self.cursor_x = self.cols - 1
        grid = self.grid
        y = self.cursor_y
        x = self.cursor_x
        if 0 <= y < self.rows and 0 <= x < self.cols:
            if self._insert_mode:
                row = grid[y]
                row[x + 1:] = row[x:-1]
            grid[y][x] = self._style_cell(ch)
        self.cursor_x += 1

    def _index(self):
        if self.cursor_y == self._scroll_bot:
            self._scroll_up(1)
        elif self.cursor_y < self.rows - 1:
            self.cursor_y += 1

    def _reverse_index(self):
        if self.cursor_y == self._scroll_top:
            self._scroll_down(1)
        elif self.cursor_y > 0:
            self.cursor_y -= 1

    def _scroll_up(self, n):
        grid = self.grid
        top, bot = self._scroll_top, self._scroll_bot
        n = min(n, bot - top + 1)
        for _ in range(n):
            del grid[top]
            grid.insert(bot, [Cell() for _ in range(self.cols)])

    def _scroll_down(self, n):
        grid = self.grid
        top, bot = self._scroll_top, self._scroll_bot
        n = min(n, bot - top + 1)
        for _ in range(n):
            del grid[bot]
            grid.insert(top, [Cell() for _ in range(self.cols)])

    def take_clipboard(self):
        """Return and clear a pending OSC 52 clipboard write, or None."""
        text = self.pending_clipboard
        self.pending_clipboard = None
        return text

    def _handle_osc(self, data: bytes):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        if text.startswith("0;") or text.startswith("2;"):
            self.title = text.split(";", 1)[-1]
            return
        # OSC 52 ; <pc> ; <base64 data>  — clipboard set from guest (open-grok)
        if text.startswith("52;"):
            try:
                import base64
                rest = text[3:]
                # pc is usually "c" (clipboard); payload after next ';'
                semi = rest.find(";")
                if semi < 0:
                    return
                payload = rest[semi + 1 :].strip()
                if not payload or payload == "?":
                    return  # query / empty
                # Some terminals pad with ST noise; strip whitespace.
                raw = base64.b64decode(payload, validate=False)
                decoded = raw.decode("utf-8", errors="replace")
                if decoded:
                    self.pending_clipboard = decoded
            except Exception:
                return

    def _parse_params(self, body: bytes):
        """Parse CSI parameter string into list of ints (empty -> default)."""
        try:
            s = body.decode("ascii", errors="ignore")
        except Exception:
            return []
        if not s:
            return []
        # private prefix ? or >
        if s[0] in "?!>":
            s = s[1:]
        parts = s.split(";")
        out = []
        for p in parts:
            if p == "":
                out.append(None)
            else:
                try:
                    out.append(int(p))
                except ValueError:
                    out.append(None)
        return out

    def _handle_csi(self, data: bytes):
        # data includes final byte
        if not data:
            return
        final = data[-1]
        body = data[:-1]
        private = b""
        if body and body[0] in (ord("?"), ord(">"), ord("!")):
            private = bytes([body[0]])
            body = body[1:]
        params = self._parse_params(body)
        def p(i, default=1):
            if i >= len(params) or params[i] is None:
                return default
            return params[i]

        ch = chr(final)

        if private == b"?" and ch in ("h", "l"):
            enable = ch == "h"
            for mode in params:
                if mode is None:
                    continue
                if mode == 25:
                    self.cursor_visible = enable
                elif mode == 1049 or mode == 1047 or mode == 47:
                    self._using_alt = enable
                    if enable and mode == 1049:
                        self._alt = self._blank_grid()
                        self.cursor_x = self.cursor_y = 0
                elif mode in (1000, 1002, 1003):
                    if enable:
                        self.mouse_mode |= 1
                    else:
                        self.mouse_mode &= ~1
                elif mode == 1006:
                    if enable:
                        self.mouse_mode |= 2
                    else:
                        self.mouse_mode &= ~2
                elif mode == 7:
                    self._auto_wrap = enable
            return

        if ch == "A":  # CUU
            self.cursor_y = max(self._scroll_top, self.cursor_y - p(0))
        elif ch == "B":  # CUD
            self.cursor_y = min(self._scroll_bot, self.cursor_y + p(0))
        elif ch == "C":  # CUF
            self.cursor_x = min(self.cols - 1, self.cursor_x + p(0))
        elif ch == "D":  # CUB
            self.cursor_x = max(0, self.cursor_x - p(0))
        elif ch == "E":
            self.cursor_y = min(self.rows - 1, self.cursor_y + p(0))
            self.cursor_x = 0
        elif ch == "F":
            self.cursor_y = max(0, self.cursor_y - p(0))
            self.cursor_x = 0
        elif ch == "G":  # CHA
            self.cursor_x = min(self.cols - 1, max(0, p(0) - 1))
        elif ch == "H" or ch == "f":  # CUP
            row = p(0, 1) - 1
            col = p(1, 1) - 1
            self.cursor_y = min(self.rows - 1, max(0, row))
            self.cursor_x = min(self.cols - 1, max(0, col))
        elif ch == "J":  # ED
            mode = p(0, 0)
            grid = self.grid
            if mode == 0:
                for x in range(self.cursor_x, self.cols):
                    grid[self.cursor_y][x] = Cell()
                for y in range(self.cursor_y + 1, self.rows):
                    grid[y] = [Cell() for _ in range(self.cols)]
            elif mode == 1:
                for y in range(0, self.cursor_y):
                    grid[y] = [Cell() for _ in range(self.cols)]
                for x in range(0, self.cursor_x + 1):
                    grid[self.cursor_y][x] = Cell()
            else:
                for y in range(self.rows):
                    grid[y] = [Cell() for _ in range(self.cols)]
        elif ch == "K":  # EL
            mode = p(0, 0)
            grid = self.grid
            row = grid[self.cursor_y]
            if mode == 0:
                for x in range(self.cursor_x, self.cols):
                    row[x] = Cell()
            elif mode == 1:
                for x in range(0, self.cursor_x + 1):
                    row[x] = Cell()
            else:
                for x in range(self.cols):
                    row[x] = Cell()
        elif ch == "L":  # IL
            n = p(0)
            grid = self.grid
            y = self.cursor_y
            for _ in range(n):
                if y <= self._scroll_bot:
                    del grid[self._scroll_bot]
                    grid.insert(y, [Cell() for _ in range(self.cols)])
        elif ch == "M":  # DL
            n = p(0)
            grid = self.grid
            y = self.cursor_y
            for _ in range(n):
                if y <= self._scroll_bot:
                    del grid[y]
                    grid.insert(self._scroll_bot, [Cell() for _ in range(self.cols)])
        elif ch == "P":  # DCH
            n = p(0)
            row = self.grid[self.cursor_y]
            x = self.cursor_x
            for _ in range(n):
                if x < self.cols:
                    del row[x]
                    row.append(Cell())
        elif ch == "X":  # ECH
            n = p(0)
            row = self.grid[self.cursor_y]
            for x in range(self.cursor_x, min(self.cols, self.cursor_x + n)):
                row[x] = Cell()
        elif ch == "S":  # SU
            self._scroll_up(p(0))
        elif ch == "T":  # SD
            self._scroll_down(p(0))
        elif ch == "r":  # DECSTBM
            top = p(0, 1) - 1
            bot = p(1, self.rows) - 1
            self._scroll_top = max(0, min(top, self.rows - 1))
            self._scroll_bot = max(self._scroll_top, min(bot, self.rows - 1))
            self.cursor_x = 0
            self.cursor_y = self._scroll_top
        elif ch == "m":  # SGR
            self._sgr(params if params else [0])
        elif ch == "n":  # DSR — ignore (no reply path here yet)
            pass
        elif ch == "s":
            self._saved_cursor = (self.cursor_x, self.cursor_y)
        elif ch == "u":
            self.cursor_x, self.cursor_y = self._saved_cursor
        elif ch == "h" and not private:
            if p(0, 0) == 4:
                self._insert_mode = True
        elif ch == "l" and not private:
            if p(0, 0) == 4:
                self._insert_mode = False
        elif ch == "d":  # VPA
            self.cursor_y = min(self.rows - 1, max(0, p(0) - 1))
        elif ch == "@":  # ICH
            n = p(0)
            row = self.grid[self.cursor_y]
            x = self.cursor_x
            for _ in range(n):
                row.insert(x, Cell())
                if len(row) > self.cols:
                    row.pop()

    def _sgr(self, params):
        if not params:
            params = [0]
        i = 0
        while i < len(params):
            p = params[i]
            if p is None:
                p = 0
            if p == 0:
                self._fg = self._bg = None
                self._bold = self._inverse = self._underline = False
            elif p == 1:
                self._bold = True
            elif p == 2:
                pass
            elif p == 3:
                pass
            elif p == 4:
                self._underline = True
            elif p == 7:
                self._inverse = True
            elif p == 22:
                self._bold = False
            elif p == 24:
                self._underline = False
            elif p == 27:
                self._inverse = False
            elif 30 <= p <= 37:
                self._fg = _ANSI16[p - 30]
            elif p == 38:
                if i + 1 < len(params) and params[i + 1] == 5 and i + 2 < len(params):
                    self._fg = _color_256(
                        0 if params[i + 2] is None else params[i + 2])
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2 and i + 4 < len(params):
                    # Use `is None` checks — `or 0` is fine for 0, but be explicit.
                    self._fg = (
                        0 if params[i + 2] is None else int(params[i + 2]),
                        0 if params[i + 3] is None else int(params[i + 3]),
                        0 if params[i + 4] is None else int(params[i + 4]),
                    )
                    i += 4
            elif p == 39:
                self._fg = None
            elif 40 <= p <= 47:
                self._bg = _ANSI16[p - 40]
            elif p == 48:
                if i + 1 < len(params) and params[i + 1] == 5 and i + 2 < len(params):
                    self._bg = _color_256(
                        0 if params[i + 2] is None else params[i + 2])
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2 and i + 4 < len(params):
                    self._bg = (
                        0 if params[i + 2] is None else int(params[i + 2]),
                        0 if params[i + 3] is None else int(params[i + 3]),
                        0 if params[i + 4] is None else int(params[i + 4]),
                    )
                    i += 4
            elif p == 49:
                self._bg = None
            elif 90 <= p <= 97:
                self._fg = _ANSI16[p - 90 + 8]
            elif 100 <= p <= 107:
                self._bg = _ANSI16[p - 100 + 8]
            i += 1

    def cell_colors(self, cell: Cell):
        """Return (fg_rgba, bg_rgba) with defaults applied.

        Open Grok sometimes emits near-black truecolor on a black panel after
        resume; the glyphs are still there (selection works) but invisible.
        On dark backgrounds, grayscale / low-chroma ink is forced light.
        """
        fg = cell.fg if cell.fg is not None else (230, 230, 235)
        bg = cell.bg if cell.bg is not None else (18, 18, 20)
        # Normalize accidental 0–1 floats into 0–255.
        if (max(fg) <= 1.0 and min(fg) >= 0.0
                and max(fg) > 0.0 and max(fg) < 1.0):
            fg = tuple(int(round(c * 255)) for c in fg[:3])
        if (max(bg) <= 1.0 and min(bg) >= 0.0
                and max(bg) > 0.0 and max(bg) < 1.0):
            bg = tuple(int(round(c * 255)) for c in bg[:3])
        if cell.inverse:
            fg, bg = bg, fg
        if cell.bold and cell.fg is not None:
            fg = tuple(min(255, int(c * 1.15)) for c in fg)

        def _lum(rgb):
            return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

        def _chroma(rgb):
            return max(rgb[0], rgb[1], rgb[2]) - min(rgb[0], rgb[1], rgb[2])

        # Dark panel: lift dim / gray text (Open Grok default after resume).
        if _lum(bg) < 140:
            if _chroma(fg) < 40 and _lum(fg) < 160:
                fg = (230, 230, 235)
            elif _lum(fg) < 90:
                fg = tuple(min(255, int(c * 2.2 + 50)) for c in fg[:3])
        elif abs(_lum(fg) - _lum(bg)) < 48:
            fg = (28, 28, 32) if _lum(bg) >= 140 else (230, 230, 235)
        return _norm_rgb(fg), _norm_rgb(bg)

    def snapshot_lines(self):
        """List of list of Cell for drawing."""
        return self.grid
