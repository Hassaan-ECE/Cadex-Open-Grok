# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Live Open Grok session inside Cadex: ConPTY + VT screen + mesh MCP.

Conversation continuity is owned by Open Grok's project-scoped session store.
Cadex pins the terminal cwd to the saved blend's project home
(``<stem>.cadex/.cadex_terminal``) so model + chat + history share one path.
Unsaved files do not start a temp TUI — the user must save first.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from urllib.parse import quote

from . import agent as agent_module
from . import modes
from .open_grok_backend import (
    _DISALLOWED_BUILTINS,
    _toml_escape,
    find_open_grok,
)


# Process-level singleton state for the embedded terminal.
_lock = threading.Lock()
_session = None  # type: TerminalSession | None

_SESSION_ID_FILE = "cadex_last_session_id.txt"
_RESIZE_DEBOUNCE_S = 0.12
_EMPTY_RECOVER_S = 4.0

# Cadex-branded project rules / agent context for open-grok.
_CADEX_AGENTS_MD = """\
# Cadex

You are inside **Cadex** — AI-native parametric CAD.

## This session
- Mesh MCP tools talk to the **live** Cadex engine and viewport.
- Units: **millimeters**, +Z up.
- Call `describe_cad_api` before the first `write_script`.
- Prefer mesh tools over shell or free-form file edits for geometry.

## How to work with the user
- They type design requests in this panel (same as chatting with Cadex).
- After a successful build, offer a short next-step (params, fillets, assembly).
- Keep replies tight; the viewport is the proof.

## Examples
- "make a 20mm cube"
- "NEMA 17 mount plate, M3 slots"
- "fillet the top edges 1mm"
- "hinged arm that falls under gravity" (dynamics)
- "mechanism that swings and hits the floor" (dynamics + contact)

## Mechanisms / dynamics (when asked) — go fast

Cadex can run rigid-body dynamics (MuJoCo). Prefer a **short path** over
research loops.

### Fail-fast order (do not skip)
1. **One targeted lookup**, not a domain dump:
   `describe_cad_api(domain="assembly", operation="dynamics")` and
   `operation="body"` / `"collision"` only if the args are unclear.
2. **Prove motion on a tiny model first** (≤ 4 free bodies, or a 1-joint
   hinge). Prefer a **visible free fall**: free bodies start ≥ 1× body
   height above the floor (or a swinging hinge), `end_time_s` ≥ 1.
   Do **not** ship a 50+ body pile until the small model accepts and moves.
3. **One `write_script`** with the known recipe (see agent profile). Avoid
   thrashing `edit_script` / `restore_version` after publication errors.
4. Tell the user: open **Params → Simulation → Play**. If motion is only a
   few mm of settle, that is a **failed demo** — raise the drop and rewrite.

### Hard contracts (common rejects)
- Fixed base: `component(..., grounded=True)` — never `fixed=True`.
- Connectors: `connector(comp, sel, offset=…)` only. Put axis/angle/position
  **inside** `offset` (list or map). Never top-level `axis=` on connector.
- **Offsets are component-local, never scene XYZ.** Connector and collision
  `offset` positions are mm in that part’s frame. Use local limb solids +
  `component(..., placement=[wx,wy,wz])` for world pose. World coords in
  offsets → wrong hinges and MJCF `body_pos` drift (~1.0) failures.
- Assembly programs need **exactly one** `assembly` + **one**
  `assembly.solve` diagnostics in `result`, plus the dynamics sim when asked.
- **Return every component and every joint** that you passed into
  `assembly.assembly([...], [...])` **exactly once** in `result`. Omitting
  them is a hard reject (even if assembly+solve+sim are present). "Lean"
  means no *extra* unlisted component/joint outputs, not "skip them".
- **Every** component needs one `assembly.body` (density required:
  steel ≈ 7850, aluminium ≈ 2700, ABS ≈ 1040).
- Contact is **opt-in** via `collision=` on bodies. No collision ⇒ pass-through.
- Free-body piles: `assembly.solve(..., require_solved=False)` is normal.
- `PUBLICATION_UNTAGGED_OBJECT` / foreign `Joints`/`Simulations`: **stop
  looping**. Ask the user to close & reopen the project once, then one clean
  `write_script`. Do not spam `restore_version`.

### Visibility bar (human can test it)
A dynamics reply is incomplete unless the sim would show **obvious** motion
in Play (order of centimeters drop, or clear hinge swing) — not a 2 mm settle.

## Tool budget (efficiency)
- After a **successful** write/edit: at most **one** verify
  (`scene_summary` preferred). Never inspect 4+ times or always pair
  focus + screenshot + multi-inspect.
- `describe_cad_api`: max **2 per user message**; use domain+operation.
  Do not dump the full assembly domain repeatedly.
- Do not inspect fake names (`sim`, intermediate `*_component` handles).
  Use solid result output names or scene_summary.
- Prefer `edit_script` for small tweaks; `set_params` when only `num()` values
  change; full `write_script` for new mechanisms.
- Colors / plastic / metal = viewport paint only — no FreeCAD materials.
- Screenshots only if asked or geometry is ambiguous.
"""


