"""Standalone realtime MuJoCo stepper for Cadex Live.

This module is installed with the engine payload but is not imported by
``cadexd``.  The Blender shell launches it with the payload's Python and talks
NDJSON over one localhost TCP connection.  MuJoCo therefore remains wholly
outside the shell process.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
import traceback
from typing import Any, Mapping, Sequence


SCHEMA = "cadex-live-v1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_SUBSTEPS = 10000


def _finite_vector(value: Any, size: int, name: str) -> list[float]:
    if (isinstance(value, (str, bytes))
            or not hasattr(value, "__len__")
            or len(value) != size):
        raise ValueError(f"{name} must contain {size} finite numbers")
    result = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{name} must contain {size} finite numbers")
        try:
            number = float(item)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{name} must contain {size} finite numbers"
            ) from error
        if not math.isfinite(number):
            raise ValueError(f"{name} must contain {size} finite numbers")
        result.append(number)
    return result


def pose_payload(
    position_m: Sequence[float], quaternion_wxyz: Sequence[float]
) -> dict[str, list[float]]:
    """MuJoCo world pose to the shell's millimetre + xyzw contract."""

    position = _finite_vector(position_m, 3, "position")
    quaternion = _finite_vector(quaternion_wxyz, 4, "quaternion")
    magnitude = math.sqrt(sum(component * component for component in quaternion))
    if magnitude <= 1.0e-12:
        raise ValueError("quaternion must be non-zero")
    w_value, x_value, y_value, z_value = (
        component / magnitude for component in quaternion
    )
    return {
        "position_mm": [component * 1000.0 for component in position],
        "rotation_xyzw": [x_value, y_value, z_value, w_value],
    }


def step_budget(
    accumulator_s: float,
    timestep_s: float,
    display_interval_s: float,
    realtime_scale: float,
) -> tuple[int, float]:
    """Whole MuJoCo steps for one display tick, retaining fractional time."""

    timestep = float(timestep_s)
    interval = float(display_interval_s)
    scale = float(realtime_scale)
    if timestep <= 0.0 or interval <= 0.0 or scale <= 0.0:
        raise ValueError("timestep, display interval and realtime scale must be positive")
    accumulated = max(0.0, float(accumulator_s)) + interval * scale
    steps = int(math.floor((accumulated + timestep * 1.0e-9) / timestep))
    if steps > MAX_SUBSTEPS:
        raise ValueError("display tick exceeds the live substep budget")
    return steps, max(0.0, accumulated - steps * timestep)


