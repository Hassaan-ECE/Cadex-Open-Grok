# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Blender UI for the embedded Open Grok terminal (CADEX_CHAT).

Draws a ConPTY-backed VT screen into the chat editor with ``SpaceCadexChat``
draw handlers, and a modal operator that forwards keyboard (and basic mouse)
input into the PTY.
"""

from __future__ import annotations

import time

import bpy
from bpy.types import Operator, Panel

from . import terminal_session


# Base point size at zoom=1.0; cell size is measured from a real mono face.
_BASE_FONT = 13.0
# Tight padding so the TUI uses nearly the full chat section.
_PAD = 2
# Font zoom: Ctrl+= / Ctrl+- (and numpad), also Ctrl+wheel.
# Also used for the Save-project gate UI so chat chrome scales with the TUI.
_zoom = 1.0
_ZOOM_MIN = 0.7
_ZOOM_MAX = 2.8
_ZOOM_STEP = 0.1

# Cached mono font id (-2 = not tried, -1/0 = fallback UI font).
_mono_font_id = -2
# (zoom_key, font_size, cell_w, cell_h, font_id)
_metrics_cache = None

_draw_handle = None
_draw_handle_execute = None
_timer_registered = False

_DRAW_HANDLE_KEY = "mesh_agent.terminal_ui.draw_handle"
_DRAW_GENERATION_KEY = "mesh_agent.terminal_ui.draw_generation"

_MONO_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\cascadiamono.ttf",
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\lucon.ttf",
    r"C:\Windows\Fonts\cour.ttf",
    r"/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    r"/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    r"/System/Library/Fonts/Menlo.ttc",
    r"/System/Library/Fonts/Monaco.ttf",
)


def _mono_font_candidates():
    """Yield the bundled Blender mono face before system fallbacks."""
    import os

    seen = set()
    try:
        local_root = bpy.utils.resource_path('LOCAL')
    except Exception:
        local_root = ""
    candidates = []
    if local_root:
        candidates.append(os.path.join(
            local_root, "datafiles", "fonts", "DejaVuSansMono.woff2"))
    candidates.extend(_MONO_FONT_CANDIDATES)
    for path in candidates:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        yield path


def _reset_blf_state(blf, font_id):
    """Reset persistent per-font flags that can clip terminal glyphs away."""
    for flag_name in (
            "CLIPPING", "ROTATION", "SHADOW", "WORD_WRAP",
            "NO_FALLBACK", "MONOCHROME"):
        flag = getattr(blf, flag_name, None)
        if flag is None:
            continue
        try:
            blf.disable(font_id, flag)
        except Exception:
            pass
    try:
        blf.aspect(font_id, 1.0)
    except Exception:
        pass
    try:
        blf.rotation(font_id, 0.0)
    except Exception:
        pass


def _ensure_mono_font():
    """Load a true monospace face so glyph advance matches the cell grid."""
    global _mono_font_id
    if _mono_font_id != -2:
        return max(0, _mono_font_id)
    import os
    import blf

    for path in _mono_font_candidates():
        if not os.path.isfile(path):
            continue
        try:
            fid = blf.load(path)
        except Exception:
            continue
        if fid is not None and int(fid) >= 0:
            _mono_font_id = int(fid)
            return _mono_font_id
    # Last resort: default UI font (may still drift on some platforms).
    _mono_font_id = 0
    return 0


def _metrics():
    """Return (font_size, cell_w, cell_h, font_id) measured for current zoom."""
    global _metrics_cache
    import blf

    font_id = _ensure_mono_font()
    font_size = max(9, int(round(_BASE_FONT * _zoom)))
    key = (_zoom, font_id, font_size)
    if _metrics_cache is not None and _metrics_cache[0] == key:
        # Cache: (key, font_size, cell_w, cell_h, font_id, descender)
        return _metrics_cache[1], _metrics_cache[2], _metrics_cache[3], _metrics_cache[4]

    _reset_blf_state(blf, font_id)
    blf.size(font_id, font_size)
    # Measure advance + full glyph box including descenders (g/y/p/q/j).
    widths = []
    heights = []
    for ch in ("M", "W", "0", "i", " ", "g", "y", "p", "q", "j", "Q", "|"):
        try:
            w, h = blf.dimensions(font_id, ch)
        except Exception:
            w, h = font_size * 0.6, font_size
        widths.append(float(w))
        heights.append(float(h))
    try:
        _w_caps, h_caps = blf.dimensions(font_id, "HM")
        _w_desc, h_desc = blf.dimensions(font_id, "gypq")
    except Exception:
        h_caps = float(font_size)
        h_desc = float(font_size) * 1.25
    cell_w = max(max(widths), 1.0)
    # Full line box: room for caps + descenders under the baseline.
    glyph_h = max(max(heights), float(h_caps), float(h_desc), float(font_size))
    # Descender depth ≈ how much "gypq" sticks below a caps line.
    descender = max(2.0, float(h_desc) - float(h_caps) * 0.72)
    descender = max(descender, float(font_size) * 0.28)
    cell_h = max(glyph_h + 2.0, float(font_size) + descender + 2.0, 1.0)
    cell_w = float(max(1, int(round(cell_w))))
    cell_h = float(max(1, int(round(cell_h))))
    # Stash descender so the draw path can place the baseline correctly.
    _metrics_cache = (key, font_size, cell_w, cell_h, font_id, descender)
    return font_size, cell_w, cell_h, font_id


def _metrics_descender():
    """Pixels of room below the baseline inside one cell (for g/y/p/q)."""
    global _metrics_cache
    _metrics()
    if _metrics_cache is not None and len(_metrics_cache) >= 6:
        return float(_metrics_cache[5])
    return max(2.0, _BASE_FONT * _zoom * 0.28)


def _set_zoom(factor):
    global _zoom, _metrics_cache
    _zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, float(factor)))
    _metrics_cache = None
    return _zoom


def _bump_zoom(delta_steps):
    return _set_zoom(_zoom + delta_steps * _ZOOM_STEP)


def _estimate_grid(region):
    if region is None:
        return 100, 32
    _fs, cell_w, cell_h, _fid = _metrics()
    cols = max(40, int((region.width - 2 * _PAD) / cell_w))
    rows = max(8, int((region.height - 2 * _PAD) / cell_h))
    return cols, rows


def _chat_area_regions(area):
    """WINDOW + optional EXECUTE under a CADEX_CHAT area."""
    window = execute = None
    for region in area.regions:
        if region.type == 'WINDOW':
            window = region
        elif region.type == 'EXECUTE':
            execute = region
    return window, execute


def _estimate_grid_for_area(area):
    """Cols/rows for the terminal WINDOW only (footer is Cadex chrome)."""
    if area is None:
        return 100, 32
    window, _execute = _chat_area_regions(area)
    if window is None:
        return 100, 32
    return _estimate_grid(window)


def _fill_region(shader, batch_for_shader, region, color):
    batch = batch_for_shader(
        shader, 'TRIS',
        {"pos": [
            (0, 0), (region.width, 0), (region.width, region.height),
            (0, 0), (region.width, region.height), (0, region.height),
        ]},
    )
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_message_panel(region, title, lines, accent=(0.47, 0.78, 1.0, 1.0)):
    """Centered status card when Open Grok is not painting yet."""
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    _fill_region(shader, batch_for_shader, region, (0.07, 0.07, 0.08, 1.0))
    font_id = 0
    y = region.height * 0.55
    _reset_blf_state(blf, font_id)
    blf.size(font_id, 16)
    blf.color(font_id, *accent)
    blf.position(font_id, _PAD + 16, y, 0)
    blf.draw(font_id, title)
    blf.size(font_id, 12)
    blf.color(font_id, 0.65, 0.65, 0.7, 1.0)
    y -= 28
    for line in lines:
        blf.position(font_id, _PAD + 16, y, 0)
        blf.draw(font_id, line)
        y -= 18
    gpu.state.blend_set('NONE')


# Hit-test rect for the centered Save project button (region space).
_save_gate_button = None  # (x0, y0, x1, y1) or None


def _draw_save_gate(region):
    """Centered Save project call-to-action when the .blend is unsaved.

    Open Grok only starts after the project has a real home on disk.
    Scales with the same Ctrl+/− / wheel zoom as the terminal.
    """
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    global _save_gate_button

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    _fill_region(shader, batch_for_shader, region, (0.07, 0.07, 0.08, 1.0))

    z = max(0.7, float(_zoom))
    title_sz = max(22, int(round(28 * z)))
    body_sz = max(14, int(round(17 * z)))
    btn_sz = max(16, int(round(20 * z)))
    hint_sz = max(12, int(round(13 * z)))
    gap = max(8, int(round(12 * z)))

    font_id = 0
    cx = region.width * 0.5
    cy = region.height * 0.55
    _reset_blf_state(blf, font_id)

    # Title
    title = "Save this project to use Cadex Chat"
    blf.size(font_id, title_sz)
    try:
        tw, th = blf.dimensions(font_id, title)
    except Exception:
        tw, th = len(title) * (title_sz * 0.55), title_sz
    blf.color(font_id, 0.92, 0.93, 0.95, 1.0)
    title_y = cy + th + gap * 2 + body_sz * 2
    blf.position(font_id, cx - tw * 0.5, title_y, 0)
    blf.draw(font_id, title)

    # Subtitle
    blf.size(font_id, body_sz)
    lines = (
        "Model, chat, and history live next to the file you choose.",
        "After you save, the Open Grok terminal opens here.",
    )
    y = title_y - th - gap
    for line in lines:
        try:
            lw, lh = blf.dimensions(font_id, line)
        except Exception:
            lw, lh = len(line) * (body_sz * 0.5), body_sz
        blf.color(font_id, 0.58, 0.59, 0.64, 1.0)
        blf.position(font_id, cx - lw * 0.5, y, 0)
        blf.draw(font_id, line)
        y -= lh + max(4, int(round(6 * z)))

    # Big centered button
    label = "Save project"
    blf.size(font_id, btn_sz)
    try:
        lw, lh = blf.dimensions(font_id, label)
    except Exception:
        lw, lh = 140, btn_sz
    pad_x = max(28, int(round(40 * z)))
    pad_y = max(14, int(round(18 * z)))
    bw = max(int(round(220 * z)), lw + pad_x * 2)
    bh = max(int(round(52 * z)), lh + pad_y * 2)
    x0 = cx - bw * 0.5
    y0 = y - gap * 2 - bh
    x1 = x0 + bw
    y1 = y0 + bh
    _save_gate_button = (x0, y0, x1, y1)

    batch = batch_for_shader(
        shader, 'TRIS',
        {"pos": [
            (x0, y0), (x1, y0), (x1, y1),
            (x0, y0), (x1, y1), (x0, y1),
        ]},
    )
    shader.bind()
    shader.uniform_float("color", (0.22, 0.48, 0.92, 1.0))
    batch.draw(shader)

    blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
    blf.position(font_id, cx - lw * 0.5, y0 + (bh - lh) * 0.5, 0)
    blf.draw(font_id, label)

    blf.size(font_id, hint_sz)
    hint = "File → Save As…  ·  Enter  ·  Ctrl + / − or wheel to zoom"
    try:
        hw, hh = blf.dimensions(font_id, hint)
    except Exception:
        hw, hh = len(hint) * (hint_sz * 0.5), hint_sz
    blf.color(font_id, 0.42, 0.44, 0.48, 1.0)
    blf.position(font_id, cx - hw * 0.5, y0 - gap - hh, 0)
    blf.draw(font_id, hint)

    gpu.state.blend_set('NONE')


def _save_gate_hit(region, event):
    """True if the click is on the centered Save project button."""
    if _save_gate_button is None or region is None:
        return False
    x0, y0, x1, y1 = _save_gate_button
    # Event is window coords; region-local for the button.
    lx = event.mouse_x - region.x
    ly = event.mouse_y - region.y
    return x0 <= lx <= x1 and y0 <= ly <= y1


def _draw_starting(region):
    err = terminal_session.last_start_error()
    if err:
        _draw_message_panel(
            region,
            "Could not start Open Grok",
            [err[:90], "Check open-grok is installed and you are logged in."],
            accent=(0.95, 0.55, 0.4, 1.0),
        )
        return
    _draw_message_panel(
        region,
        "Starting Open Grok…",
        ["Chat history for this model will resume when available."],
    )


def _draw_terminal():
    """POST_PIXEL: paint Open Grok into the WINDOW region only.

    The EXECUTE footer is Cadex chrome (section toggles), not more terminal.
    """
    import blf
    import gpu
    from gpu_extras.batch import batch_for_shader

    context = bpy.context
    region = getattr(context, "region", None)
    if region is None or region.type != 'WINDOW':
        return
    area = getattr(context, "area", None)
    if area is None or area.type != 'CADEX_CHAT':
        return

    font_size, cell_w, cell_h, font_id = _metrics()
    cols, rows = _estimate_grid(region)

    session = terminal_session.get_session()
    if session is None:
        if not terminal_session.project_home_ready():
            _draw_save_gate(region)
        else:
            _draw_starting(region)
        return

    if session.alive():
        # Debounced resize — avoids thrashing ConPTY while the layout settles.
        session.request_resize(cols, rows)
        session.apply_due_resize()
        screen = session.screen
    else:
        _draw_idle(region, cols, rows)
        return

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    bg = (0.07, 0.07, 0.08, 1.0)
    _fill_region(shader, batch_for_shader, region, bg)

    grid = screen.snapshot_lines()
    origin_x = float(_PAD)
    # Integer pixel rows so adjacent cell backgrounds share edges (no hairlines).
    top_px = int(region.height - _PAD)
    # blf.position y is the baseline; leave room below it for g/y/p/q/j.
    descender_px = max(2, int(round(_metrics_descender())))
    # Keep a little air above the caps so the baseline sits in the cell.
    baseline_from_bot = min(descender_px + 1, max(2, int(cell_h) - 3))

    # Two passes: all backgrounds first, then glyphs. Otherwise the next
    # row's background overpaints descenders that slightly enter its box.
    n_rows = min(rows, len(grid))
    for y in range(n_rows):
        row = grid[y]
        row_top = top_px - int(y * cell_h)
        row_bot = top_px - int((y + 1) * cell_h)
        x = 0
        while x < min(len(row), cols):
            cell = row[x]
            _fg, bgc = screen.cell_colors(cell)
            run_end = x + 1
            while run_end < min(len(row), cols):
                _, bg2 = screen.cell_colors(row[run_end])
                if bg2 != bgc:
                    break
                run_end += 1
            if bgc is not None:
                x0 = origin_x + x * cell_w
                x1 = origin_x + run_end * cell_w
                batch = batch_for_shader(
                    shader, 'TRIS',
                    {"pos": [
                        (x0, row_bot), (x1, row_bot), (x1, row_top),
                        (x0, row_bot), (x1, row_top), (x0, row_top),
                    ]},
                )
                shader.bind()
                shader.uniform_float("color", bgc)
                batch.draw(shader)
            x = run_end if run_end > x else x + 1

    # Critical: after gpu batch draws, unbind the shader. Leaving UNIFORM_COLOR
    # bound makes blf.draw paint invisible/black text on some Blender builds
    # (glyphs are still “there” for selection, but you cannot see them).
    try:
        gpu.shader.unbind()
    except Exception:
        pass
    gpu.state.blend_set('ALPHA')
    _reset_blf_state(blf, font_id)
    blf.size(font_id, font_size)

    for y in range(n_rows):
        row = grid[y]
        row_bot = top_px - int((y + 1) * cell_h)
        # Baseline inside the cell, above row_bot by the descender budget.
        py = float(row_bot + baseline_from_bot)
        x = 0
        while x < min(len(row), cols):
            cell = row[x]
            fg, _bgc = screen.cell_colors(cell)
            t_end = x + 1
            while t_end < min(len(row), cols):
                fg2, _ = screen.cell_colors(row[t_end])
                if fg2 != fg:
                    break
                t_end += 1
            text = "".join(
                (row[i].ch if row[i].ch and row[i].ch != "\x00" else " ")
                for i in range(x, t_end)
            )
            if text.strip():
                r, g, b, a = fg
                # Hard floor on ink brightness so dark truecolor never vanishes.
                if (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.55:
                    r, g, b = 0.92, 0.92, 0.94
                blf.color(font_id, r, g, b, 1.0)
                for i, ch in enumerate(text):
                    if ch == " " or ch == "\x00":
                        continue
                    blf.position(
                        font_id, origin_x + (x + i) * cell_w, py, 0)
                    blf.draw(font_id, ch)
            x = t_end if t_end > x else x + 1

    # Open Grok paints its own selection in the cell grid; no host overlay.

    if screen.cursor_visible:
        try:
            shader.bind()
        except Exception:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            shader.bind()
        cx = origin_x + min(screen.cursor_x, cols - 1) * cell_w
        cy_top = top_px - int(min(screen.cursor_y, rows - 1) * cell_h)
        cy_bot = top_px - int((min(screen.cursor_y, rows - 1) + 1) * cell_h)
        batch = batch_for_shader(
            shader, 'TRIS',
            {"pos": [
                (cx, cy_bot), (cx + cell_w, cy_bot),
                (cx + cell_w, cy_top),
                (cx, cy_bot), (cx + cell_w, cy_top),
                (cx, cy_top),
            ]},
        )
        shader.uniform_float("color", (0.85, 0.85, 0.9, 0.35))
        batch.draw(shader)
        try:
            gpu.shader.unbind()
        except Exception:
            pass

    gpu.state.blend_set('NONE')


def _draw_idle(region, cols, rows):
    _draw_message_panel(
        region,
        "Open Grok exited",
        [
            "Press Restart in the chat header to bring it back.",
            "Restart resumes this project's chat when possible.",
        ],
        accent=(0.85, 0.85, 0.88, 1.0),
    )


# ---------------------------------------------------------------------------
# Key → PTY bytes
# ---------------------------------------------------------------------------

_SPECIAL = {
    'RET': b'\r',
    'SPACE': b' ',
    'TAB': b'\t',
    'BACK_SPACE': b'\x7f',
    'DEL': b'\x1b[3~',
    'ESC': b'\x1b',
    'LEFT_ARROW': b'\x1b[D',
    'RIGHT_ARROW': b'\x1b[C',
    'UP_ARROW': b'\x1b[A',
    'DOWN_ARROW': b'\x1b[B',
    'HOME': b'\x1b[H',
    'END': b'\x1b[F',
    'PAGE_UP': b'\x1b[5~',
    'PAGE_DOWN': b'\x1b[6~',
    'INSERT': b'\x1b[2~',
}

for i in range(1, 13):
    # xterm F1-F12
    seqs = {
        1: b'\x1bOP', 2: b'\x1bOQ', 3: b'\x1bOR', 4: b'\x1bOS',
        5: b'\x1b[15~', 6: b'\x1b[17~', 7: b'\x1b[18~', 8: b'\x1b[19~',
        9: b'\x1b[20~', 10: b'\x1b[21~', 11: b'\x1b[23~', 12: b'\x1b[24~',
    }
    _SPECIAL['F{:d}'.format(i)] = seqs[i]


def _event_to_bytes(event):
    """Map a Blender event to PTY input bytes, or None."""
    if event.value not in {'PRESS', 'CLICK'}:
        # Unicode chars may arrive as PRESS only; still accept.
        if event.value != 'PRESS':
            return None

    # Prefer unicode for printable (handles shift letters).
    uni = getattr(event, "ascii", None) or ""
    if not uni and getattr(event, "unicode", None):
        uni = event.unicode
    # Blender: event.ascii is often empty; use type for specials.

    ctrl = bool(event.ctrl)
    alt = bool(event.alt)

    et = event.type

    if et in _SPECIAL and not (et == 'ESC' and event.shift):
        data = _SPECIAL[et]
        if et == 'TAB' and event.shift:
            return b'\x1b[Z'
        if ctrl and et == 'RET':
            return b'\n'
        return data

    # Ctrl+letter
    if ctrl and len(et) == 1 and et.isalpha():
        return bytes([ord(et.upper()) - ord('A') + 1])

    # Printable via ascii/unicode
    if uni and len(uni) == 1 and ord(uni) >= 32:
        ch = uni
        if alt:
            return b'\x1b' + ch.encode('utf-8')
        return ch.encode('utf-8')

    # Fallback: unshifted letter from type
    if len(et) == 1 and et.isalpha() and not ctrl:
        ch = et.lower() if not event.shift else et.upper()
        if alt:
            return b'\x1b' + ch.encode('ascii')
        return ch.encode('ascii')

    if et in {'ZERO', 'ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX', 'SEVEN',
              'EIGHT', 'NINE'}:
        digits = {
            'ZERO': '0', 'ONE': '1', 'TWO': '2', 'THREE': '3', 'FOUR': '4',
            'FIVE': '5', 'SIX': '6', 'SEVEN': '7', 'EIGHT': '8', 'NINE': '9',
        }
        shift_map = {
            'ZERO': ')', 'ONE': '!', 'TWO': '@', 'THREE': '#', 'FOUR': '$',
            'FIVE': '%', 'SIX': '^', 'SEVEN': '&', 'EIGHT': '*', 'NINE': '(',
        }
        ch = shift_map[et] if event.shift else digits[et]
        return ch.encode('ascii')

    punct = {
        'PERIOD': ('.', '>'), 'COMMA': (',', '<'), 'MINUS': ('-', '_'),
        'EQUAL': ('=', '+'), 'SLASH': ('/', '?'), 'BACK_SLASH': ('\\', '|'),
        'SEMI_COLON': (';', ':'), 'QUOTE': ("'", '"'),
        'LEFT_BRACKET': ('[', '{'), 'RIGHT_BRACKET': (']', '}'),
        'ACCENT_GRAVE': ('`', '~'),
    }
    if et in punct:
        ch = punct[et][1] if event.shift else punct[et][0]
        return ch.encode('ascii')

    return None


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _chat_window_region(context):
    screen = getattr(context, "screen", None)
    if screen is None:
        return None
    for area in screen.areas:
        if area.type != 'CADEX_CHAT':
            continue
        for region in area.regions:
            if region.type == 'WINDOW':
                return region
    return None


def _find_chat_area(context=None):
    if context is not None:
        screen = getattr(context, "screen", None)
        if screen is not None:
            for a in screen.areas:
                if a.type == 'CADEX_CHAT':
                    return a
    try:
        for window in bpy.context.window_manager.windows:
            for a in window.screen.areas:
                if a.type == 'CADEX_CHAT':
                    return a
    except Exception:
        pass
    return None


def _chat_region_ready(area):
    """Wait until the chat WINDOW has a real size before spawning ConPTY."""
    if area is None:
        return False
    window, _execute = _chat_area_regions(area)
    if window is None:
        return False
    return window.width >= 160 and window.height >= 100


def _ensure_terminal(context=None, fresh=False, initial_prompt=None):
    """Start Open Grok if needed using the live chat region size.

    Requires a saved project home. Defers start until the chat area is sized.
    """
    if not terminal_session.project_home_ready():
        return False, (
            "Save the project first. Model, chat, and history live next to "
            "the .blend file you choose."
        )
    area = _find_chat_area(context)
    if not _chat_region_ready(area):
        return False, "waiting for chat layout"
    cols, rows = _estimate_grid_for_area(area)
    return terminal_session.ensure_running(
        cols=cols, rows=rows, fresh=fresh, initial_prompt=initial_prompt)


def _invoke_save_for_chat():
    """Open Save As so the project home (model + chat) can be chosen."""
    try:
        bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')
        return True
    except Exception:
        try:
            bpy.ops.mesh_agent.save_project_for_chat()
            return True
        except Exception:
            return False


class MESH_AGENT_OT_terminal_start(Operator):
    """Internal: ensure the default Open Grok terminal is running."""

    bl_idname = "mesh_agent.terminal_start"
    bl_label = "Open Grok"
    bl_description = "Ensure the embedded Open Grok terminal is running"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        ok, msg = _ensure_terminal(context)
        if not ok:
            if msg != "waiting for chat layout":
                self.report({'WARNING'}, msg)
                return {'CANCELLED'}
            return {'FINISHED'}
        for area in context.screen.areas:
            if area.type == 'CADEX_CHAT':
                area.tag_redraw()
        return {'FINISHED'}


class MESH_AGENT_OT_save_project_for_chat(Operator):
    """Save the .blend so chat/history can live next to the model."""

    bl_idname = "mesh_agent.save_project_for_chat"
    bl_label = "Save project"
    bl_description = (
        "Save this Cadex file. Chat and history will live next to it "
        "under <name>.cadex"
    )

    def execute(self, context):
        # Prefer Save As when unsaved so the user picks the project home.
        try:
            if not terminal_session.blend_is_saved():
                bpy.ops.wm.save_as_mainfile('INVOKE_DEFAULT')
            else:
                bpy.ops.wm.save_mainfile()
        except Exception as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        return {'FINISHED'}


class MESH_AGENT_OT_terminal_stop(Operator):
    bl_idname = "mesh_agent.terminal_stop"
    bl_label = "Stop Terminal"
    bl_description = "Stop the embedded Open Grok process"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        terminal_session.stop()
        for area in context.screen.areas:
            if area.type == 'CADEX_CHAT':
                area.tag_redraw()
        return {'FINISHED'}


class MESH_AGENT_OT_terminal_restart(Operator):
    bl_idname = "mesh_agent.terminal_restart"
    bl_label = "Restart"
    bl_description = (
        "Restart Open Grok for this project (resumes chat when possible)"
    )

    def execute(self, context):
        terminal_session.stop(status="Open Grok terminal restarting.")
        ok, msg = _ensure_terminal(context, fresh=False)
        if not ok:
            if msg != "waiting for chat layout":
                self.report({'ERROR'}, msg)
                return {'CANCELLED'}
            return {'FINISHED'}
        _kick_input_router()
        for area in context.screen.areas:
            if area.type == 'CADEX_CHAT':
                area.tag_redraw()
        return {'FINISHED'}


class MESH_AGENT_OT_terminal_new_chat(Operator):
    bl_idname = "mesh_agent.terminal_new_chat"
    bl_label = "New chat"
    bl_description = (
        "Start a fresh Open Grok conversation for this project "
        "(does not resume the previous session)"
    )

    def execute(self, context):
        if not terminal_session.project_home_ready():
            self.report(
                {'ERROR'},
                "Save the project first so the new chat has a home.",
            )
            return {'CANCELLED'}
        terminal_session.stop(status="Starting a new chat for this project.")
        ok, msg = _ensure_terminal(context, fresh=True)
        if not ok:
            if msg != "waiting for chat layout":
                self.report({'ERROR'}, msg)
                return {'CANCELLED'}
            return {'FINISHED'}
        _kick_input_router()
        for area in context.screen.areas:
            if area.type == 'CADEX_CHAT':
                area.tag_redraw()
        return {'FINISHED'}


_PASS_EVENTS = frozenset({
    'TIMER', 'TIMER0', 'TIMER1',
    'TIMER_REPORT', 'TIMERREGION', 'WINDOW_DEACTIVATE', 'NDOF_MOTION',
    'RIGHTMOUSE', 'MIDDLEMOUSE',
})

# Wheel / trackpad while the terminal owns input — forwarded to the PTY.
_WHEEL_EVENTS = frozenset({
    'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
    'WHEELINMOUSE', 'WHEELOUTMOUSE',
    'TRACKPADPAN', 'TRACKPADZOOM',
})

_input_router_active = False

# Track left-button drag so we can send SGR motion (button+32) to Open Grok.
# Selection/copy is owned by open-grok itself (native clipboard + its TUI).
_mouse_left_down = False
_mouse_last_cell = None  # (col, row) 1-based last report


def _flush_osc_clipboard(context, session):
    """Apply OSC 52 clipboard writes Open Grok may emit (fallback path)."""
    screen = getattr(session, "screen", None)
    if screen is None:
        return
    text = getattr(screen, "take_clipboard", None)
    if not callable(text):
        pending = getattr(screen, "pending_clipboard", None)
        if not pending:
            return
        try:
            context.window_manager.clipboard = pending
        except Exception:
            pass
        try:
            screen.pending_clipboard = None
        except Exception:
            pass
        return
    try:
        value = text()
    except Exception:
        return
    if value:
        try:
            context.window_manager.clipboard = value
        except Exception:
            pass


def _area_under_mouse(context, event):
    screen = getattr(context, "screen", None)
    if screen is None:
        return None
    for area in screen.areas:
        if (area.x <= event.mouse_x < area.x + area.width
                and area.y <= event.mouse_y < area.y + area.height):
            return area
    return None


def _chat_window_under_mouse(context, event):
    """CADEX_CHAT WINDOW region under the cursor, or None."""
    area = _area_under_mouse(context, event)
    if area is None or area.type != 'CADEX_CHAT':
        return None, None
    for region in area.regions:
        if region.type != 'WINDOW':
            continue
        if (region.x <= event.mouse_x < region.x + region.width
                and region.y <= event.mouse_y < region.y + region.height):
            return area, region
    # Click on chat header/footer still counts as selecting the chat editor.
    return area, None


def _cell_under_mouse(region, event):
    """1-based (col, row) under the cursor for mouse reports."""
    _fs, cell_w, cell_h, _fid = _metrics()
    cols, rows = _estimate_grid(region)
    mx = int((event.mouse_x - region.x - _PAD) / cell_w) + 1
    my_from_top = (region.y + region.height) - event.mouse_y - _PAD
    my = int(my_from_top / cell_h) + 1
    mx = max(1, min(cols, mx))
    my = max(1, min(rows, my))
    return mx, my, cols, rows


def _send_mouse_to_pty(session, area, region, event, kind="press"):
    """Forward mouse to Open Grok so *its* selection/copy owns the gesture.

    kind: "press" | "release" | "drag"
    SGR (1006): press 0, drag 32 (button 0 + 32 motion), release via 'm'.
    """
    global _mouse_last_cell
    if region is None or area is None or region.type != 'WINDOW':
        return False
    if not (session.screen.mouse_mode & 1):
        return False
    mx, my, _cols, _rows = _cell_under_mouse(region, event)
    if kind == "drag" and _mouse_last_cell == (mx, my):
        return False
    _mouse_last_cell = (mx, my)

    if session.screen.mouse_mode & 2:
        # SGR mouse (DECSET 1006) — what ratatui / open-grok expect.
        if kind == "release":
            session.write(
                "\x1b[<0;{:d};{:d}m".format(mx, my).encode("ascii"))
        elif kind == "drag":
            # Button 0 held + motion bit 32.
            session.write(
                "\x1b[<32;{:d};{:d}M".format(mx, my).encode("ascii"))
        else:
            session.write(
                "\x1b[<0;{:d};{:d}M".format(mx, my).encode("ascii"))
        return True

    # X10-style (press only is meaningful; release/drag best-effort).
    if kind == "release":
        b = 32 + 3  # button release
    elif kind == "drag":
        b = 32 + 32  # motion + button 0
    else:
        b = 32  # button 0 press
    bx = min(223, mx) + 32
    by = min(223, my) + 32
    session.write(bytes([0x1B, ord('['), ord('M'), b, bx, by]))
    return True


def _send_wheel_to_pty(session, area, region, event):
    """Forward scroll to Open Grok (SGR mouse wheel or Page Up/Down)."""
    if region is None or region.type != 'WINDOW':
        return False
    et = event.type
    # TRACKPADPAN: use mouse_y delta if present; treat positive as up.
    if et == 'TRACKPADPAN':
        dy = float(getattr(event, "mouse_prev_y", 0) - event.mouse_y)
        if abs(dy) < 1.0:
            # Some builds use mouse_y relative; fall back to prev_x unused.
            dy = float(getattr(event, "mouse_y", 0))
        if dy == 0:
            return False
        up = dy > 0
    elif et in {'WHEELUPMOUSE', 'WHEELINMOUSE'}:
        up = True
    elif et in {'WHEELDOWNMOUSE', 'WHEELOUTMOUSE'}:
        up = False
    elif et == 'TRACKPADZOOM':
        return False
    else:
        return False

    mx, my, _c, _r = _cell_under_mouse(region, event)
    # X11 button 4 = wheel up (64 in SGR with motion), 5 = wheel down (65).
    # Open Grok / ratatui expect these for transcript scroll.
    if session.screen.mouse_mode & 1:
        btn = 64 if up else 65
        if session.screen.mouse_mode & 2:
            session.write(
                "\x1b[<{:d};{:d};{:d}M".format(btn, mx, my).encode("ascii"))
        else:
            # X10: button code is 32 + button (4 or 5 → 36/37) …
            b = 32 + (4 if up else 5)
            bx = min(223, mx) + 32
            by = min(223, my) + 32
            session.write(bytes([0x1B, ord('['), ord('M'), b, bx, by]))
        return True
    # No mouse tracking: keyboard scroll fallback.
    session.write(b'\x1b[5~' if up else b'\x1b[6~')
    return True


def _is_zoom_event(event, allow_plain_wheel=False):
    """Ctrl+= / Ctrl+- / Ctrl+numpad / Ctrl+wheel → font zoom.

    On the Save gate (no TUI scroll), plain wheel also zooms when
    ``allow_plain_wheel`` is true.
    """
    et = event.type
    wheel_up = et in {'WHEELUPMOUSE', 'WHEELINMOUSE'}
    wheel_down = et in {'WHEELDOWNMOUSE', 'WHEELOUTMOUSE'}
    if allow_plain_wheel and event.value in {'PRESS', 'NOTHING', 'ANY'}:
        if wheel_up:
            return 1
        if wheel_down:
            return -1
    if not event.ctrl or event.value != 'PRESS':
        return 0
    if et in {'EQUAL', 'NUMPAD_PLUS'} or wheel_up:
        return 1
    if et in {'MINUS', 'NUMPAD_MINUS'} or wheel_down:
        return -1
    # Some keyboards emit PLUS for the + key with shift; still honor Ctrl.
    if et == 'PLUS':
        return 1
    return 0


class MESH_AGENT_OT_terminal_input(Operator):
    """Always-on keyboard router for the Cadex chat terminal.

    Behaves like any other editor section:
    - Click the chat panel → keys go to Open Grok
    - Click the viewport (or anywhere else) → keys go back to Cadex
    No separate Focus button and no Esc-to-release.
    """

    bl_idname = "mesh_agent.terminal_input"
    bl_label = "Terminal Input"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _input_router_active, _mouse_left_down, _mouse_last_cell
        if _input_router_active:
            return {'CANCELLED'}
        _input_router_active = True
        # Chat owns keys once the user has activated that editor section.
        self._chat_owns_keys = False
        _mouse_left_down = False
        _mouse_last_cell = None
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _modal_save_gate(self, context, event):
        """Unsaved project: big Save gate only — no fake input field."""
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            area, region = _chat_window_under_mouse(context, event)
            if area is None:
                hit = _area_under_mouse(context, event)
                if hit is not None and hit.type == 'CADEX_CHAT':
                    area = hit
            self._chat_owns_keys = area is not None
            for a in context.screen.areas:
                if a.type == 'CADEX_CHAT':
                    a.tag_redraw()
            if self._chat_owns_keys and region is not None:
                if _save_gate_hit(region, event):
                    _invoke_save_for_chat()
                    return {'RUNNING_MODAL'}
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        if not self._chat_owns_keys:
            return {'PASS_THROUGH'}

        # Zoom: Ctrl+/−, Ctrl+wheel, or plain wheel (nothing to scroll here).
        zoom_dir = _is_zoom_event(event, allow_plain_wheel=True)
        if zoom_dir:
            _bump_zoom(zoom_dir)
            for a in context.screen.areas:
                if a.type == 'CADEX_CHAT':
                    a.tag_redraw()
            return {'RUNNING_MODAL'}

        if event.value == 'PRESS' and event.type in {'RET', 'NUMPAD_ENTER', 'SPACE'}:
            _invoke_save_for_chat()
            return {'RUNNING_MODAL'}

        if event.type in _PASS_EVENTS or event.type in {
                'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            return {'PASS_THROUGH'}

        # Swallow other keys so they don't hit Blender while the gate is up.
        if event.value in {'PRESS', 'CLICK'}:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def modal(self, context, event):
        global _mouse_left_down, _mouse_last_cell
        session = terminal_session.get_session()

        # --- Unsaved: centered Save project gate (real TUI after save) ------
        if not terminal_session.project_home_ready():
            return self._modal_save_gate(context, event)

        if session is None or not session.alive():
            if session is not None:
                session.focused = False
            # Keep focus flag if user clicked chat; keys ignored until start.
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                area, region = _chat_window_under_mouse(context, event)
                if area is None:
                    hit = _area_under_mouse(context, event)
                    if hit is not None and hit.type == 'CADEX_CHAT':
                        area = hit
                self._chat_owns_keys = area is not None
                for a in context.screen.areas:
                    if a.type == 'CADEX_CHAT':
                        a.tag_redraw()
            # Stay registered; pass everything until open-grok is back.
            return {'PASS_THROUGH'}

        # Open Grok may write clipboard via OSC 52; apply when present.
        _flush_osc_clipboard(context, session)

        # Font zoom is Cadex-side (our blf paint), not open-grok's.
        zoom_dir = _is_zoom_event(event)
        if zoom_dir and self._chat_owns_keys:
            _bump_zoom(zoom_dir)
            for a in context.screen.areas:
                if a.type == 'CADEX_CHAT':
                    a.tag_redraw()
            return {'RUNNING_MODAL'}

        # Scroll history into Open Grok while the chat panel owns input.
        if self._chat_owns_keys and event.type in _WHEEL_EVENTS:
            if event.value not in {'PRESS', 'NOTHING', 'ANY'}:
                # TRACKPADPAN often reports as NOTHING continuously.
                if event.type not in {'TRACKPADPAN', 'TRACKPADZOOM'}:
                    return {'RUNNING_MODAL'}
            area, region = _chat_window_under_mouse(context, event)
            if region is None:
                # Wheel over header still scrolls if chat owns keys.
                hit = _area_under_mouse(context, event)
                if hit is not None and hit.type == 'CADEX_CHAT':
                    for r in hit.regions:
                        if r.type == 'WINDOW':
                            region = r
                            area = hit
                            break
            if region is not None and _send_wheel_to_pty(
                    session, area, region, event):
                for a in context.screen.areas:
                    if a.type == 'CADEX_CHAT':
                        a.tag_redraw()
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        # --- Mouse drag motion → Open Grok (its selection UI) --------------
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            if (_mouse_left_down and self._chat_owns_keys
                    and (session.screen.mouse_mode & 1)):
                area, region = _chat_window_under_mouse(context, event)
                if region is not None:
                    if _send_mouse_to_pty(
                            session, area, region, event, kind="drag"):
                        for a in context.screen.areas:
                            if a.type == 'CADEX_CHAT':
                                a.tag_redraw()
                        return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}

        # --- Mouse: focus section; clicks go to Open Grok -------------------
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            area, region = _chat_window_under_mouse(context, event)
            # Footer/header clicks: still "this editor is active" but do not
            # steal the click from Blender UI chrome.
            if area is None:
                hit = _area_under_mouse(context, event)
                if hit is not None and hit.type == 'CADEX_CHAT':
                    area = hit
                    region = None
            was = self._chat_owns_keys
            self._chat_owns_keys = area is not None
            session.focused = self._chat_owns_keys
            if was != self._chat_owns_keys:
                for a in context.screen.areas:
                    if a.type == 'CADEX_CHAT':
                        a.tag_redraw()
            if self._chat_owns_keys and region is not None:
                _mouse_left_down = True
                _mouse_last_cell = None
                _send_mouse_to_pty(
                    session, area, region, event, kind="press")
                return {'RUNNING_MODAL'}
            if not self._chat_owns_keys:
                _mouse_left_down = False
                _mouse_last_cell = None
            return {'PASS_THROUGH'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if self._chat_owns_keys:
                area, region = _chat_window_under_mouse(context, event)
                if _mouse_left_down:
                    _mouse_left_down = False
                    if area is not None and region is not None:
                        _send_mouse_to_pty(
                            session, area, region, event, kind="release")
                    # Open Grok auto-copies on release; also pick up OSC 52.
                    _flush_osc_clipboard(context, session)
                    for a in context.screen.areas:
                        if a.type == 'CADEX_CHAT':
                            a.tag_redraw()
                    return {'RUNNING_MODAL'}
            _mouse_left_down = False
            return {'PASS_THROUGH'}

        if event.type in _PASS_EVENTS:
            return {'PASS_THROUGH'}

        # --- Keyboard: only when chat is the active section -----------------
        if not self._chat_owns_keys:
            return {'PASS_THROUGH'}

        data = _event_to_bytes(event)
        if data is not None:
            session.write(data)
            return {'RUNNING_MODAL'}

        # Swallow other key presses so G / Tab / etc. don't hit Blender while
        # typing in open-grok (same idea as the Text Editor owning the keymap).
        if event.value in {'PRESS', 'CLICK'}:
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        global _input_router_active, _mouse_left_down, _mouse_last_cell
        _input_router_active = False
        _mouse_left_down = False
        _mouse_last_cell = None
        session = terminal_session.get_session()
        if session is not None:
            session.focused = False


# Keep old idname working for any keymaps / scripts that still call Focus.
class MESH_AGENT_OT_terminal_focus(Operator):
    bl_idname = "mesh_agent.terminal_focus"
    bl_label = "Focus Chat"
    bl_description = "Activate the Cadex chat terminal (same as clicking it)"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        _kick_input_router()
        session = terminal_session.get_session()
        if session is not None and session.alive():
            session.focused = True
            # Mark ownership as if the user clicked chat (no event available).
            # The input modal will learn on the next real click; set a flag
            # via a soft kick by simulating ownership on the running op is
            # not accessible — so just ensure router is up.
        return {'FINISHED'}


def _kick_input_router():
    """Ensure the always-on input modal is running (one per session)."""
    global _input_router_active
    if _input_router_active or bpy.app.background:
        return
    try:
        window = bpy.context.window
        if window is None:
            return
        with bpy.context.temp_override(window=window):
            bpy.ops.mesh_agent.terminal_input('INVOKE_DEFAULT')
    except Exception:
        pass


def _auto_start_timer():
    """Keep Open Grok running and the input router attached."""
    if bpy.app.background:
        return None
    try:
        has_chat = False
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'CADEX_CHAT':
                    has_chat = True
                    break
            if has_chat:
                break
        if not has_chat:
            return 2.0
        # Hide any leftover EXECUTE footer from older layouts (header owns chrome).
        _hide_execute_regions()
        if not terminal_session.project_home_ready():
            # Unsaved: Save gate only; real Open Grok after the file has a home.
            _kick_input_router()
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'CADEX_CHAT':
                        area.tag_redraw()
            return 1.0
        if not terminal_session.is_running():
            prompt = terminal_session.peek_pending_launch_prompt() or None
            ok, msg = _ensure_terminal(initial_prompt=prompt)
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'CADEX_CHAT':
                        area.tag_redraw()
            # Retry sooner while waiting for layout after file open/save.
            return 0.25 if msg == "waiting for chat layout" else 0.6
        # Poll often while the TUI may still be empty (first paint).
        terminal_session.poll_and_redraw()
        _kick_input_router()
        session = terminal_session.get_session()
        if (session is not None and session.alive()
                and not session.screen_has_content()
                and (time.time() - session.started_at) < 8.0):
            return 0.15
    except Exception:
        pass
    return 1.0


def _hide_execute_regions():
    """Best-effort: collapse EXECUTE so the terminal fills the section."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'CADEX_CHAT':
                    continue
                for region in area.regions:
                    if region.type != 'EXECUTE':
                        continue
                    # RNA rarely exposes hide; tag for redraw after C unhide fixes.
                    try:
                        area.tag_redraw()
                    except Exception:
                        pass
    except Exception:
        pass