def get_session():
    return _session


def is_running():
    s = _session
    return s is not None and s.alive()


def status_text():
    s = _session
    if s is None:
        if not project_home_ready():
            return "save project first"
        return "starting…"
    if s.alive():
        return "{}×{}".format(s.cols, s.rows)
    if getattr(s, "error", ""):
        return s.error[:48]
    return "exited — Restart"


_last_start_error = ""
# Prompt typed before save; consumed once the project home exists.
_pending_launch_prompt = ""


def last_start_error():
    return _last_start_error


def set_pending_launch_prompt(text):
    """Queue a first message to send after the project is saved."""
    global _pending_launch_prompt
    _pending_launch_prompt = str(text or "").strip()


def take_pending_launch_prompt():
    global _pending_launch_prompt
    text = _pending_launch_prompt
    _pending_launch_prompt = ""
    return text


def peek_pending_launch_prompt():
    return _pending_launch_prompt


def blend_is_saved():
    """True when the .blend has a real path on disk."""
    try:
        import bpy
        path = str(bpy.data.filepath or "").strip()
        return bool(path) and os.path.isfile(path)
    except Exception:
        return False


def project_home_paths(scene=None):
    """Return (project_root, terminal_workdir) for the saved blend, or ('','').

    project_root = ``<dir>/<stem>.cadex``
    terminal_workdir = ``<project_root>/.cadex_terminal``
    """
    try:
        import bpy
        from . import cadex_backend
        scene = scene or bpy.context.scene
        if not blend_is_saved():
            return "", ""
        root = os.path.abspath(cadex_backend.project_root(scene))
        term = os.path.join(root, ".cadex_terminal")
        return root, term
    except Exception:
        return "", ""


def project_home_ready(scene=None):
    """Saved blend with a resolvable project home (dirs may still be created)."""
    root, term = project_home_paths(scene)
    return bool(root and term)


def ensure_project_home(scene=None):
    """Create project + terminal dirs for the saved blend. Returns (ok, root, term, err)."""
    root, term = project_home_paths(scene)
    if not root or not term:
        return False, "", "", (
            "Save the project first. Model, chat, and history live next to "
            "the .blend file you choose."
        )
    try:
        os.makedirs(root, exist_ok=True)
        os.makedirs(term, exist_ok=True)
    except OSError as ex:
        return False, root, term, "Could not create project home: {!s}".format(ex)
    return True, root, term, ""


def ensure_running(cols=100, rows=32, fresh=False, initial_prompt=None):
    """Start the terminal if it is not already alive. Idempotent."""
    global _last_start_error
    if is_running():
        return True, "already running"
    if initial_prompt is None:
        initial_prompt = take_pending_launch_prompt() or None
    else:
        # Caller peeked; clear so we do not double-send later.
        take_pending_launch_prompt()
    ok, msg = start(
        cols=cols, rows=rows, fresh=fresh, initial_prompt=initial_prompt)
    if not ok:
        _last_start_error = msg
        # Keep the prompt so a later retry (after save / layout) can still send.
        if initial_prompt:
            set_pending_launch_prompt(initial_prompt)
    else:
        _last_start_error = ""
    return ok, msg