class LiveStepper:
    """One loaded MJCF model and its interactive state."""

    def __init__(self, xml_path: str, display_hz: float = 60.0) -> None:
        import mujoco

        self.mujoco = mujoco
        self.xml_path = str(xml_path)
        self.display_hz = float(display_hz)
        if not math.isfinite(self.display_hz) or self.display_hz <= 0.0:
            raise ValueError("display_hz must be positive")
        self.display_interval_s = 1.0 / self.display_hz
        self.paused = False
        self.realtime_scale = 1.0
        self._accumulator_s = 0.0
        self._forces: dict[str, dict[str, Any]] = {}
        # Cursor spring evaluated each substep from true body state (no UI lag).
        self._drag: dict[str, Any] | None = None
        self._pending_message: dict[str, Any] | None = None
        (self.model, self.data, self.keyframe_id,
         self.body_ids, self.dynamic_bodies) = self._load_model(self.xml_path)
        self.reset()

    def _load_model(self, xml_path: str):
        path = str(xml_path)
        model = self.mujoco.MjModel.from_xml_path(path)
        data = self.mujoco.MjData(model)
        keyframe_id = int(
            self.mujoco.mj_name2id(
                model, self.mujoco.mjtObj.mjOBJ_KEY, "solved"
            )
        )
        if keyframe_id < 0:
            raise ValueError("MJCF has no keyframe named 'solved'")
        body_ids: dict[str, int] = {}
        dynamic_bodies: list[str] = []
        for body_id in range(1, int(model.nbody)):
            name = self.mujoco.mj_id2name(
                model, self.mujoco.mjtObj.mjOBJ_BODY, body_id
            )
            if not name:
                continue
            clean = str(name)
            body_ids[clean] = body_id
            if int(model.body_dofnum[body_id]) > 0:
                dynamic_bodies.append(clean)
        return model, data, keyframe_id, body_ids, dynamic_bodies

    def _reset_data(self, model, data, keyframe_id: int) -> None:
        self.mujoco.mj_resetDataKeyframe(model, data, keyframe_id)
        if int(model.nu):
            data.ctrl[:] = 0.0
        data.qfrc_applied[:] = 0.0
        data.xfrc_applied[:] = 0.0
        self.mujoco.mj_forward(model, data)

    @property
    def bodies(self) -> list[str]:
        return list(self.body_ids)

    def reset(self) -> None:
        self._reset_data(self.model, self.data, self.keyframe_id)
        self._forces.clear()
        self._drag = None
        self._accumulator_s = 0.0

    def reload_xml(
        self,
        xml_path: str,
        preserve_state: bool = True,
        request_id: int = 0,
    ) -> dict[str, Any]:
        """Atomically load MJCF and preserve generalized state when compatible."""

        path = str(xml_path)
        old_qpos = [float(value) for value in self.data.qpos]
        old_qvel = [float(value) for value in self.data.qvel]
        old_ctrl = [float(value) for value in self.data.ctrl]
        old_time = float(self.data.time)
        model, data, keyframe_id, body_ids, dynamic_bodies = self._load_model(path)
        self._reset_data(model, data, keyframe_id)
        preserved = bool(
            preserve_state
            and int(model.nq) == len(old_qpos)
            and int(model.nv) == len(old_qvel)
        )
        if preserved:
            if int(model.nq):
                data.qpos[:] = old_qpos
            if int(model.nv):
                data.qvel[:] = old_qvel
            if int(model.nu) == len(old_ctrl) and int(model.nu):
                data.ctrl[:] = old_ctrl
            data.time = old_time
            self.mujoco.mj_forward(model, data)

        self.xml_path = path
        self.model = model
        self.data = data
        self.keyframe_id = keyframe_id
        self.body_ids = body_ids
        self.dynamic_bodies = dynamic_bodies
        self._forces.clear()
        self._drag = None
        self._accumulator_s = 0.0
        message = (
            "Live model reloaded; motion preserved."
            if preserved else
            "Live model structure changed; reset to the solved keyframe."
        )
        return {
            "schema": SCHEMA,
            "type": "reloaded",
            "request_id": int(request_id),
            "preserved": preserved,
            "nq": int(model.nq),
            "nv": int(model.nv),
            "bodies": self.bodies,
            "dynamic_bodies": list(self.dynamic_bodies),
            "model": self.xml_path,
            "t": float(self.data.time),
            "message": message,
        }

    def take_command_message(self) -> dict[str, Any] | None:
        message = self._pending_message
        self._pending_message = None
        return message

    def ready_message(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "type": "ready",
            "bodies": self.bodies,
            "dynamic_bodies": list(self.dynamic_bodies),
            "timestep_s": float(self.model.opt.timestep),
            "display_hz": self.display_hz,
            "model": self.xml_path,
            "nq": int(self.model.nq),
            "nv": int(self.model.nv),
            "realtime_scale": float(self.realtime_scale),
        }

    def state_message(self) -> dict[str, Any]:
        placements = {
            name: pose_payload(
                self.data.xpos[body_id], self.data.xquat[body_id]
            )
            for name, body_id in self.body_ids.items()
        }
        return {
            "schema": SCHEMA,
            "type": "state",
            "t": float(self.data.time),
            "paused": bool(self.paused),
            "realtime_scale": float(self.realtime_scale),
            "placements": placements,
        }

    def _body_linear_velocity(self, body_id: int) -> list[float]:
        """World-frame linear velocity of a body (m/s)."""

        import numpy

        residual = numpy.zeros(6, dtype=float)
        # MuJoCo packs spatial velocity as [angular(3), linear(3)] when flg_local=0.
        self.mujoco.mj_objectVelocity(
            self.model,
            self.data,
            self.mujoco.mjtObj.mjOBJ_BODY,
            body_id,
            residual,
            0,
        )
        return [float(residual[3]), float(residual[4]), float(residual[5])]

    def _apply_drag_force(self) -> None:
        """PD spring to the cursor using *current* MuJoCo state (stable drag)."""

        drag = self._drag
        if not drag:
            return
        body_id = self.body_ids.get(str(drag.get("body") or ""))
        if body_id is None:
            return
        target = drag["target_m"]
        # Attachment follows the body COM for stability (point forces on thin
        # links with laggy UI feedback were a major source of shaking).
        position = [
            float(self.data.xpos[body_id][0]),
            float(self.data.xpos[body_id][1]),
            float(self.data.xpos[body_id][2]),
        ]
        velocity = self._body_linear_velocity(body_id)
        stiffness = float(drag["stiffness"])
        damping = float(drag["damping"])
        force_cap = float(drag["max_force"])
        force = [
            (target[axis] - position[axis]) * stiffness - velocity[axis] * damping
            for axis in range(3)
        ]
        magnitude = math.sqrt(sum(component * component for component in force))
        if magnitude > force_cap > 0.0:
            scale = force_cap / magnitude
            force = [component * scale for component in force]
        self.data.xfrc_applied[body_id, :3] = force
        self.data.xfrc_applied[body_id, 3:] = 0.0

    def _apply_forces(self) -> None:
        self.data.qfrc_applied[:] = 0.0
        self.data.xfrc_applied[:] = 0.0
        for name, record in self._forces.items():
            body_id = self.body_ids.get(name)
            if body_id is None:
                continue
            force = record["force_N"]
            torque = record["torque_Nm"]
            point = record.get("point_m")
            if point is None:
                self.data.xfrc_applied[body_id, :3] = force
                self.data.xfrc_applied[body_id, 3:] = torque
            else:
                self.mujoco.mj_applyFT(
                    self.model,
                    self.data,
                    force,
                    torque,
                    point,
                    body_id,
                    self.data.qfrc_applied,
                )
        self._apply_drag_force()

    def _hold_pose(self) -> None:
        """Zero velocities after a paused pose edit so the freeze sticks."""

        if int(self.model.nv):
            self.data.qvel[:] = 0.0
        self.data.qfrc_applied[:] = 0.0
        self.data.xfrc_applied[:] = 0.0
        self.mujoco.mj_forward(self.model, self.data)

    def _user_wrench_active(self) -> bool:
        return bool(self._forces) or self._drag is not None

    def step_display_interval(self) -> None:
        # Paused with no user wrench: freeze. Paused *with* drag/force:
        # quasi-static pose edit under joint constraints (gravity off).
        posing = bool(self.paused and self._user_wrench_active())
        if self.paused and not posing:
            return

        gravity = None
        if posing:
            gravity = [float(value) for value in self.model.opt.gravity]
            self.model.opt.gravity[:] = 0.0

        try:
            scale = self.realtime_scale
            if posing:
                # Spend more substeps so hinge chains settle toward the cursor.
                scale = max(float(scale), 4.0)
            steps, self._accumulator_s = step_budget(
                self._accumulator_s,
                float(self.model.opt.timestep),
                self.display_interval_s,
                scale,
            )
            if posing:
                min_steps = max(
                    1,
                    int(round(0.03 / max(float(self.model.opt.timestep), 1.0e-6))),
                )
                steps = max(steps, min_steps)
            for _index in range(steps):
                self._apply_forces()
                self.mujoco.mj_step(self.model, self.data)
                if posing and int(self.model.nv):
                    # Near-kinematic settle: kill residual oscillation each step.
                    self.data.qvel[:] *= 0.35
            if posing:
                self._hold_pose()
        finally:
            if gravity is not None:
                self.model.opt.gravity[:] = gravity

    def apply_command(self, message: Mapping[str, Any]) -> bool:
        kind = str(message.get("type") or "")
        if kind == "pause":
            self.paused = True
            self._hold_pose()
        elif kind == "resume":
            self.paused = False
            self._accumulator_s = 0.0
        elif kind == "reset":
            self.reset()
        elif kind == "set_realtime":
            scale = float(message.get("scale"))
            if not math.isfinite(scale) or not 0.05 <= scale <= 4.0:
                raise ValueError("realtime scale must be between 0.05 and 4")
            self.realtime_scale = scale
        elif kind == "reload":
            xml_path = str(message.get("xml") or "")
            if not xml_path:
                raise ValueError("reload requires an MJCF path")
            self._pending_message = self.reload_xml(
                xml_path,
                preserve_state=bool(message.get("preserve_state", True)),
                request_id=int(message.get("request_id") or 0),
            )
        elif kind == "set_ctrl":
            values = _finite_vector(message.get("values"), int(self.model.nu), "values")
            if int(self.model.nu):
                self.data.ctrl[:] = values
        elif kind == "apply_force":
            body = str(message.get("body") or "")
            if body not in self.dynamic_bodies:
                raise ValueError(f"body {body!r} is not dynamic")
            point = message.get("point_m")
            self._forces[body] = {
                "force_N": _finite_vector(message.get("force_N"), 3, "force_N"),
                "torque_Nm": _finite_vector(
                    message.get("torque_Nm", [0.0, 0.0, 0.0]),
                    3,
                    "torque_Nm",
                ),
                "point_m": (
                    None
                    if point is None
                    else _finite_vector(point, 3, "point_m")
                ),
            }
        elif kind == "set_drag_target":
            # Cursor spring: re-evaluated every mj_step from true xpos/xvel.
            body = str(message.get("body") or "")
            if body not in self.dynamic_bodies:
                raise ValueError(f"body {body!r} is not dynamic")
            stiffness = float(message.get("stiffness", 40.0))
            damping = float(message.get("damping", 12.0))
            max_force = float(message.get("max_force", 25.0))
            if not math.isfinite(stiffness) or stiffness < 0.0:
                raise ValueError("stiffness must be a non-negative finite number")
            if not math.isfinite(damping) or damping < 0.0:
                raise ValueError("damping must be a non-negative finite number")
            if not math.isfinite(max_force) or max_force <= 0.0:
                raise ValueError("max_force must be a positive finite number")
            self._drag = {
                "body": body,
                "target_m": _finite_vector(message.get("target_m"), 3, "target_m"),
                "stiffness": stiffness,
                "damping": damping,
                "max_force": max_force,
            }
        elif kind == "clear_drag":
            self._drag = None
            if self.paused and not self._forces:
                self._hold_pose()
        elif kind == "clear_forces":
            self._forces.clear()
            self._drag = None
            self.data.qfrc_applied[:] = 0.0
            self.data.xfrc_applied[:] = 0.0
            if self.paused:
                self._hold_pose()
        elif kind == "shutdown":
            return False
        else:
            raise ValueError(f"unknown live command {kind!r}")
        return True


