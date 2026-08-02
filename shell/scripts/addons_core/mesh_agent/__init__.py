# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Mesh Agent: chat-driven scene building.

Cadex chat *is* the embedded Open Grok terminal (ConPTY + mesh MCP). It
auto-starts with the chat editor; model selection lives in open-grok
(``/model``). OAuth: ``open-grok login --oauth`` / ``--codex``. Headless
``open-grok -p`` remains only as a non-UI backend path.
"""

bl_info = {
    "name": "Mesh Agent",
    "description": "Chat assistant that builds and edits the scene (Open Grok / Claude)",
    "author": "Mesh",
    "version": (0, 2),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Mesh",
    "support": "OFFICIAL",
    "category": "3D View",
}

import bpy
from bpy.app.handlers import persistent

from . import agent as agent_module
from . import cadex_backend as cadex_backend_module
from . import cadex_live as cadex_live_module
from . import cadex_live_modal as cadex_live_modal_module
from . import cadex_pick as cadex_pick_module
from . import cadex_terminal_pick as cadex_terminal_pick_module
from . import cadex_training as cadex_training_module
from . import model as model_module
from . import spaces
from . import wiring as wiring_module
from . import wiring_ui as wiring_ui_module
from . import topbar as topbar_module
from . import ui
from . import terminal_session
from . import terminal_ui


def _wrap_remedy(text, width=64):
    """Break the remedy sentence into label-sized lines (labels don't wrap)."""
    lines, current = [], ""
    for word in str(text or "").split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


class MeshAgentPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    agent_backend: bpy.props.EnumProperty(
        name="Agent Backend",
        description="CLI agent that runs each chat turn (OAuth; no API keys from Cadex)",
        items=(
            ('open_grok', "Open Grok (recommended)",
             "open-grok headless; Grok OAuth and/or ChatGPT OAuth via "
             "`open-grok login --oauth` / `--codex`"),
            ('claude', "Claude Code",
             "claude -p; requires Anthropic / Claude subscription"),
        ),
        default='open_grok',
    )
    model: bpy.props.EnumProperty(
        name="Model",
        description="Model id for the selected backend",
        items=(
            # Open Grok / multi-provider (no bare grok-4 — use 4.5)
            ('grok-4.5', "grok-4.5",
             "Grok 4.5 via open-grok login --oauth"),
            ('gpt-5.6-sol', "gpt-5.6-sol",
             "ChatGPT OAuth via open-grok login --codex"),
            ('__default__', "Config default",
             "Use the model from ~/.opengrok config"),
            # Claude Code (only when backend is Claude)
            ('claude-fable-5', "Claude Fable", "Claude Code only"),
            ('claude-opus-4-8', "Claude Opus", "Claude Code only"),
            ('claude-sonnet-4-6', "Claude Sonnet", "Claude Code only"),
            ('claude-haiku-4-5', "Claude Haiku", "Claude Code only"),
        ),
        default='grok-4.5',
    )
    open_grok_path: bpy.props.StringProperty(
        name="Open Grok Path",
        description="Path to open-grok.exe (leave empty to auto-detect)",
        subtype='FILE_PATH',
        default="",
    )
    claude_path: bpy.props.StringProperty(
        name="Claude Code Path",
        description="Path to the `claude` CLI binary (leave empty to auto-detect)",
        subtype='FILE_PATH',
        default="",
    )
    max_tool_calls: bpy.props.IntProperty(
        name="Tool Call Limit",
        description="Maximum tool calls / agent turns the assistant may make "
                    "in one chat turn",
        default=agent_module.DEFAULT_TOOL_CAP,
        min=1, max=200,
    )
    freecadcmd_path: bpy.props.StringProperty(
        name="Cadex Engine (FreeCADCmd)",
        description="Leave empty to use the cadex engine bundled with Mesh. "
                    "Set this only to point Cadex mode at a different engine "
                    "build, e.g. when developing the engine itself",
        subtype='FILE_PATH',
        default="",
    )

    engine_timeout_seconds: bpy.props.FloatProperty(
        name="Engine Timeout (s)",
        description="Wall-clock budget for one cadex engine script run. "
                    "0 leaves the engine's own default in force",
        default=0.0, min=0.0, max=3600.0,
    )
    engine_memory_limit_mb: bpy.props.IntProperty(
        name="Engine Memory (MB)",
        description="Memory ceiling for one cadex engine script run. "
                    "0 leaves the engine's own default in force",
        default=0, min=0, max=131072,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "agent_backend")
        layout.prop(self, "model")
        if self.agent_backend == 'open_grok':
            layout.prop(self, "open_grok_path")
        else:
            layout.prop(self, "claude_path")
        layout.prop(self, "max_tool_calls")
        layout.prop(self, "freecadcmd_path")
        budgets = layout.row(align=True)
        budgets.prop(self, "engine_timeout_seconds")
        budgets.prop(self, "engine_memory_limit_mb")

        # Resolved engine row: what Cadex mode will actually run, and why
        # not when it cannot. Same wording as the chat panel and the tool
        # error, so one problem reads as one problem.
        ok, reason, remedy = cadex_backend_module.preflight()
        engine = layout.column(align=True)
        if ok:
            resolved, _module = cadex_backend_module.resolved_engine()
            engine.label(text="Cadex engine: " + (resolved or "found"),
                         icon='CHECKMARK')
        else:
            row = engine.row()
            row.alert = True
            row.label(text=reason, icon='ERROR')
            for line in _wrap_remedy(remedy):
                sub = engine.row()
                sub.enabled = False
                sub.label(text=line)

        column = layout.column()
        if self.agent_backend == 'open_grok':
            column.label(
                text="Auth: open-grok login --oauth (Grok) and/or "
                     "--codex (ChatGPT). No API keys.",
                icon='INFO')
            column.label(
                text="Embedded terminal: Start Open Grok in the chat editor "
                     "(needs pywinpty in Cadex Python).",
                icon='CONSOLE')
        else:
            column.label(
                text="Uses Claude Code login; run `claude` once in a "
                     "terminal to sign in.",
                icon='INFO')
        if not bpy.app.online_access:
            column.label(
                text="Online access is disabled in Preferences > System > Network.",
                icon='ERROR')


@persistent
def _save_pre_handler(_filepath):
    if cadex_live_module.is_running():
        cadex_live_module.stop(restore=True)
    agent_module.get_agent().save_state()
    # Last moment before the write, and bpy.data.filepath still names the
    # OLD file here, so this is the only point at which a Save-As can record
    # which project currently holds the model -- and the value lands inside
    # the file being written, so a duplicate opened in a fresh session knows
    # it too (ADR-046).
    try:
        cadex_backend_module.remember_source_root(bpy.context.scene)
    except Exception:
        pass


def _stop_terminal_for_file_change(scene=None, require_rebind=False):
    """Stop the old project terminal and invalidate its MCP credentials."""
    if terminal_session.get_session() is None:
        return False
    if require_rebind and not terminal_session.needs_rebind(scene):
        return False
    terminal_session.stop(persist=False)
    agent_module.get_agent().rotate_bridge()
    return True


@persistent
def _save_post_handler(_filepath):
    # Save-As renames the file, and the engine project root is derived from
    # the file name: the child spawned for the old root is no longer this
    # file's engine. Drop it, and say so if the model was left behind.
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    # First save of an unsaved file also needs a terminal home created.
    was_running = terminal_session.is_running()
    rebound = _stop_terminal_for_file_change(
        scene, require_rebind=True)
    # If nothing was running (user just saved for the first time), still
    # ensure the project home exists so auto-start can resume/create chat.
    try:
        terminal_session.ensure_project_home(scene)
    except Exception:
        pass
    try:
        note = cadex_backend_module.on_file_changed(scene)
    except Exception:
        note = ""
    agent = agent_module.get_agent()
    if note:
        agent.history.add("status", note)
    if rebound:
        agent.history.add(
            "status",
            "Open Grok terminal stopped after the project path changed; "
            "chat will restart for this file's project home.",
        )
    elif not was_running and terminal_session.project_home_ready(scene):
        pending = terminal_session.peek_pending_launch_prompt()
        if pending:
            agent.history.add(
                "status",
                "Project saved. Starting chat with your first message…",
            )
        else:
            agent.history.add(
                "status",
                "Project saved. Cadex Chat will start here and keep history "
                "with this file.",
            )
    if note or rebound or not was_running:
        agent.save_state()
    # Kick Open Grok immediately after save (with queued first prompt if any).
    try:
        if terminal_session.project_home_ready(scene):
            terminal_session.ensure_project_home(scene)
            if not terminal_session.is_running():
                from . import terminal_ui as terminal_ui_module
                prompt = terminal_session.peek_pending_launch_prompt() or None
                terminal_ui_module._ensure_terminal(initial_prompt=prompt)
    except Exception:
        pass
    # Nudge chat redraw so the save gate flips to starting/TUI.
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'CADEX_CHAT':
                    area.tag_redraw()
    except Exception:
        pass


@persistent
def _load_pre_handler(_filepath):
    cadex_live_module.stop(restore=False)


@persistent
def _load_post_handler(_filepath):
    # Stop before adopting the new .blend: the old process must never see the
    # new scene through its still-live MCP bridge.
    stopped = _stop_terminal_for_file_change()
    agent = agent_module.get_agent()
    agent.load_state()
    # Restore parameter sliders from the specs saved in the scene.
    model_module.on_load()
    # A different file is current; its engine project is a different one.
    # Without this, opening a second .blend leaks the first file's cadexd
    # child and can answer from the wrong project store.
    note = _report_file_change()
    try:
        terminal_session.ensure_project_home()
    except Exception:
        pass
    if stopped:
        agent.history.add(
            "status",
            "Previous Open Grok terminal stopped; this file will resume its "
            "own project-scoped chat when ready.",
        )
    if note or stopped:
        agent.save_state()
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'CADEX_CHAT':
                    area.tag_redraw()
    except Exception:
        pass


@persistent
def _frame_change_handler(*_args):
    # The Policy Outputs bars are drawn from `scene.frame_current`, and
    # `match_region_with_redraws` (screen_ops.cc) has no case for
    # SPACE_CADEX_PARAMS: playback tags the 3D viewport and leaves the
    # parameters editor showing whichever frame it last drew. Adding that
    # case would be a `docs/BLENDER-TREE.md` §2b line against inherited
    # Blender; tagging from the add-on is free and does the same job.
    #
    # Tag only -- no property writes. A frame-change handler that assigned
    # to the scene would re-enter the depsgraph on every frame of playback.
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        return
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'CADEX_PARAMS':
                area.tag_redraw()


def _report_file_change():
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    try:
        note = cadex_backend_module.on_file_changed(scene)
    except Exception:
        return ""
    if note:
        agent_module.get_agent().history.add("status", note)
    return note or ""


classes = (
    MeshAgentPreferences,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    model_module.register()
    cadex_backend_module.register()
    cadex_pick_module.register()
    cadex_terminal_pick_module.register()
    cadex_training_module.register()
    cadex_live_module.register()
    cadex_live_modal_module.register()
    wiring_module.register()
    ui.register()
    # Embedded Open Grok terminal (ConPTY) in CADEX_CHAT.
    terminal_ui.register()
    spaces.register()
    # Registers the menus; the app template is what puts them on the bar
    # (topbar.install), so a stock Blender session keeps its own top bar.
    topbar_module.register()
    # Last, and the only one allowed to stand down: a Panel or Header
    # naming an unregistered space type raises "Region not found in
    # space type" and aborts the whole registration loop, which is how
    # the top-bar menus once disappeared (ADR-036). On a bundle built
    # before ADR-066 re-registered the node editor, this leaves
    # EDITOR_AVAILABLE False and everything else working.
    wiring_ui_module.register()
    bpy.app.handlers.save_pre.append(_save_pre_handler)
    bpy.app.handlers.save_post.append(_save_post_handler)
    bpy.app.handlers.load_pre.append(_load_pre_handler)
    bpy.app.handlers.load_post.append(_load_post_handler)
    bpy.app.handlers.frame_change_post.append(_frame_change_handler)


def unregister():
    if _save_pre_handler in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_save_pre_handler)
    if _save_post_handler in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(_save_post_handler)
    if _load_pre_handler in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_load_pre_handler)
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    if _frame_change_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_frame_change_handler)
    wiring_ui_module.unregister()
    topbar_module.unregister()
    spaces.unregister()
    terminal_ui.unregister()
    ui.unregister()
    wiring_module.unregister()
    cadex_live_modal_module.unregister()
    cadex_live_module.unregister()
    cadex_training_module.unregister()
    cadex_terminal_pick_module.unregister()
    cadex_pick_module.unregister()
    cadex_backend_module.unregister()
    model_module.unregister()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    agent_module.shutdown_agent()
