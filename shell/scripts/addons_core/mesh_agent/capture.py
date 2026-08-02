# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Viewport capture for the agent: offscreen render, downscaled, base64 PNG."""

import base64
import os
import tempfile

# Open Grok drops MCP images that fail integrity / size checks
# ("image bytes are truncated"). Keep payloads small and well-formed.
_DEFAULT_MAX_EDGE = 480
_HARD_MAX_EDGE = 640
# ~150 KiB raw PNG ≈ ~200 KiB base64 — under typical MCP tool-result caps.
_MAX_PNG_BYTES = 150_000


def _find_view3d():
    import bpy

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return window, area, area.spaces.active, region
    return None, None, None, None


def _png_is_complete(data):
    """True if bytes look like a finished PNG (signature + IEND)."""
    if not data or len(data) < 24:
        return False
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    # IEND chunk type + CRC at end of a valid file.
    return b"IEND" in data[-32:]


def _encode_png_base64(data):
    if not _png_is_complete(data):
        return None, "Captured image was incomplete (truncated PNG)."
    if len(data) > _MAX_PNG_BYTES:
        return None, (
            "Captured image is too large for the chat pipeline "
            "({:d} bytes).".format(len(data))
        )
    return base64.b64encode(data).decode("ascii"), None


def _save_image_png_bytes(image, path):
    image.filepath_raw = path
    image.file_format = 'PNG'
    # Prefer smaller files when the Blender version supports compression.
    try:
        image.file_format = 'PNG'
        if hasattr(image, "filepath_raw"):
            pass
    except Exception:
        pass
    image.save()
    with open(path, "rb") as file:
        return file.read()


def _downscale_until_fit(image, max_size, path):
    """Save PNG; if still too big, shrink longest edge and retry."""
    width, height = image.size
    if width < 1 or height < 1:
        return None, "Empty capture."
    edge = max(width, height)
    target = min(int(max_size), _HARD_MAX_EDGE)
    if edge > target:
        scale = target / float(edge)
        image.scale(max(8, int(width * scale)), max(8, int(height * scale)))

    for _attempt in range(6):
        data = _save_image_png_bytes(image, path)
        if _png_is_complete(data) and len(data) <= _MAX_PNG_BYTES:
            return data, None
        # Shrink more and try again.
        w, h = image.size
        if max(w, h) <= 64:
            if _png_is_complete(data):
                # Still oversized at tiny resolution — return anyway only if
                # complete; caller may still reject on size.
                return data, None
            return None, "Could not produce a small enough viewport PNG."
        image.scale(max(8, int(w * 0.7)), max(8, int(h * 0.7)))
    return None, "Could not produce a small enough viewport PNG."


def image_file_png_base64(path, max_size=1024):
    """Load an image file, downscale it if needed, and return it as
    (base64_png, None) or (None, error_message). Works headless."""
    import bpy

    if not path or not os.path.isfile(path):
        return None, "Image file not found: {!s}".format(path)
    try:
        image = bpy.data.images.load(path)
    except RuntimeError as ex:
        return None, "Could not load image: {!s}".format(ex)
    try:
        width, height = image.size
        if width == 0 or height == 0:
            return None, "Unreadable image: {!s}".format(path)
        tmp = os.path.join(tempfile.gettempdir(), "mesh_agent_attach_tmp.png")
        data, err = _downscale_until_fit(
            image, min(int(max_size), _HARD_MAX_EDGE), tmp)
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        if data is None:
            return None, err or "Could not encode image."
        return _encode_png_base64(data)
    finally:
        bpy.data.images.remove(image)


def screenshot_png_base64(max_size=None):
    """Return (base64_png, None) or (None, error_message)."""
    import bpy

    if bpy.app.background:
        return None, ("Viewport capture is unavailable in background mode; "
                      "use scene_summary instead.")

    if max_size is None:
        max_size = _DEFAULT_MAX_EDGE
    try:
        max_size = int(max_size)
    except (TypeError, ValueError):
        max_size = _DEFAULT_MAX_EDGE
    max_size = max(64, min(max_size, _HARD_MAX_EDGE))

    window, area, space, region = _find_view3d()
    if space is None:
        return None, "No 3D viewport found; use scene_summary instead."

    import gpu

    scale = min(1.0, max_size / max(region.width, region.height, 1))
    width = max(8, int(region.width * scale))
    height = max(8, int(region.height * scale))

    region_3d = space.region_3d
    offscreen = gpu.types.GPUOffScreen(width, height)
    try:
        offscreen.draw_view3d(
            bpy.context.scene,
            bpy.context.view_layer,
            space,
            region,
            region_3d.view_matrix,
            region_3d.window_matrix,
            do_color_management=True,
        )
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            pixel_buffer = framebuffer.read_color(
                0, 0, width, height, 4, 0, 'FLOAT')
        pixel_buffer.dimensions = width * height * 4
        pixels = [value for value in pixel_buffer]
    finally:
        offscreen.free()

    image = bpy.data.images.new(
        "mesh_agent_capture", width, height, alpha=True)
    try:
        image.pixels.foreach_set(pixels)
        path = os.path.join(tempfile.gettempdir(), "mesh_agent_capture.png")
        data, err = _downscale_until_fit(image, max_size, path)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        if data is None:
            return None, err or "Viewport capture failed."
        return _encode_png_base64(data)
    finally:
        bpy.data.images.remove(image)
