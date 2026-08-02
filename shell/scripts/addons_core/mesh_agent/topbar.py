# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""The Cadex top bar: File and Edit, and nothing that says Blender.

The app template blanked the whole upper bar (ADR-037), which took
`File > Open / Save / Save As / Import / Export` with it, and Preferences,
which has no other door in the product -- the model selector is in the chat
header, but the engine path, the tool-call cap and the run budgets are add-on
preferences and were unreachable.

What comes back is two menus, not the bar: the stock bar also carries the
Blender menu (splash, about, system), Render, the workspace tabs and the
scene/view-layer pickers. A CAD app has nothing to render, ships one
workspace, and shows one scene.

The menus live here rather than in `shell/scripts/startup/bl_ui/` for the
reason `spaces.py` gives: `bl_ui` is inherited Blender and conservative,
`mesh_agent` is ours. Nothing upstream is edited -- `install()` swaps the
header's draw at runtime, and only the app template calls it, so the add-on
loaded into a stock Blender session leaves that session's top bar alone.

Almost everything the menus point at is stock: `wm.open_mainfile`,
`wm.save_mainfile` and the rest are the operators Blender ships, and
Import/Export are Blender's own menus, so a format added by an enabled add-on
appears here without this file knowing about it. The document is the `.blend`
(ADR-033): it carries the script mirror, the parameter specs and the engine
project id, so File > Save saves the model.

