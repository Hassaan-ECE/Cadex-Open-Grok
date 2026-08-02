# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""
Mesh app template: clean viewport on the left, Cadex Chat on the right,
Cadex Parameters under the viewport.

Launch with: blender --app-template Mesh

**The layout is `startup.blend`, not code.** It became expressible as a saved
screen when the chat and parameter columns became real editor types (ADR-035):
a saved screen can only record area *types*, and until then the area types
were lying -- all three columns were Properties editors, told apart at draw
time by comparing their coordinates. What used to be a 340-line retrying timer
state machine that split areas, monkeypatched two header draw functions and
re-registered every foreign Tool panel with `poll -> False` is now a file, and
this module is what is left over (ADR-037).

Three things survive, because none of them can live in a .blend:

- **Enabling the add-on.** `preferences.addons` is `UserDef`, not `Main`, so a
  startup file cannot carry it. Shipping a `Mesh/userpref.blend` would work
  and would also pin the user's theme, paths, keymap and autosave -- so this
  stays four lines of Python instead.
- **The top bar.** It carries the Cadex File and Edit menus rather than
  Blender's six (`mesh_agent.topbar`, ADR-041). A header's draw function is
  code, not screen data, so no `.blend` can carry it -- and the swap belongs
  to the product shell rather than to the add-on, so that `mesh_agent` in a
  stock Blender session leaves that session's top bar alone.
- **Suppressing the splash** (ADR-042). It is a `UserDef` flag, and the one
  thing here that has to run in the load handler rather than the timer --
  `creator.c` reads the flag immediately after `WM_init`.

To re-author the layout: launch, arrange it by hand, `File > Defaults > Save
Startup File`, then copy
`<config>/Mesh/startup.blend` over the one beside this file. Do it in one
commit -- every re-save is a new git-LFS object and the old one is never
reclaimed.
"""

import bpy
from bpy.app.handlers import persistent


def _ensure_agent_addon():
    # Must run deferred: add-on paths are not registered yet while the app
    # template's own register() executes during startup.
    # default_set=True so Cadex remembers the add-on across launches (UserDef).
    # Cadex *is* this product; leaving it off every restart was a stock-Blender
    # courtesy that does not apply when the only app template is Mesh.
    import addon_utils
    try:
        addon_utils.enable("mesh_agent", default_set=True, persistent=True)
    except Exception:
        import traceback
        traceback.print_exc()


def _cadex_topbar():
    # Must run after the add-on is enabled: the menus the bar draws are
    # registered by `mesh_agent.register()`.
    from mesh_agent import topbar
    topbar.install()


def _hide_splash():
    """No Blender splash on startup (ADR-042).

    Must run from the handler rather than the timer: the check is
    `U.uiflag & USER_SPLASH_DISABLE` in `wm_init_splash_show_on_startup_check`
    (`wm_init_exit.cc`), read from `creator.cc` right after `WM_init` -- which
    is after this handler and long before any timer fires.

    The dirty flag is put back deliberately. Preferences auto-save on exit
    when dirty, and this is the product deciding what it launches into, not
    the user editing a preference: writing it into `userpref.blend` would
    reach through the shared profile into stock Blender sessions too. It costs
    nothing to re-apply, because this runs on every startup.
    """
    preferences = bpy.context.preferences
    if not preferences.view.show_splash:
        return
    was_dirty = preferences.is_dirty
    preferences.view.show_splash = False
    preferences.is_dirty = was_dirty


def _ensure_online_access():
    """Cadex chat needs network (Open Grok / agent). Enable the System flag.

    Same dirty-flag care as ``_hide_splash``: product default for this app
    template, not a permanent write into the user's shared prefs if we can
    avoid it. Blender still may persist the toggle when they save prefs.
    """
    preferences = bpy.context.preferences
    system = preferences.system
    if getattr(system, "use_online_access", True):
        return
    was_dirty = preferences.is_dirty
    system.use_online_access = True
    preferences.is_dirty = was_dirty


def _ensure_viewport_overlays():
    """Turn viewport overlays (floor grid) on for the Cadex 3D views.

    Mesh ``startup.blend`` ships with ``show_overlays = False`` for a clean
    CAD look, which also hides the floor grid. Users expect a grid and a way
    to toggle it; enable overlays here, then they can turn them off from
    Edit > Toggle Viewport Overlays (or Shift+Alt+Z once mesh_agent is on).
    """
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for space in area.spaces:
                if space.type != 'VIEW_3D':
                    continue
                space.overlay.show_overlays = True
                space.overlay.show_floor = True
                space.overlay.show_ortho_grid = True


def _apply():
    try:
        _ensure_online_access()
        _ensure_agent_addon()
        _cadex_topbar()
        _ensure_viewport_overlays()
    except Exception:
        import traceback
        traceback.print_exc()
    return None


def _schedule_apply():
    if bpy.app.background:
        return
    if not bpy.app.timers.is_registered(_apply):
        bpy.app.timers.register(_apply, first_interval=0.1)


@persistent
def load_handler(_):
    """Factory startup (fresh Cadex launch with --app-template Mesh)."""
    if bpy.app.background:
        return
    _hide_splash()
    # Online access is also applied from the timer: some builds resolve
    # preferences after load handlers, and chat needs the flag set before
    # the first turn.
    try:
        _ensure_online_access()
    except Exception:
        pass
    _schedule_apply()


@persistent
def load_post_handler(_):
    """Any .blend open: keep mesh_agent on (opening a file is not factory startup)."""
    if bpy.app.background:
        return
    _schedule_apply()


def register():
    bpy.app.handlers.load_factory_startup_post.append(load_handler)
    bpy.app.handlers.load_post.append(load_post_handler)


def unregister():
    if load_handler in bpy.app.handlers.load_factory_startup_post:
        bpy.app.handlers.load_factory_startup_post.remove(load_handler)
    if load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(load_post_handler)
