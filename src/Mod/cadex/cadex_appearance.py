# SPDX-License-Identifier: LGPL-2.1-or-later

"""Display appearance helpers for Cadex xscript (per-output paint).

Colors are **display-only**: they travel with DomainValue properties into the
lifecycle ``display`` map and Blender hydrate. They do not revive the deleted
FreeCAD Material catalog domain (ADR-006 / ADR-010).
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping


_HEX_RE = re.compile(
    r"^#?"
    r"([0-9a-fA-F]{2})"
    r"([0-9a-fA-F]{2})"
    r"([0-9a-fA-F]{2})"
    r"([0-9a-fA-F]{2})?$"
)


def normalize_color(value: Any, *, operation: str = "color") -> tuple[float, float, float, float]:
    """Normalize a color to sRGB floats in ``[0, 1]`` as ``(r, g, b, a)``.

    Accepts:

    - ``(r, g, b)`` or ``(r, g, b, a)`` with components in ``[0, 1]`` or ``[0, 255]``
    - ``"#rrggbb"`` / ``"#rrggbbaa"`` / without ``#``
    """

    if isinstance(value, str):
        text = value.strip()
        match = _HEX_RE.match(text)
        if not match:
            raise ValueError(
                f"api.{operation}: invalid color: expected #RRGGBB or #RRGGBBAA. "
                f"Received {value!r}."
            )
        channels = [int(match.group(i), 16) / 255.0 for i in range(1, 4)]
        alpha = int(match.group(4), 16) / 255.0 if match.group(4) else 1.0
        return (channels[0], channels[1], channels[2], alpha)

    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ValueError(
            f"api.{operation}: invalid color: expected [r, g, b] or [r, g, b, a] "
            f"or a hex string. Received {value!r}."
        )

    raw = list(value)
    has_alpha = len(raw) == 4
    channels = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(
                f"api.{operation}: invalid color: channel {index} must be a number. "
                f"Received {value!r}."
            )
        channel = float(item)
        if not math.isfinite(channel):
            raise ValueError(
                f"api.{operation}: invalid color: channel {index} must be finite. "
                f"Received {value!r}."
            )
        channels.append(channel)

    # Byte mode only when every RGB channel is in [0, 255] and at least one > 1.
    rgb = channels[:3]
    as_bytes = any(c > 1.0 + 1.0e-9 for c in rgb)
    if as_bytes:
        for index, channel in enumerate(rgb):
            if channel < 0.0 or channel > 255.0:
                raise ValueError(
                    f"api.{operation}: invalid color: 0–255 channel {index} out of range. "
                    f"Received {value!r}."
                )
        out_rgb = [c / 255.0 for c in rgb]
    else:
        for index, channel in enumerate(rgb):
            if channel < 0.0 or channel > 1.0:
                raise ValueError(
                    f"api.{operation}: invalid color: channel {index} must be in [0, 1]. "
                    f"Received {value!r}."
                )
        out_rgb = list(rgb)

    if has_alpha:
        alpha = channels[3]
        if as_bytes and alpha > 1.0 + 1.0e-9:
            if alpha < 0.0 or alpha > 255.0:
                raise ValueError(
                    f"api.{operation}: invalid color: alpha out of 0–255 range. "
                    f"Received {value!r}."
                )
            alpha = alpha / 255.0
        elif alpha < 0.0 or alpha > 1.0:
            raise ValueError(
                f"api.{operation}: invalid color: alpha must be in [0, 1]. "
                f"Received {value!r}."
            )
    else:
        alpha = 1.0
    return (out_rgb[0], out_rgb[1], out_rgb[2], alpha)


def appearance_from_color(value: Any, *, operation: str = "color") -> dict[str, Any]:
    """Build the DomainValue ``appearance`` property from a whole-body color."""

    r, g, b, a = normalize_color(value, operation=operation)
    return {"diffuse": [r, g, b, a]}


def _normalize_face_map(
    faces: Any, *, operation: str = "paint_faces"
) -> dict[str, list[float]]:
    """Normalize ``{face_id: color}`` to string keys and diffuse lists."""

    if not isinstance(faces, Mapping) or not faces:
        raise ValueError(
            f"api.{operation}: invalid faces: expected a non-empty map of "
            f"1-based face id → color. Received {faces!r}."
        )
    out: dict[str, list[float]] = {}
    for key, color in faces.items():
        if isinstance(key, bool) or not isinstance(key, (int, str)):
            raise ValueError(
                f"api.{operation}: invalid faces: face id must be an int or "
                f"digit string. Received key {key!r}."
            )
        try:
            face_id = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"api.{operation}: invalid faces: face id must be an integer. "
                f"Received key {key!r}."
            ) from exc
        if face_id < 1:
            raise ValueError(
                f"api.{operation}: invalid faces: face ids are 1-based. "
                f"Received {face_id}."
            )
        r, g, b, a = normalize_color(color, operation=operation)
        out[str(face_id)] = [r, g, b, a]
    return out


def merge_face_colors(
    base: Mapping[str, Any] | None,
    face_colors: Mapping[str, list[float]],
    *,
    default_color: Any = None,
    operation: str = "paint_faces",
) -> dict[str, Any]:
    """Merge face colors into an appearance dict (face keys overwrite)."""

    result: dict[str, Any] = {}
    if isinstance(base, Mapping):
        if isinstance(base.get("diffuse"), (list, tuple)):
            try:
                r, g, b, a = normalize_color(base["diffuse"], operation=operation)
                result["diffuse"] = [r, g, b, a]
            except ValueError:
                pass
        existing = base.get("faces")
        if isinstance(existing, Mapping):
            merged = {}
            for key, value in existing.items():
                try:
                    rid = str(int(key))
                    r, g, b, a = normalize_color(value, operation=operation)
                    merged[rid] = [r, g, b, a]
                except (TypeError, ValueError):
                    continue
            result["faces"] = merged
    if default_color is not None:
        r, g, b, a = normalize_color(default_color, operation=operation)
        result["diffuse"] = [r, g, b, a]
    faces = dict(result.get("faces") or {})
    faces.update(face_colors)
    result["faces"] = faces
    return result


def appearance_from_face_paint(
    *,
    faces: Any = None,
    color: Any = None,
    colors: Any = None,
    base: Mapping[str, Any] | None = None,
    operation: str = "paint_faces",
) -> dict[str, Any]:
    """Build appearance for ``paint_faces``.

    Forms:

    - ``faces=[1,2,3], color=(1,0,0)`` — paint those 1-based faces one color
    - ``colors={1: (1,0,0), 2: \"#0f0\"}`` — per-face map
    - both: map is applied first, then list overwrites those ids with ``color``
    """

    face_map: dict[str, list[float]] = {}
    if colors is not None:
        face_map.update(_normalize_face_map(colors, operation=operation))
    if faces is not None:
        if color is None:
            raise ValueError(
                f"api.{operation}: color= is required when faces= is a list of ids."
            )
        if not isinstance(faces, (list, tuple)) or not faces:
            raise ValueError(
                f"api.{operation}: faces= must be a non-empty list of 1-based face ids."
            )
        single = normalize_color(color, operation=operation)
        for item in faces:
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise ValueError(
                    f"api.{operation}: faces= entries must be integers. Received {item!r}."
                )
            face_id = int(item)
            if face_id < 1:
                raise ValueError(
                    f"api.{operation}: face ids are 1-based. Received {face_id}."
                )
            face_map[str(face_id)] = [single[0], single[1], single[2], single[3]]
    if not face_map:
        raise ValueError(
            f"api.{operation}: provide colors={{face: color}} and/or faces=[...] with color=."
        )
    return merge_face_colors(base, face_map, operation=operation)


def appearance_from_properties(properties: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a normalized appearance dict from DomainValue properties, or None."""

    if not isinstance(properties, Mapping):
        return None
    raw = properties.get("appearance")
    if not isinstance(raw, Mapping):
        return None
    result: dict[str, Any] = {}
    diffuse = raw.get("diffuse")
    if isinstance(diffuse, (list, tuple)) and len(diffuse) in (3, 4):
        try:
            r, g, b, a = normalize_color(diffuse, operation="appearance")
            result["diffuse"] = [r, g, b, a]
        except ValueError:
            pass
    faces = raw.get("faces")
    if isinstance(faces, Mapping) and faces:
        try:
            result["faces"] = _normalize_face_map(faces, operation="appearance")
        except ValueError:
            pass
    if not result:
        return None
    return result


def appearance_cache_key(appearance: Mapping[str, Any] | None) -> str:
    """Stable string for hydrate cache keys (body + face paints)."""

    if not appearance:
        return ""
    parts: list[str] = []
    diffuse = appearance.get("diffuse") if isinstance(appearance, Mapping) else None
    if isinstance(diffuse, (list, tuple)) and len(diffuse) >= 3:
        for index in range(4):
            value = float(diffuse[index]) if index < len(diffuse) else 1.0
            parts.append(f"d:{value:.6f}")
    faces = appearance.get("faces") if isinstance(appearance, Mapping) else None
    if isinstance(faces, Mapping) and faces:
        for key in sorted(faces.keys(), key=lambda k: int(k) if str(k).isdigit() else 0):
            color = faces[key]
            if not isinstance(color, (list, tuple)) or len(color) < 3:
                continue
            chunk = [f"{float(color[i]) if i < len(color) else 1.0:.6f}" for i in range(4)]
            parts.append(f"f{key}:" + ",".join(chunk))
    return "|".join(parts)
