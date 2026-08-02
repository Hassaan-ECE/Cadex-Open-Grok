"""Focused unit and stock-MuJoCo coverage for the Cadex Live sidecar."""

from __future__ import annotations

import math
from pathlib import Path
import socket

import pytest

import cadex_live_stepper as live


def test_pose_payload_converts_si_and_quaternion_order() -> None:
    pose = live.pose_payload([0.012, -0.003, 0.5], [2.0, 0.0, 0.0, 0.0])
    assert pose == {
        "position_mm": [12.0, -3.0, 500.0],
        "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
    }
    with pytest.raises(ValueError, match="finite"):
        live.pose_payload([math.nan, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="non-zero"):
        live.pose_payload([0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0])


def test_step_budget_retains_fractional_solver_time() -> None:
    steps, remainder = live.step_budget(0.0, 0.006, 0.01, 1.0)
    assert steps == 1
    assert remainder == pytest.approx(0.004)
    steps, remainder = live.step_budget(remainder, 0.006, 0.01, 1.0)
    assert steps == 2
    assert remainder == pytest.approx(0.002)


def test_ndjson_protocol_round_trip() -> None:
    sender, receiver = socket.socketpair()
    try:
        receiver.setblocking(False)
        sender.sendall(live._encode({"type": "pause", "schema": live.SCHEMA}))
        messages, connected = live._receive(receiver, bytearray())
    finally:
        sender.close()
        receiver.close()
    assert connected is True
    assert messages == [{"type": "pause", "schema": live.SCHEMA}]


def _write_hinge_model(path: Path) -> None:
    path.write_text(
        """<mujoco model="cadex-live-test">
  <option timestep="0.001" gravity="0 0 -9.81"/>
  <worldbody>
    <body name="base">
      <geom type="sphere" size="0.01" mass="0.1"/>
      <body name="link" pos="0 0 -0.1">
        <joint name="hinge" type="hinge" axis="0 1 0"/>
        <geom type="capsule" fromto="0 0 0 0 0 -0.2" size="0.01" mass="0.1"/>
      </body>
    </body>
  </worldbody>
  <keyframe><key name="solved" qpos="0.35"/></keyframe>
</mujoco>
""",
        encoding="utf-8",
    )


def test_stepper_loads_solved_keyframe_and_handles_controls(tmp_path: Path) -> None:
    pytest.importorskip("mujoco")
    model_path = tmp_path / "live.xml"
    _write_hinge_model(model_path)
    stepper = live.LiveStepper(str(model_path), display_hz=50.0)

    ready = stepper.ready_message()
    assert ready["schema"] == live.SCHEMA
    assert ready["bodies"] == ["base", "link"]
    assert ready["dynamic_bodies"] == ["link"]
    initial = stepper.state_message()
    assert initial["t"] == pytest.approx(0.0)

    for _index in range(10):
        stepper.step_display_interval()
    moved = stepper.state_message()
    assert moved["t"] == pytest.approx(0.2)
    initial_rotation = initial["placements"]["link"]["rotation_xyzw"]
    moved_rotation = moved["placements"]["link"]["rotation_xyzw"]
    assert max(abs(a - b) for a, b in zip(initial_rotation, moved_rotation)) > 0.001

    assert stepper.apply_command({"type": "pause"}) is True
    paused_at = stepper.state_message()["t"]
    stepper.step_display_interval()
    assert stepper.state_message()["t"] == pytest.approx(paused_at)
    assert stepper.apply_command({"type": "resume"}) is True
    assert stepper.apply_command({
        "type": "apply_force",
        "body": "link",
        "force_N": [1.0, 0.0, 0.0],
        "torque_Nm": [0.0, 0.0, 0.0],
        "point_m": None,
    }) is True
    stepper.step_display_interval()
    position = stepper.state_message()["placements"]["link"]["position_mm"]
    assert all(math.isfinite(value) for value in position)
    assert stepper.apply_command({"type": "clear_forces"}) is True
    assert stepper.apply_command({"type": "reset"}) is True
    assert stepper.state_message()["t"] == pytest.approx(0.0)


def test_paused_force_repositions_then_holds(tmp_path: Path) -> None:
    """While paused, forces still settle joints (zero-g); then the pose freezes."""

    pytest.importorskip("mujoco")
    model_path = tmp_path / "live-pose.xml"
    _write_hinge_model(model_path)
    stepper = live.LiveStepper(str(model_path), display_hz=50.0)
    start = stepper.state_message()["placements"]["link"]["rotation_xyzw"]

    assert stepper.apply_command({"type": "pause"}) is True
    # Pure pause with no force must not advance time or drift.
    frozen_t = stepper.state_message()["t"]
    stepper.step_display_interval()
    assert stepper.state_message()["t"] == pytest.approx(frozen_t)

    assert stepper.apply_command({
        "type": "apply_force",
        "body": "link",
        "force_N": [8.0, 0.0, 0.0],
        "torque_Nm": [0.0, 0.0, 0.0],
        "point_m": None,
    }) is True
    for _index in range(12):
        stepper.step_display_interval()
    posed = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    assert max(abs(a - b) for a, b in zip(start, posed)) > 0.01

    assert stepper.apply_command({"type": "clear_forces"}) is True
    held = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    held_t = stepper.state_message()["t"]
    for _index in range(8):
        stepper.step_display_interval()
    still = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    assert max(abs(a - b) for a, b in zip(held, still)) < 1.0e-5
    # No forces while paused: time and pose stay put.
    assert stepper.state_message()["t"] == pytest.approx(held_t)


def test_drag_target_moves_without_exploding(tmp_path: Path) -> None:
    """Sidecar-side PD drag should move a hinge and stay finite (no shake blow-up)."""

    pytest.importorskip("mujoco")
    model_path = tmp_path / "live-drag.xml"
    _write_hinge_model(model_path)
    stepper = live.LiveStepper(str(model_path), display_hz=50.0)
    # Hinge body origin stays near the joint; orientation is the motion signal.
    start = stepper.state_message()["placements"]["link"]["rotation_xyzw"]

    assert stepper.apply_command({
        "type": "set_drag_target",
        "body": "link",
        "target_m": [0.2, 0.0, -0.1],
        "stiffness": 30.0,
        "damping": 15.0,
        "max_force": 20.0,
    }) is True
    for _index in range(40):
        stepper.step_display_interval()
    mid = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    assert all(math.isfinite(value) for value in mid)
    assert max(abs(a - b) for a, b in zip(start, mid)) > 0.05

    assert stepper.apply_command({"type": "clear_drag"}) is True
    assert stepper.apply_command({"type": "pause"}) is True
    # Paused pose-drag still moves under zero-g settle.
    before = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    assert stepper.apply_command({
        "type": "set_drag_target",
        "body": "link",
        "target_m": [-0.15, 0.0, -0.1],
        "stiffness": 50.0,
        "damping": 25.0,
        "max_force": 35.0,
    }) is True
    for _index in range(25):
        stepper.step_display_interval()
    after = stepper.state_message()["placements"]["link"]["rotation_xyzw"]
    assert all(math.isfinite(value) for value in after)
    assert max(abs(a - b) for a, b in zip(before, after)) > 0.02