classes = (
    MESH_AGENT_OT_terminal_start,
    MESH_AGENT_OT_save_project_for_chat,
    MESH_AGENT_OT_terminal_stop,
    MESH_AGENT_OT_terminal_restart,
    MESH_AGENT_OT_terminal_new_chat,
    MESH_AGENT_OT_terminal_input,
    MESH_AGENT_OT_terminal_focus,
)


def _draw_terminal_dispatch(generation):
    """Call the current renderer and ignore stale handlers after reload."""
    if bpy.app.driver_namespace.get(_DRAW_GENERATION_KEY) != generation:
        return
    _draw_terminal()


def _remove_draw_handle(handle):
    if handle is None:
        return
    try:
        bpy.types.SpaceCadexChat.draw_handler_remove(handle, 'WINDOW')
    except Exception:
        pass


def register():
    global _draw_handle, _draw_handle_execute
    for cls in classes:
        bpy.utils.register_class(cls)
    namespace = bpy.app.driver_namespace
    stored_handle = namespace.pop(_DRAW_HANDLE_KEY, None)
    _remove_draw_handle(stored_handle)
    if _draw_handle is not stored_handle:
        _remove_draw_handle(_draw_handle)
    generation = int(namespace.get(_DRAW_GENERATION_KEY, 0)) + 1
    namespace[_DRAW_GENERATION_KEY] = generation
    try:
        _draw_handle = bpy.types.SpaceCadexChat.draw_handler_add(
            _draw_terminal_dispatch, (generation,), 'WINDOW', 'POST_PIXEL')
        namespace[_DRAW_HANDLE_KEY] = _draw_handle
    except Exception:
        _draw_handle = None
    _draw_handle_execute = None
    if not bpy.app.background:
        if not bpy.app.timers.is_registered(_auto_start_timer):
            bpy.app.timers.register(_auto_start_timer, first_interval=0.4)


def unregister():
    global _draw_handle, _draw_handle_execute
    if bpy.app.timers.is_registered(_auto_start_timer):
        bpy.app.timers.unregister(_auto_start_timer)
    terminal_session.stop(persist=False)
    namespace = bpy.app.driver_namespace
    namespace[_DRAW_GENERATION_KEY] = int(
        namespace.get(_DRAW_GENERATION_KEY, 0)) + 1
    stored_handle = namespace.pop(_DRAW_HANDLE_KEY, None)
    _remove_draw_handle(stored_handle)
    if _draw_handle is not stored_handle:
        _remove_draw_handle(_draw_handle)
    _draw_handle = None
    _draw_handle_execute = None
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