class TerminalSession:
    def __init__(self, conpty, screen, workdir, project_root, cols, rows,
                 session_id=""):
        self.conpty = conpty
        self.screen = screen
        self.workdir = workdir
        self.project_root = project_root
        self.session_id = str(session_id or "")
        self.cols = cols
        self.rows = rows
        self.started_at = time.time()
        self.error = ""
        self.focused = False
        self._pump_stop = threading.Event()
        # Debounced size from the draw path (avoid thrashing ConPTY on load).
        self._pending_cols = cols
        self._pending_rows = rows
        self._resize_deadline = 0.0
        self._last_content_at = time.time()
        self._recover_attempted = False

    def alive(self):
        try:
            return self.conpty is not None and self.conpty.alive()
        except Exception:
            return False

    def poll(self):
        """Read PTY output into the VT screen. Call from main-thread timer."""
        if self.conpty is None:
            return False
        self._apply_pending_resize()
        changed = False
        try:
            while True:
                data = self.conpty.read(65536)
                if not data:
                    break
                self.screen.feed(data)
                changed = True
                self._last_content_at = time.time()
        except Exception as ex:
            self.error = str(ex)
            return True
        if changed and self.screen_has_content():
            self._recover_attempted = False
        elif self.alive():
            self._maybe_recover_empty()
        return changed

    def write(self, data):
        if self.conpty is None:
            return
        try:
            self.conpty.write(data)
        except Exception as ex:
            self.error = str(ex)

    def request_resize(self, cols, rows):
        """Queue a size change; applied after a short debounce in poll()/draw.

        Only (re)starts the debounce when the *desired* size changes. Drawing
        every frame must not keep pushing the deadline out forever — that left
        Open Grok stuck at the birth size and a black panel.
        """
        cols = max(40, int(cols))
        rows = max(8, int(rows))
        if cols == self.cols and rows == self.rows:
            self._pending_cols = cols
            self._pending_rows = rows
            self._resize_deadline = 0.0
            return
        if cols == self._pending_cols and rows == self._pending_rows:
            # Already queued; do not reset the deadline.
            return
        self._pending_cols = cols
        self._pending_rows = rows
        self._resize_deadline = time.time() + _RESIZE_DEBOUNCE_S

    def resize(self, cols, rows):
        """Immediate resize (start path / forced). Prefer request_resize for UI."""
        cols = max(40, int(cols))
        rows = max(8, int(rows))
        self._pending_cols = cols
        self._pending_rows = rows
        self._resize_deadline = 0.0
        if cols == self.cols and rows == self.rows:
            return
        self.cols = cols
        self.rows = rows
        self.screen.resize(cols, rows)
        if self.conpty is not None:
            try:
                self.conpty.resize(cols, rows)
            except Exception:
                pass

    def apply_due_resize(self):
        """Apply a debounced resize if its deadline has passed (draw/timer)."""
        self._apply_pending_resize()

    def _apply_pending_resize(self):
        if self._resize_deadline <= 0:
            return
        if time.time() < self._resize_deadline:
            return
        cols, rows = self._pending_cols, self._pending_rows
        self._resize_deadline = 0.0
        if cols == self.cols and rows == self.rows:
            return
        self.cols = cols
        self.rows = rows
        self.screen.resize(cols, rows)
        if self.conpty is not None:
            try:
                self.conpty.resize(cols, rows)
            except Exception:
                pass

    def screen_has_content(self):
        """True if any non-space glyph is on the visible grid."""
        try:
            grid = self.screen.snapshot_lines()
        except Exception:
            return False
        for row in grid:
            for cell in row:
                ch = getattr(cell, "ch", " ") or " "
                if ch not in (" ", "\x00", ""):
                    return True
        return False

    def _maybe_recover_empty(self):
        """If the TUI stays blank after start, force one resize kick."""
        if self._recover_attempted:
            return
        if time.time() - self.started_at < _EMPTY_RECOVER_S:
            return
        if self.screen_has_content():
            return
        if time.time() - self._last_content_at < _EMPTY_RECOVER_S:
            return
        self._recover_attempted = True
        # Nudge Open Grok to full-repaint after a sticky empty alt-screen.
        try:
            self.conpty.resize(self.cols, self.rows)
        except Exception:
            pass
        try:
            # Ctrl+L — common full redraw in TUIs; harmless if ignored.
            self.write(b"\x0c")
        except Exception:
            pass

    def stop(self):
        if self.conpty is not None:
            try:
                self.conpty.close()
            except Exception:
                pass
            self.conpty = None


def _cadex_agent_path():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "cadex_agent.md")


