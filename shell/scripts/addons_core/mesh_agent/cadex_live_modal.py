"""Viewport spring-drag interaction for Cadex Live.

Drag mode stays active until Esc/RMB so multiple grabs work without re-clicking
the panel button. While Live is paused, the same drag path poses the mechanism
under joint constraints (sidecar runs zero-g settle steps only while forces are
applied) so the user can place links, then Resume to play from that pose.
"""

import time

from . import cadex_hydrate
from . import cadex_live


# Gains are applied *inside* the MuJoCo sidecar each substep (true body state).
# Soft + overdamped avoids the classic delayed-UI spring shake.
SPRING_N_PER_M = 28.0
SPRING_PAUSED_N_PER_M = 55.0
DAMPING_NS_PER_M = 14.0
DAMPING_PAUSED_NS_PER_M = 28.0
MAX_FORCE_N = 18.0
MAX_FORCE_PAUSED_N = 40.0
UPDATE_SECONDS = 1.0 / 60.0


def _viewport_at(areas, mouse_x, mouse_y):
    for area in areas:
        if getattr(area, "type", "") != 'VIEW_3D':
            continue
        for region in getattr(area, "regions", ()):
            if getattr(region, "type", "") != 'WINDOW':
                continue
            if (region.x <= mouse_x < region.x + region.width
                    and region.y <= mouse_y < region.y + region.height):
                space = getattr(getattr(area, "spaces", None), "active", None)
                region_3d = getattr(space, "region_3d", None)
                if region_3d is not None:
                    return area, region, region_3d
    return None, None, None


def _make_operator():
    import bpy
    from bpy_extras import view3d_utils

    class MESH_AGENT_OT_live_drag(bpy.types.Operator):
        bl_idname = "mesh_agent.live_drag"
        bl_label = "Drag Body"
        bl_description = (
            "Enter drag mode: LMB-drag dynamic bodies repeatedly; "
            "works while paused to pose, then Resume. Esc exits drag mode"
        )

        @classmethod
        def poll(cls, _context):
            return cadex_live.can_drag()

        def invoke(self, context, _event):
            if context.window is None or not cadex_live.is_running():
                return {'CANCELLED'}
            self._timer = context.window_manager.event_timer_add(
                UPDATE_SECONDS, window=context.window
            )
            self._dragging = False
            self._body = ""
            self._object = None
            self._local_attach = None
            self._target_world = None
            self._previous_world = None
            self._previous_time = time.perf_counter()
            self._region = None
            self._region_3d = None
            self._depth = 0.0
            context.window.cursor_modal_set('CROSSHAIR')
            context.window_manager.modal_handler_add(self)
            paused = bool(cadex_live.status().get("paused"))
            if paused:
                self.report(
                    {'INFO'},
                    "Pose mode: LMB-drag links while paused; Resume to play; Esc exits",
                )
            else:
                self.report(
                    {'INFO'},
                    "Drag mode: LMB-drag links repeatedly; Esc exits",
                )
            return {'RUNNING_MODAL'}

        def _finish(self, context, cancelled=False):
            self._end_grab()
            timer = getattr(self, "_timer", None)
            if timer is not None:
                context.window_manager.event_timer_remove(timer)
                self._timer = None
            try:
                context.window.cursor_modal_restore()
            except Exception:
                pass
            return {'CANCELLED'} if cancelled else {'FINISHED'}

        def _end_grab(self):
            """Release the current body but stay in drag mode."""
            was_dragging = bool(self._dragging)
            self._dragging = False
            self._body = ""
            self._object = None
            self._local_attach = None
            self._target_world = None
            self._previous_world = None
            self._region = None
            self._region_3d = None
            if was_dragging:
                cadex_live.clear_drag()
                cadex_live.clear_forces()

        def _ray(self, event):
            coordinate = (
                event.mouse_x - self._region.x,
                event.mouse_y - self._region.y,
            )
            origin = view3d_utils.region_2d_to_origin_3d(
                self._region, self._region_3d, coordinate
            )
            direction = view3d_utils.region_2d_to_vector_3d(
                self._region, self._region_3d, coordinate
            ).normalized()
            return origin, direction

        def _begin_drag(self, context, event):
            _area, region, region_3d = _viewport_at(
                context.window.screen.areas, event.mouse_x, event.mouse_y
            )
            if region is None:
                return False
            coordinate = (event.mouse_x - region.x, event.mouse_y - region.y)
            origin = view3d_utils.region_2d_to_origin_3d(
                region, region_3d, coordinate
            )
            direction = view3d_utils.region_2d_to_vector_3d(
                region, region_3d, coordinate
            ).normalized()
            depsgraph = context.evaluated_depsgraph_get()
            hit, location, _normal, _index, obj, _matrix = context.scene.ray_cast(
                depsgraph, origin, direction
            )
            if not hit or obj is None:
                self.report({'INFO'}, "Nothing under the cursor")
                return False
            body = str(obj.get(cadex_hydrate.OUTPUT_PROP, "") or "")
            if not cadex_live.can_drag(body):
                self.report({'WARNING'}, "That Cadex body is fixed or not in Live")
                return False
            self._dragging = True
            self._body = body
            self._object = obj
            self._local_attach = obj.matrix_world.inverted_safe() @ location
            self._region = region
            self._region_3d = region_3d
            # Depth plane through the hit so drag stays parallel to the view.
            self._depth = max(0.001, float((location - origin).dot(direction)))
            self._target_world = location.copy()
            self._previous_world = location.copy()
            self._previous_time = time.perf_counter()
            self._send_drag_target()
            return True

        def _update_target(self, event):
            origin, direction = self._ray(event)
            self._target_world = origin + direction * self._depth

        def _send_drag_target(self):
            """Send cursor target in metres; sidecar owns the PD spring."""
            if not self._dragging or not self._body or self._target_world is None:
                return
            paused = bool(cadex_live.status().get("paused"))
            cadex_live.set_drag_target(
                self._body,
                (
                    float(self._target_world.x) * 0.001,
                    float(self._target_world.y) * 0.001,
                    float(self._target_world.z) * 0.001,
                ),
                stiffness=(
                    SPRING_PAUSED_N_PER_M if paused else SPRING_N_PER_M
                ),
                damping=(
                    DAMPING_PAUSED_NS_PER_M if paused else DAMPING_NS_PER_M
                ),
                max_force=(
                    MAX_FORCE_PAUSED_N if paused else MAX_FORCE_N
                ),
            )

        def modal(self, context, event):
            if not cadex_live.is_running():
                return self._finish(context, cancelled=True)
            if event.type in {'RIGHTMOUSE', 'ESC'}:
                return self._finish(context, cancelled=True)
            if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                self._begin_drag(context, event)
                return {'RUNNING_MODAL'}
            if event.type == 'MOUSEMOVE' and self._dragging:
                self._update_target(event)
                self._send_drag_target()
                return {'RUNNING_MODAL'}
            if event.type == 'TIMER' and self._dragging:
                # Keep the target fresh even if the mouse is still.
                self._send_drag_target()
                return {'RUNNING_MODAL'}
            if event.type == 'LEFTMOUSE' and event.value == 'RELEASE' \
                    and self._dragging:
                # Stay in drag mode for the next grab.
                self._end_grab()
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

    return MESH_AGENT_OT_live_drag


_operator = None


def register():
    global _operator
    import bpy
    _operator = _make_operator()
    bpy.utils.register_class(_operator)


def unregister():
    global _operator
    cadex_live.clear_forces()
    if _operator is not None:
        import bpy
        try:
            bpy.utils.unregister_class(_operator)
        except RuntimeError:
            pass
    _operator = None