def _encode(message: Mapping[str, Any]) -> bytes:
    payload = json.dumps(
        dict(message), ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("live protocol frame exceeds 1 MB")
    return payload + b"\n"


def _send(connection: socket.socket, message: Mapping[str, Any]) -> None:
    connection.sendall(_encode(message))


def _receive(
    connection: socket.socket, buffer: bytearray
) -> tuple[list[dict[str, Any]], bool]:
    connected = True
    while True:
        try:
            chunk = connection.recv(65536)
        except BlockingIOError:
            break
        if not chunk:
            connected = False
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_FRAME_BYTES:
            raise ValueError("live protocol input exceeds 1 MB")

    messages = []
    while True:
        newline = buffer.find(b"\n")
        if newline < 0:
            break
        raw = bytes(buffer[:newline]).strip()
        del buffer[:newline + 1]
        if not raw:
            continue
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("live protocol message must be an object")
        messages.append(decoded)
    return messages, connected


def serve(xml_path: str, port: int = 0, display_hz: float = 60.0) -> int:
    try:
        stepper = LiveStepper(xml_path, display_hz=display_hz)
    except Exception as error:
        print(
            json.dumps({"ready": False, "error": str(error)}),
            flush=True,
        )
        traceback.print_exc(file=sys.stderr)
        return 2

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", int(port)))
    listener.listen(1)
    listener.settimeout(15.0)
    actual_port = int(listener.getsockname()[1])
    print(
        json.dumps({"ready": True, "port": actual_port, "schema": SCHEMA}),
        flush=True,
    )

    try:
        connection, _address = listener.accept()
    except socket.timeout:
        return 3
    finally:
        listener.close()

    connection.setblocking(False)
    buffer = bytearray()
    running = True
    next_frame = time.perf_counter()
    try:
        _send(connection, stepper.ready_message())
        while running:
            try:
                messages, connected = _receive(connection, buffer)
            except (ValueError, json.JSONDecodeError) as error:
                _send(
                    connection,
                    {"schema": SCHEMA, "type": "error", "message": str(error)},
                )
                buffer.clear()
                messages, connected = [], True
            if not connected:
                break
            for message in messages:
                try:
                    running = stepper.apply_command(message)
                    response = stepper.take_command_message()
                    if response is not None:
                        _send(connection, response)
                except Exception as error:
                    _send(
                        connection,
                        {
                            "schema": SCHEMA,
                            "type": "error",
                            "message": str(error),
                            "request_id": int(message.get("request_id") or 0),
                        },
                    )
                if not running:
                    break
            if not running:
                break

            now = time.perf_counter()
            if now < next_frame:
                time.sleep(min(next_frame - now, 0.005))
                continue
            stepper.step_display_interval()
            _send(connection, stepper.state_message())
            next_frame += stepper.display_interval_s
            if now - next_frame > 0.25:
                next_frame = now + stepper.display_interval_s
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        try:
            connection.close()
        except OSError:
            pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cadex Live MuJoCo sidecar")
    parser.add_argument("--xml", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--display-hz", type=float, default=60.0)
    arguments = parser.parse_args(argv)
    return serve(
        arguments.xml, port=arguments.port, display_hz=arguments.display_hz
    )


if __name__ == "__main__":
    raise SystemExit(main())
