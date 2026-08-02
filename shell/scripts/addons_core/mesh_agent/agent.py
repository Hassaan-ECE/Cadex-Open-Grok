# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Turn orchestration for the Mesh agent.

Threading model: the backend (a `claude -p` subprocess reader, or the mock)
runs in worker threads and pushes stream events onto ``self.events``; the MCP
bridge pushes pending tool calls onto ``self.bridge.requests``. Both queues are
drained on Blender's main thread — via a ``bpy.app.timers`` callback in the
GUI, or an explicit ``drain()`` loop in background mode/tests — because ``bpy``
is not thread-safe. Tool calls execute inside ``drain()`` and their results
unblock the waiting bridge socket thread, which resumes Claude Code.

Undo batching: mutating tool calls are counted per turn and a single
``ed.undo_push`` is issued when the turn finishes, so one Cmd-Z reverts the
whole chat turn.
"""

import os
import queue
import threading
import time
import traceback

from . import history as history_module
from . import modes
from . import tools
from .bridge import BridgeServer

# Used when the Claude backend is selected; Open Grok uses prefs / config default.
DEFAULT_MODEL = "claude-fable-5"
DEFAULT_TOOL_CAP = 25
# Default agent CLI for this fork: Open Grok (OAuth). Claude remains optional.
DEFAULT_AGENT_BACKEND = "open_grok"

# After the AI mutates the model, wait this long with no further tool calls
# before auto-saving the .blend + chat checkpoint (one save per turn).
_AUTOSAVE_QUIET_S = 2.0

# Legacy name: product prompt lives in modes.CADEX_SYSTEM_PROMPT (cadex-agent skill).
# Kept so `from .agent import SYSTEM_PROMPT` and tests still resolve.
def _system_prompt_text():
    from . import modes
    return modes.CADEX_SYSTEM_PROMPT


# Eager string for any code that concatenates SYSTEM_PROMPT at import time.
# modes.system_prompt() is the live API; this mirrors it once at load.
try:
    from .modes import CADEX_SYSTEM_PROMPT as SYSTEM_PROMPT
except Exception:  # pragma: no cover — import cycle safety
    SYSTEM_PROMPT = ""


def _default_undo_push(message):
    import bpy
    try:
        bpy.ops.ed.undo_push(message=message)
    except RuntimeError:
        # Undo may be unavailable (e.g. some background configurations).
        pass


def _tag_redraw():
    import bpy
    if bpy.app.background:
        return
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            # TEXT_EDITOR because one of them may be showing the script mirror,
            # which a turn (or a rebuild) rewrites under it.
            if area.type in {'VIEW_3D', 'CADEX_CHAT', 'CADEX_PARAMS',
                             'TEXT_EDITOR'}:
                area.tag_redraw()


def get_prefs():
    import bpy
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon is not None else None


class Agent:
    def __init__(self):
        self.history = history_module.ChatHistory()
        self.events = queue.Queue()
        self.bridge = None
        self.backend = None
        self.busy = False
        self.last_error = ""
        # Image attachments for the session; indices are stable so the model
        # can request any of them across turns via get_attached_image.
        self.attachments = []
        self._sent_attachments = 0
        # Test/injection hooks.
        self.backend_factory = None
        self.tool_cap_override = None
        self._undo_push = _default_undo_push
        self._prompt = ""
        self._tool_calls = 0
        self._mutations = 0
        self._got_result = False
        self._timer_fn = self._timer
        # A tool call whose engine work is still running: (request, Pending).
        # At most one, because the bridge socket thread blocks on the reply,
        # so Claude Code cannot issue a second tool call until this one is
        # answered. Kept as a slot rather than a queue so that invariant is
        # visible rather than assumed.
        self._pending = None
        # Per-turn cancel flag, polled by the cadexd client every 50 ms.
        self._cancel_event = threading.Event()
        # Project auto-save after AI work (embedded TUI + headless turns).
        self._autosave_dirty = False
        self._autosave_after = 0.0
        self._last_autosave_error = ""

    # -- setup -------------------------------------------------------------

    def ensure_bridge(self):
        if self.bridge is None:
            self.bridge = BridgeServer(tools.list_tools)
        return self.bridge

    def rotate_bridge(self):
        """Invalidate old MCP credentials and rebind any headless backend."""
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        if self.backend is not None:
            bridge = self.ensure_bridge()
            if hasattr(self.backend, "bridge_port"):
                self.backend.bridge_port = bridge.port
            if hasattr(self.backend, "bridge_token"):
                self.backend.bridge_token = bridge.token
        return self.bridge

    # -- blend-scoped chat state --------------------------------------------

    def save_state(self):
        """Persist headless history plus terminal metadata into the .blend."""
        if self.backend is not None:
            self.history.session_id = str(
                getattr(self.backend, "session_id", "") or "")
        self.history.save_to_text_block()

    def load_state(self):
        """Adopt the newly-opened .blend's headless conversation state.

        The Agent is a process-level singleton, so without this the backend
        keeps the *previous* file's session id and the next turn resumes
        the wrong conversation into the wrong model. Opening a file
        therefore rebinds the headless session, and a file with no saved
        session starts a fresh one. Embedded terminal continuity remains in
        Open Grok's project workdir; ``history`` only mirrors its metadata.
        """
        self.history.load_from_text_block()
        if self.backend is not None and hasattr(self.backend, "session_id"):
            self.backend.session_id = self.history.session_id or None

    def new_conversation(self):
        """Start a fresh conversation. False if a turn is running.

        Emptying the transcript is not enough on its own. The backend outlives
        the turn and keeps the session id it learned from the stream, so the
        next turn would still pass ``--resume`` and the model would answer
        with everything the user just cleared still in its context. The
        attachments go with it: their indices are what ``get_attached_image``
        takes, and a new conversation starts them again at zero.
        """
        if self.busy:
            return False
        self.history.clear()
        if self.backend is not None and hasattr(self.backend, "session_id"):
            self.backend.session_id = None
        self.attachments = []
        self._sent_attachments = 0
        self.save_state()
        _tag_redraw()
        return True

    def shutdown(self):
        if self.backend is not None:
            self.backend.cancel()
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None

    def _tool_cap(self):
        if self.tool_cap_override is not None:
            return self.tool_cap_override
        prefs = get_prefs()
        return prefs.max_tool_calls if prefs is not None else DEFAULT_TOOL_CAP

    def _make_backend(self):
        import bpy

        bridge = self.ensure_bridge()
        if self.backend_factory is not None:
            return self.backend_factory(bridge)

        if os.environ.get("MESH_AGENT_MOCK"):
            from .mock_backend import MockBackend
            return MockBackend(bridge_port=bridge.port, bridge_token=bridge.token)

        # Cadex chat needs network for Open Grok / Claude. Prefer enabling the
        # System toggle rather than failing with a preferences scavenger hunt.
        if not bpy.app.online_access:
            try:
                prefs_sys = bpy.context.preferences.system
                if hasattr(prefs_sys, "use_online_access"):
                    was_dirty = bpy.context.preferences.is_dirty
                    prefs_sys.use_online_access = True
                    bpy.context.preferences.is_dirty = was_dirty
            except Exception:
                pass
        if not bpy.app.online_access:
            self.history.add(
                "status",
                "Online access is disabled. Enable it in Edit > Preferences > "
                "System > Network > Allow Online Access, then try again.")
            return None

        prefs = get_prefs()
        backend_id = (
            getattr(prefs, "agent_backend", None)
            if prefs is not None
            else None
        ) or DEFAULT_AGENT_BACKEND
        tool_names = [tool["name"] for tool in tools.list_tools()]
        system_prompt = modes.system_prompt()
        max_turns = (
            prefs.max_tool_calls if prefs is not None else DEFAULT_TOOL_CAP
        )

        if backend_id == "open_grok":
            from .open_grok_backend import (
                MODEL_CONFIG_DEFAULT,
                OpenGrokBackend,
                find_open_grok,
            )
            path = find_open_grok(
                prefs.open_grok_path if prefs is not None else "")
            if path is None:
                self.history.add(
                    "status",
                    "Open Grok CLI not found. Install open-grok, ensure it is "
                    "on PATH (or set Open Grok Path in add-on preferences), "
                    "then run `open-grok login --oauth` and/or "
                    "`open-grok login --codex` (OAuth; no API keys).")
                return None
            model = (
                prefs.model if prefs is not None else MODEL_CONFIG_DEFAULT
            )
            return OpenGrokBackend(
                open_grok_path=path,
                model=model,
                system_prompt=system_prompt,
                tool_names=tool_names,
                bridge_port=bridge.port,
                bridge_token=bridge.token,
                max_turns=max_turns,
            )

        # Optional Claude Code path (requires Anthropic subscription).
        from .backend import ClaudeCodeBackend, find_claude
        claude_path = find_claude(
            prefs.claude_path if prefs is not None else "")
        if claude_path is None:
            self.history.add(
                "status",
                "Claude Code CLI not found. Install it "
                "(https://claude.com/claude-code) or switch Agent Backend to "
                "Open Grok in add-on preferences.")
            return None
        model = prefs.model if prefs is not None else DEFAULT_MODEL
        return ClaudeCodeBackend(
            claude_path=claude_path,
            model=model,
            system_prompt=system_prompt,
            tool_names=tool_names,
            bridge_port=bridge.port,
            bridge_token=bridge.token,
        )

    def _sync_model(self):
        """Adopt the preferences' model before a turn starts. Like the mode,
        the backend rebuilds its argv per turn, so updating ``backend.model``
        in place switches models while ``--resume`` keeps the conversation.
        Backends without a ``model`` attribute (the mock) are left alone."""
        prefs = get_prefs()
        if (prefs is None or self.backend is None
                or not hasattr(self.backend, "model")):
            return
        if self.backend.model != prefs.model:
            self.backend.model = prefs.model
            self.history.add("status", "Model: " + prefs.model)

    # -- turn lifecycle ----------------------------------------------------

    def attach_image(self, path):
        """Queue an image for the next turn. Returns its index or -1."""
        if not path or not os.path.isfile(path):
            return -1
        index = len(self.attachments)
        self.attachments.append({"path": path,
                                 "name": os.path.basename(path)})
        self.history.add("status", "Attached image #{:d}: {:s}".format(
            index, os.path.basename(path)))
        _tag_redraw()
        return index

    def pending_attachment_count(self):
        return len(self.attachments) - self._sent_attachments

    def _attachment_note(self):
        """Prompt suffix describing attachments added since the last turn."""
        new = self.attachments[self._sent_attachments:]
        if not new:
            return ""
        lines = ["[The user attached image #{:d} ({:s}); call "
                 "get_attached_image with index={:d} to view it.]".format(
                     self._sent_attachments + offset, item["name"],
                     self._sent_attachments + offset)
                 for offset, item in enumerate(new)]
        self._sent_attachments = len(self.attachments)
        return "\n\n" + "\n".join(lines)

    def start_turn(self, prompt):
        if self.busy:
            return False
        prompt = prompt.strip()
        if not prompt and self.pending_attachment_count() == 0:
            return False
        if not prompt:
            prompt = "See the attached image(s)."

        self.history.add("user", prompt)
        self._sync_model()

        if self.backend is None:
            self.backend = self._make_backend()
        if self.backend is None:
            _tag_redraw()
            return False

        self._prompt = prompt
        self._tool_calls = 0
        self._mutations = 0
        self._got_result = False
        self._thought_open = False
        self.last_error = ""
        self._cancel_event.clear()
        self.busy = True
        self.history.begin_assistant()
        # The transcript shows the plain prompt; the model additionally gets
        # notes about freshly attached images and freshly picked BREP pins.
        note = self._attachment_note()
        from . import cadex_pick
        note += cadex_pick.consume_pin_notes()
        # A measured terminal is not a pin (docs/XSCRIPT.md): its own queue,
        # its own wording, drained here so several picks cost one turn
        # rather than one turn each (ADR-067).
        from . import cadex_terminal_pick
        note += cadex_terminal_pick.consume_terminal_notes()
        self.backend.start_turn(prompt + note, self.events)
        self._ensure_timer()
        _tag_redraw()
        return True

    def cancel(self):
        if not self.busy:
            return
        # Set the flag before killing Claude Code: a modeling request already
        # in flight is cancelled through the protocol (the engine answers
        # RUN_CANCELLED), not left to finish into a dead turn.
        self._cancel_event.set()
        if self.backend is not None:
            self.backend.cancel()
        self.history.add("status", "Cancelled.")

    def cancellation_check(self):
        """Predicate handed to long-running tools; true once cancelled."""
        return self._cancel_event.is_set

    # -- main-thread pump --------------------------------------------------

    def _timer(self):
        try:
            self.drain()
        except Exception:
            traceback.print_exc()
        try:
            self._maybe_autosave()
        except Exception:
            traceback.print_exc()
        # Keep pumping while a headless turn OR the embedded Open Grok
        # terminal is alive (mesh MCP tools arrive on the bridge either way).
        try:
            from . import terminal_session
            term_live = terminal_session.is_running()
            if term_live:
                terminal_session.poll_and_redraw()
        except Exception:
            term_live = False
        # Stay scheduled while an autosave is pending after AI work.
        wait_autosave = self._autosave_dirty
        if self.busy or term_live or wait_autosave:
            if wait_autosave and not self.busy and not term_live:
                return 0.25
            return 0.05 if term_live else 0.1
        return None

    def _ensure_timer(self):
        import bpy
        if bpy.app.background:
            return
        if not bpy.app.timers.is_registered(self._timer_fn):
            bpy.app.timers.register(self._timer_fn, first_interval=0.05)

    def _ensure_bridge_pump(self):
        """Start the main-thread drain timer for bridge-only traffic.

        Used by the embedded Open Grok terminal: there is no headless turn
        (``busy`` stays false), but mesh MCP tool calls still need draining.
        """
        self.ensure_bridge()
        self._ensure_timer()

    def drain(self):
        """Process pending tool calls and stream events. Main thread only."""
        handled = False

        if self._pending is not None and self._poll_pending():
            handled = True

        # While a tool's engine work is in flight the bridge cannot have sent
        # another call (it blocks on the reply), but leaving the queue alone
        # keeps replies strictly in dispatch order regardless.
        if self._pending is None and self.bridge is not None:
            while True:
                try:
                    request = self.bridge.requests.get_nowait()
                except queue.Empty:
                    break
                handled = True
                self._handle_tool_request(request)

        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            handled = True
            kind = event[0]
            if kind == "stream":
                self._on_stream(event[1])
            elif kind == "error":
                self._finish(error=event[1])
            elif kind == "exit":
                returncode, stderr_tail = event[1], event[2]
                if self.busy and not self._got_result:
                    detail = stderr_tail or "exit code {:d}".format(returncode)
                    self._finish(error="Agent ended unexpectedly: " + detail)
                elif self.busy:
                    self._finish()

        if handled:
            _tag_redraw()
        return handled

    def _handle_tool_request(self, request):
        # Cap only applies to headless chat turns. The embedded Open Grok
        # terminal is a long-lived session and must not be cut mid-session.
        if self.busy:
            cap = self._tool_cap()
            if self._tool_calls >= cap:
                request.reply(
                    [{"type": "text",
                      "text": "Tool call limit ({:d}) reached for this turn. "
                              "Summarize progress and stop.".format(cap)}],
                    True)
                return
            self._tool_calls += 1
        result = tools.execute(request.tool, request.input, agent=self)
        if isinstance(result, tools.Pending):
            self._pending = (request, result)
            # Background mode has no timer to poll us again, so resolve here.
            # Same code path either way — the poll loop just runs inline.
            import bpy
            if bpy.app.background:
                while not self._poll_pending():
                    pass
            return
        self._settle(request, result)

    def _settle(self, request, result):
        """Reply to one tool call and account for it. Main thread only."""
        content, is_error = result
        if not is_error and request.tool in tools.MUTATING_TOOLS:
            self._mutations += 1
            # Embedded Open Grok turns never set busy/_finish; schedule a
            # quiet-period autosave so the .blend + chat survive a quit.
            self._mark_autosave_needed()
        request.reply(content, is_error)

    def _mark_autosave_needed(self):
        """Note that AI changed the model; save once tools go quiet."""
        self._autosave_dirty = True
        self._autosave_after = time.time() + _AUTOSAVE_QUIET_S
        self._ensure_timer()

    def _bridge_idle(self):
        if self._pending is not None:
            return False
        if self.bridge is None:
            return True
        try:
            return self.bridge.requests.empty()
        except Exception:
            return True

    def _maybe_autosave(self):
        """Silent-save the project after the AI finishes mutating work."""
        if not self._autosave_dirty:
            return
        if time.time() < self._autosave_after:
            return
        if self.busy or not self._bridge_idle():
            # Still mid-turn; wait for another quiet window.
            self._autosave_after = time.time() + _AUTOSAVE_QUIET_S
            return
        self._autosave_dirty = False
        ok, detail = autosave_project()
        if ok:
            _tag_redraw()
        else:
            self._last_autosave_error = detail or "autosave failed"

    def _poll_pending(self):
        """Poll the deferred tool call; True once it has been answered."""
        pending = self._pending
        if pending is None:
            return False
        request, work = pending
        try:
            outcome = work.poll()
        except Exception:
            outcome = ([{"type": "text", "text": traceback.format_exc()}], True)
        if outcome is None:
            return False
        self._pending = None
        self._settle(request, outcome)
        return True

    def _on_stream(self, obj):
        obj_type = obj.get("type")
        if obj_type == "stream_event":
            event = obj.get("event", {})
            if event.get("type") == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text = delta.get("text") or ""
                    if not text:
                        return
                    # Open Grok reasoning: muted, one bullet per thought
                    # event, so the user can follow and cancel.
                    if obj.get("cadex_thought"):
                        self._thought_open = True
                        self.history.append_thought(text)
                        return
                    if getattr(self, "_thought_open", False):
                        # Close the thought block before the final reply.
                        self.history.end_assistant()
                        self.history.begin_assistant()
                        self._thought_open = False
                    self.history.append_stream(text)
        elif obj_type == "assistant":
            for block in obj.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    short = name.rsplit("__", 1)[-1]
                    self._thought_open = False
                    self.history.end_assistant()
                    self.history.add("status", "· " + short)
                    self.history.begin_assistant()
        elif obj_type == "result":
            self._got_result = True
            self._thought_open = False
            if obj.get("is_error"):
                self.last_error = str(obj.get("result", "unknown error"))

    def _finish(self, error=None):
        if not self.busy:
            return
        # A turn can end (cancel, backend crash) while a tool is still
        # waiting on the engine. Answer it, or the bridge socket thread
        # blocks until its 600 s timeout.
        if self._pending is not None:
            request, work = self._pending
            self._pending = None
            outcome = None
            try:
                outcome = work.poll()
            except Exception:
                pass
            self._settle(request, outcome or (
                [{"type": "text", "text": "The turn ended before this tool "
                                          "finished."}], True))
        self.busy = False
        self.history.end_assistant()
        if error:
            self.last_error = str(error)
        if self.last_error:
            self.history.add("status", "Error: " + self.last_error)
        if self._mutations > 0:
            self._undo_push("Mesh: " + self._prompt[:60])
            # Headless path: turn is over — save immediately.
            self._autosave_dirty = True
            self._autosave_after = 0.0
            try:
                self._maybe_autosave()
            except Exception:
                traceback.print_exc()
        try:
            self.save_state()
        except Exception:
            traceback.print_exc()


def autosave_project():
    """Write the .blend and checkpoint Open Grok session metadata.

    Returns (ok, detail). Skips silently when the file has never been saved
    (no path) — the compose/save-first flow owns that case.
    """
    import bpy

    path = str(bpy.data.filepath or "").strip()
    if not path:
        return False, "unsaved"
    try:
        # Save even if Blender's dirty flag is clear: engine geometry lives
        # beside the file and may have changed without marking the blend.
        bpy.ops.wm.save_mainfile()
    except Exception as ex:
        return False, str(ex)
    try:
        from . import terminal_session
        terminal_session.checkpoint_session()
    except Exception:
        pass
    try:
        get_agent().save_state()
    except Exception:
        pass
    return True, path


# Module-level singleton used by the UI.
_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


def shutdown_agent():
    global _agent
    if _agent is not None:
        _agent.shutdown()
        _agent = None