def _write_project_mcp(workdir, bridge_port, bridge_token):
    conf_dir = os.path.join(workdir, ".opengrok")
    os.makedirs(conf_dir, exist_ok=True)
    python = sys.executable.replace("\\", "/")
    shim = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mcp_shim.py"
    ).replace("\\", "/")
    body = (
        "# Generated by Cadex mesh_agent terminal — do not commit.\n"
        "# OAuth tokens come from ~/.opengrok (login --oauth / --codex).\n"
        "[mcp_servers.mesh]\n"
        'command = "{python}"\n'
        "args = [\n"
        '  "{shim}",\n'
        '  "--port", "{port}",\n'
        '  "--token", "{token}",\n'
        "]\n"
        "enabled = true\n"
        "startup_timeout_sec = 60\n"
        "tool_timeout_sec = 600\n"
        "\n"
        "[ui]\n"
        'screen_mode = "fullscreen"\n'
        "compact_mode = true\n"
        "\n"
        "[ui.notifications.title]\n"
        "enabled = true\n"
        'items = ["action-required", "spinner", "activity", '
        '"session-name", "model"]\n'
    ).format(
        python=_toml_escape(python),
        shim=_toml_escape(shim),
        port=str(bridge_port),
        token=_toml_escape(bridge_token),
    )
    path = os.path.join(conf_dir, "config.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    with open(os.path.join(workdir, "CADEX_AGENT_WORKDIR.txt"), "w",
              encoding="utf-8") as handle:
        handle.write(
            "Cadex embedded Open Grok workdir. Mesh MCP tools talk to the "
            "live Cadex session. Sessions for this folder are the chat "
            "history for the sibling .blend model.\n"
        )
    with open(os.path.join(workdir, "AGENTS.md"), "w", encoding="utf-8") as handle:
        handle.write(_CADEX_AGENTS_MD)
    rules_dir = os.path.join(conf_dir, "rules")
    os.makedirs(rules_dir, exist_ok=True)
    with open(os.path.join(rules_dir, "cadex.md"), "w", encoding="utf-8") as handle:
        handle.write(_CADEX_AGENTS_MD)
    return path


def _system_prompt_for_terminal():
    tool_hint = (
        "\n\nYou must act through the mesh MCP tools (server name mesh) for "
        "all CAD work. Prefer mesh tools over shell or free-form file edits "
        "for geometry. The user is chatting with you inside the Cadex app."
    )
    return modes.system_prompt() + tool_hint


def _current_project_root(scene=None):
    try:
        import bpy
        from . import cadex_backend
        scene = scene or bpy.context.scene
        return os.path.abspath(cadex_backend.project_root(scene))
    except Exception:
        return ""


def _same_path(left, right):
    if not left or not right:
        return str(left or "") == str(right or "")
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right))


def needs_rebind(scene=None):
    """Whether the process-global terminal belongs to another project root."""
    session = _session
    if session is None:
        return False
    root, _term = project_home_paths(scene)
    if not root:
        return True
    return not _same_path(session.project_root, root)


def _record_terminal_state(session, status=""):
    try:
        agent = agent_module.get_agent()
        if session is not None:
            agent.history.set_terminal_state(
                workdir=session.workdir,
                project_root=session.project_root,
                session_id=session.session_id,
            )
        if status:
            agent.history.add("status", status)
        agent.save_state()
    except Exception:
        pass


def checkpoint_session():
    """Refresh Open Grok session id for this project and persist into the .blend.

    Called after AI auto-save so reopen can resume the same conversation.
    Does not add chat noise.
    """
    session = _session
    if session is None:
        # Still persist any workdir we know from history when terminal is down.
        try:
            agent = agent_module.get_agent()
            agent.save_state()
        except Exception:
            pass
        return
    try:
        latest = _discover_latest_session_id(session.workdir)
        if latest:
            session.session_id = latest
            _write_last_session_id(session.workdir, latest)
    except Exception:
        pass
    try:
        agent = agent_module.get_agent()
        agent.history.set_terminal_state(
            workdir=session.workdir,
            project_root=session.project_root,
            session_id=session.session_id,
        )
        agent.save_state()
    except Exception:
        pass


def _session_id_path(workdir):
    return os.path.join(workdir, _SESSION_ID_FILE)