The exception is `Import Geometry...` (ADR-043), the first non-stock row in
this menu. It has to be ours: stock Import loads a mesh into the *Blender*
scene, which in Cadex is a display mirror of the engine's outputs and not the
model. Importing geometry into the model means putting the file in the engine
project's asset store, which only the engine may write -- so the row calls a
`put_asset` op, and the model then names the stored asset from the script.
"""

import bpy
from bpy.types import Menu, Operator

from . import agent as agent_module


class MESH_AGENT_OT_import_asset(Operator):
    bl_idname = "mesh_agent.import_asset"
    bl_label = "Import Geometry"
    bl_description = ("Copy an STL, OBJ or PLY file into this model's assets "
                      "so the script can build with it")

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.stl;*.obj;*.ply",
                                          options={'HIDDEN'})

    def invoke(self, context, _event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from . import cadex_backend

        payload = cadex_backend.put_asset(context.scene, self.filepath)
        history = agent_module.get_agent().history
        if payload.get("ok") is not True:
            report = str(payload.get("error") or payload)
            history.add("status", "Could not import geometry: " + report)
            self.report({'WARNING'}, report.splitlines()[0] if report else
                        "Import failed")
            return {'CANCELLED'}
        # Name the stored asset, because that name -- not the path the user
        # picked -- is what mesh.import_file() takes.
        history.add("status",
                    "Imported {:s} into this model's assets. Ask for it by "
                    "name: mesh.import_file(\"{:s}\").".format(
                        self.filepath, str(payload.get("name") or "")))
        return {'FINISHED'}


class CADEX_MT_file(Menu):
    bl_label = "File"

    def draw(self, context):
        layout = self.layout

        # INVOKE_DEFAULT, not EXEC: `wm.read_homefile`'s invoke is what raises
        # the "Save changes?" dialog. And `app_template` is deliberately left
        # unset -- unset means "the template already in force" (`wm_files.cc`,
        # `wm_homefile_read_exec`), so New lands back in the Cadex layout
        # rather than in stock Blender.
        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("wm.read_homefile", text="New", icon='FILE_NEW')

        layout.operator_context = 'INVOKE_AREA'
        layout.operator("wm.open_mainfile", text="Open...", icon='FILE_FOLDER')
        layout.menu("TOPBAR_MT_file_open_recent")
        layout.operator("wm.revert_mainfile")

        layout.separator()

        # Save on an unsaved file has no path to write to, so it has to open
        # the browser; on a saved one it must not. Stock does the same.
        layout.operator_context = ('EXEC_AREA' if context.blend_data.is_saved
                                   else 'INVOKE_AREA')
        layout.operator("wm.save_mainfile", text="Save", icon='FILE_TICK')

        layout.operator_context = 'INVOKE_AREA'
        layout.operator("wm.save_as_mainfile", text="Save As...")
        layout.operator("wm.save_as_mainfile", text="Save Copy...").copy = True

        layout.separator()

        # Ours, and first: importing *into the model* is the thing a CAD user
        # means by Import here. The stock menus below load into the Blender
        # scene, which is the display mirror (ADR-043).
        layout.operator("mesh_agent.import_asset", text="Import Geometry...",
                        icon='IMPORT')

        layout.separator()

        layout.menu("TOPBAR_MT_file_import", icon='IMPORT')
        layout.menu("TOPBAR_MT_file_export", icon='EXPORT')

        layout.separator()

        layout.operator("wm.quit_blender", text="Quit", icon='QUIT')


class MESH_AGENT_OT_toggle_viewport_overlays(Operator):
    """Show or hide 3D viewport overlays (floor grid, axes, etc.).

    Cadex's Mesh layout starts with overlays off for a clean CAD look, and
    the stock Shift+Alt+Z keymap is often missing in this stripped shell —
    so we ship our own toggle under Edit and rebind Shift+Alt+Z.
    """

    bl_idname = "mesh_agent.toggle_viewport_overlays"
    bl_label = "Toggle Viewport Overlays"
    bl_description = "Show or hide the floor grid and other 3D view overlays"

    def execute(self, context):
        spaces = []
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != 'VIEW_3D':
                    continue
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        spaces.append(space)
        if not spaces:
            self.report({'WARNING'}, "No 3D viewport found")
            return {'CANCELLED'}
        # Flip all views to match the first (keeps them in sync).
        new_state = not spaces[0].overlay.show_overlays
        for space in spaces:
            space.overlay.show_overlays = new_state
            if new_state:
                space.overlay.show_floor = True
                space.overlay.show_ortho_grid = True
        self.report(
            {'INFO'},
            "Viewport overlays " + ("on" if new_state else "off"))
        return {'FINISHED'}


class CADEX_MT_edit(Menu):
    bl_label = "Edit"

    def draw(self, _context):
        layout = self.layout

        layout.operator("ed.undo", icon='LOOP_BACK')
        layout.operator("ed.redo", icon='LOOP_FORWARDS')

        layout.separator()

        layout.operator("mesh_agent.toggle_viewport_overlays",
                        text="Toggle Viewport Overlays / Grid",
                        icon='GRID')

        layout.separator()

        # The only way into the add-on preferences: the engine path, the
        # tool-call cap and the run budgets live there (`__init__.py`).
        layout.operator("screen.userpref_show", text="Preferences...",
                        icon='PREFERENCES')


class CADEX_MT_editor_menus(Menu):
    bl_idname = "CADEX_MT_editor_menus"
    bl_label = ""

    def draw(self, _context):
        layout = self.layout
        layout.menu("CADEX_MT_file")
        layout.menu("CADEX_MT_edit")


def draw_upper_bar(header, context):
    """Replacement for `TOPBAR_HT_upper_bar.draw` (installed, not registered).

    The topbar's header draws once per alignment. The right-hand pass is where
    the scene and view-layer pickers went; Cadex has nothing to put there, so
    it draws nothing and the menus sit alone on the left.
    """
    if context.region.alignment == 'RIGHT':
        return
    # `draw_collapsible` folds the menus into one icon when the window is too
    # narrow for them, which is the stock behaviour and free.
    CADEX_MT_editor_menus.draw_collapsible(context, header.layout)


# ---------------------------------------------------------------------------
# Installing over the stock header
# ---------------------------------------------------------------------------

#: The stock draw, while ours is installed; None when it is not.
_stock_draw = None


def _reregister_with_draw(cls, draw):
    # Like poll, draw is captured at registration time, so re-register.
    bpy.utils.unregister_class(cls)
    cls.draw = draw
    bpy.utils.register_class(cls)


def installed():
    return _stock_draw is not None


def install():
    """Put the Cadex menus on the top bar. Idempotent."""
    global _stock_draw
    if _stock_draw is not None:
        return
    cls = bpy.types.TOPBAR_HT_upper_bar
    _stock_draw = cls.draw
    _reregister_with_draw(cls, draw_upper_bar)


def uninstall():
    """Give the stock bar back. Idempotent."""
    global _stock_draw
    if _stock_draw is None:
        return
    draw, _stock_draw = _stock_draw, None
    _reregister_with_draw(bpy.types.TOPBAR_HT_upper_bar, draw)


classes = (
    MESH_AGENT_OT_import_asset,
    MESH_AGENT_OT_toggle_viewport_overlays,
    CADEX_MT_file,
    CADEX_MT_edit,
    CADEX_MT_editor_menus,
)

_addon_keymaps = []


def _register_overlay_keymap():
    """Shift+Alt+Z → toggle overlays (missing from Cadex's stripped keymaps)."""
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc is None:
        return
    km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
    kmi = km.keymap_items.new(
        "mesh_agent.toggle_viewport_overlays",
        type='Z', value='PRESS', shift=True, alt=True)
    _addon_keymaps.append((km, kmi))


def _unregister_overlay_keymap():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    _register_overlay_keymap()


def unregister():
    # Before the menus go: a header pointing at menu classes that are no
    # longer registered draws a row of errors, and disabling the add-on is
    # exactly when that would happen.
    _unregister_overlay_keymap()
    uninstall()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