def _read_last_session_id(workdir):
    path = _session_id_path(workdir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
        return value if value else ""
    except Exception:
        return ""


def _write_last_session_id(workdir, session_id):
    if not workdir or not session_id:
        return
    try:
        with open(_session_id_path(workdir), "w", encoding="utf-8") as handle:
            handle.write(str(session_id).strip() + "\n")
    except Exception:
        pass


def _opengrok_home():
    return os.path.expanduser(
        os.environ.get("OPENGROK_HOME") or os.path.join("~", ".opengrok"))


def _encode_session_cwd(workdir):
    """Match Open Grok's URL-encoded cwd folder name."""
    abs_path = os.path.abspath(workdir)
    # Try the form open-grok commonly uses (full path, URL-encoded).
    candidates = [
        quote(abs_path, safe=""),
        quote(abs_path.replace("\\", "/"), safe=""),
    ]
    # Windows drive letter variants.
    if len(abs_path) >= 2 and abs_path[1] == ":":
        candidates.append(quote(abs_path, safe=""))
    return candidates


def _session_group_dirs(workdir):
    """Directories under ~/.opengrok/sessions that belong to this workdir."""
    base = os.path.join(_opengrok_home(), "sessions")
    if not os.path.isdir(base):
        return []
    encoded = set(_encode_session_cwd(workdir))
    found = []
    try:
        names = os.listdir(base)
    except OSError:
        return []
    for name in names:
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        if name in encoded:
            found.append(path)
            continue
        # Long-path slug layout: folder contains a .cwd marker file.
        cwd_file = os.path.join(path, ".cwd")
        if os.path.isfile(cwd_file):
            try:
                with open(cwd_file, "r", encoding="utf-8") as handle:
                    marked = handle.read().strip()
                if _same_path(marked, workdir):
                    found.append(path)
            except Exception:
                pass
    return found


def _discover_latest_session_id(workdir):
    """Newest session id for this terminal workdir, or ''."""
    best_id = ""
    best_mtime = -1.0
    for group in _session_group_dirs(workdir):
        try:
            entries = os.listdir(group)
        except OSError:
            continue
        for name in entries:
            sess_dir = os.path.join(group, name)
            if not os.path.isdir(sess_dir):
                continue
            summary = os.path.join(sess_dir, "summary.json")
            mtime = 0.0
            if os.path.isfile(summary):
                try:
                    mtime = os.path.getmtime(summary)
                except OSError:
                    mtime = 0.0
            else:
                try:
                    mtime = os.path.getmtime(sess_dir)
                except OSError:
                    continue
            if mtime >= best_mtime:
                best_mtime = mtime
                best_id = name
    return best_id


def _session_dir_exists(workdir, session_id):
    if not session_id:
        return False
    for group in _session_group_dirs(workdir):
        if os.path.isdir(os.path.join(group, session_id)):
            return True
    return False


def _resume_args(workdir, fresh=False):
    """CLI args to continue this project's chat, or [] for a brand-new session.

    Returns (args_list, session_id_hint).
    """
    if fresh:
        return [], ""
    stored = _read_last_session_id(workdir)
    if stored and _session_dir_exists(workdir, stored):
        return ["--resume", stored], stored
    try:
        history = agent_module.get_agent().history
        hid = str(getattr(history, "terminal_session_id", "") or "")
        if hid and _session_dir_exists(workdir, hid):
            return ["--resume", hid], hid
    except Exception:
        pass
    latest = _discover_latest_session_id(workdir)
    if latest:
        # Prefer explicit resume for determinism over bare --continue.
        return ["--resume", latest], latest
    # No prior session for this project home — start a brand-new conversation.
    return [], ""


def start(cols=100, rows=32, fresh=False, initial_prompt=None):
    """Start (or restart) the embedded Open Grok terminal.

    Requires a saved .blend so model + chat share one project home.
    ``fresh=True`` starts a new conversation (does not resume).
    ``initial_prompt`` is passed as Open Grok's first user message (used when
    the user typed before saving the project).

    Returns (ok: bool, message: str).
    """
    global _session
    with _lock:
        if _session is not None and _session.alive():
            return True, "Open Grok terminal already running"
        if _session is not None:
            _session.stop()
            _session = None

    if sys.platform != "win32":
        return False, "Embedded Open Grok terminal is currently Windows-only"

    ok_home, project_root, workdir, home_err = ensure_project_home()
    if not ok_home:
        return False, home_err

    try:
        from .terminal_conpty import ConPTY
        from .terminal_vt import TerminalScreen
    except ImportError as ex:
        return False, "ConPTY unavailable: {!s}".format(ex)

    agent = agent_module.get_agent()
    bridge = agent.ensure_bridge()
    agent._ensure_bridge_pump()

    prefs = agent_module.get_prefs()
    path = find_open_grok(
        prefs.open_grok_path if prefs is not None else "")
    if path is None:
        return False, (
            "open-grok not found. Install it and run "
            "`open-grok login --oauth` (and/or `--codex`)."
        )

    try:
        import bpy
        if not bpy.app.online_access:
            try:
                prefs_sys = bpy.context.preferences.system
                if hasattr(prefs_sys, "use_online_access"):
                    was_dirty = bpy.context.preferences.is_dirty
                    prefs_sys.use_online_access = True
                    bpy.context.preferences.is_dirty = was_dirty
            except Exception:
                pass
    except Exception:
        pass

    _write_project_mcp(workdir, bridge.port, bridge.token)

    system = _system_prompt_for_terminal()
    prompt_path = os.path.join(workdir, "cadex_system_prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as handle:
        handle.write(system)

    agent_def = _cadex_agent_path()
    # A queued first message always starts a *new* turn on a clean session
    # for this project home (do not resume over the typed prompt).
    prompt = str(initial_prompt or "").strip()
    if prompt:
        fresh = True
    resume_args, session_hint = _resume_args(workdir, fresh=fresh)
    cmd = [
        path,
        "--cwd", workdir,
        "--disallowed-tools", _DISALLOWED_BUILTINS,
        "--no-subagents",
        "--permission-mode", "bypassPermissions",
        "--system-prompt-override", system,
        "--fullscreen",
    ]
    cmd.extend(resume_args)
    if os.path.isfile(agent_def):
        cmd.extend(["--agent", agent_def])
    if prompt:
        # Positional initial prompt — same as `open-grok "make a cube"`.
        cmd.append(prompt)

    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("COLORTERM", "truecolor")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("GROK_AUTH_PROVIDER_LABEL", "Cadex / Open Grok")

    cols = max(40, int(cols))
    rows = max(8, int(rows))
    screen = TerminalScreen(cols, rows)
    try:
        conpty = ConPTY(cmd, cwd=workdir, env=env, cols=cols, rows=rows)
    except OSError as ex:
        return False, "Failed to start ConPTY: {!s}".format(ex)

    # Prefer a concrete id when we have one; else rediscover after a beat.
    session_id = session_hint or _discover_latest_session_id(workdir)
    session = TerminalSession(
        conpty, screen, workdir, project_root, cols, rows,
        session_id=session_id)
    with _lock:
        _session = session

    screen.feed(
        b"\x1b[2J\x1b[H"
        b"\x1b[38;2;120;200;255m  Cadex\x1b[0m\r\n"
        b"\x1b[90m  parametric CAD \xc2\xb7 open-grok session\x1b[0m\r\n"
        b"\r\n"
        b"\x1b[90m  starting\xe2\x80\xa6\x1b[0m\r\n"
    )
    if session_id:
        _write_last_session_id(workdir, session_id)
    if prompt:
        note = (
            "Open Grok started for this project with your first message."
        )
    elif fresh:
        note = (
            "Open Grok started a new chat for this project "
            "({}).".format(os.path.basename(project_root) or project_root)
        )
    elif resume_args:
        note = (
            "Open Grok terminal started for this project; resuming chat "
            "history from its project home."
        )
    else:
        note = (
            "Open Grok terminal started; chat history will live with this "
            "project."
        )
    _record_terminal_state(session, note)
    return True, "Cadex terminal started"


def restart(cols=100, rows=32, fresh=False, initial_prompt=None):
    stop(status="Open Grok terminal restarting.")
    return start(
        cols=cols, rows=rows, fresh=fresh, initial_prompt=initial_prompt)


def stop(status="Open Grok terminal stopped.", persist=True):
    global _session
    with _lock:
        session = _session
        _session = None
    if session is not None:
        # Refresh last session id from disk before tearing down.
        try:
            latest = _discover_latest_session_id(session.workdir)
            if latest:
                session.session_id = latest
                _write_last_session_id(session.workdir, latest)
        except Exception:
            pass
        session.stop()
        if persist:
            _record_terminal_state(session, status)
    return True, "Open Grok terminal stopped"


def poll_and_redraw():
    """Timer-friendly: poll PTY, tag chat areas if dirty."""
    s = _session
    if s is None:
        return False
    # Late-bind session id once Open Grok has created the store.
    if not s.session_id:
        try:
            latest = _discover_latest_session_id(s.workdir)
            if latest:
                s.session_id = latest
                _write_last_session_id(s.workdir, latest)
                _record_terminal_state(s)
        except Exception:
            pass
    changed = s.poll()
    alive = s.alive()
    if changed or not alive:
        try:
            import bpy
            if not bpy.app.background:
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'CADEX_CHAT':
                            area.tag_redraw()
        except Exception:
            pass
    if not alive:
        return False
    return True
