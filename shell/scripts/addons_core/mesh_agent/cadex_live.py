"""Cadex Live client: sidecar lifecycle, pose pump and shell bookkeeping.

The shell never imports MuJoCo.  It starts the engine payload's standalone
stepper, receives world poses over localhost NDJSON, and applies them to the
already-hydrated component objects on Blender's main thread.
"""

import json
import math
import os
import queue
import socket
import subprocess
import threading


LIVE_SCHEMA = "cadex-live-v1"
MJCF_ARTIFACT_KIND = "assembly_mjcf_xml"
DISPLAY_HZ = 60.0
START_TIMEOUT_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 5.0

_session = None
_session_lock = threading.RLock()
_last_error = ""


def pose_to_matrix(pose):
    """``{position_mm, rotation_xyzw}`` to one row-major 4x4 placement."""

    if not isinstance(pose, dict):
        return None
    position = pose.get("position_mm")
    rotation = pose.get("rotation_xyzw")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        return None
    if not isinstance(rotation, (list, tuple)) or len(rotation) != 4:
        return None
    try:
        position = [float(value) for value in position]
        x_value, y_value, z_value, w_value = (
            float(value) for value in rotation
        )
    except (TypeError, ValueError):
        return None
    values = position + [x_value, y_value, z_value, w_value]
    if any(not math.isfinite(value) for value in values):
        return None
    magnitude = math.sqrt(
        x_value * x_value + y_value * y_value
        + z_value * z_value + w_value * w_value
    )
    if magnitude <= 1.0e-12:
        return None
    x_value /= magnitude
    y_value /= magnitude
    z_value /= magnitude
    w_value /= magnitude
    xx_value = x_value * x_value
    yy_value = y_value * y_value
    zz_value = z_value * z_value
    xy_value = x_value * y_value
    xz_value = x_value * z_value
    yz_value = y_value * z_value
    wx_value = w_value * x_value
    wy_value = w_value * y_value
    wz_value = w_value * z_value
    return [
        1.0 - 2.0 * (yy_value + zz_value),
        2.0 * (xy_value - wz_value),
        2.0 * (xz_value + wy_value),
        position[0],
        2.0 * (xy_value + wz_value),
        1.0 - 2.0 * (xx_value + zz_value),
        2.0 * (yz_value - wx_value),
        position[1],
        2.0 * (xz_value - wy_value),
        2.0 * (yz_value + wx_value),
        1.0 - 2.0 * (xx_value + yy_value),
        position[2],
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def _project_manifest(root):
    path = os.path.join(str(root), "script.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def accepted_staging(root, manifest=None):
    """Absolute accepted-attempt staging directory, or ``""``."""

    manifest = manifest if isinstance(manifest, dict) else _project_manifest(root)
    attempt = manifest.get("accepted_attempt") or {}
    relative = str(attempt.get("staging") or "")
    if not relative:
        return ""
    path = os.path.abspath(os.path.join(str(root), *relative.replace("\\", "/").split("/")))
    return path if os.path.isdir(path) else ""


def mjcf_candidates(display_map, project_root, manifest=None):
    """Sorted ``[(output, absolute_xml_path)]`` from display and the store."""

    root = os.path.abspath(str(project_root))
    staging = accepted_staging(root, manifest=manifest)
    found = {}
    for name, entry in sorted((display_map or {}).items()):
        entry = entry or {}
        if str(entry.get("artifact_kind") or "") != MJCF_ARTIFACT_KIND:
            continue
        raw = str(entry.get("artifact_path") or "")
        paths = [raw]
        if raw and not os.path.isabs(raw):
            if staging:
                paths.append(os.path.join(staging, *raw.replace("\\", "/").split("/")))
            paths.append(os.path.join(root, *raw.replace("\\", "/").split("/")))
        for path in paths:
            absolute = os.path.abspath(path) if path else ""
            if absolute and os.path.isfile(absolute):
                found[str(name)] = absolute
                break

    if staging:
        outputs = os.path.join(staging, "outputs")
        try:
            names = sorted(os.listdir(outputs))
        except OSError:
            names = []
        for filename in names:
            if not filename.endswith("-model.xml"):
                continue
            path = os.path.join(outputs, filename)
            if not os.path.isfile(path):
                continue
            output = filename[:-len("-model.xml")]
            found.setdefault(output, os.path.abspath(path))
    return sorted(found.items())


def choose_mjcf(candidates):
    """One unambiguous candidate as ``(name, path, error)``."""

    candidates = list(candidates or ())
    if not candidates:
        return "", "", (
            "Live needs a published MJCF model. Add one assembly MJCF output "
            "to the project script, rebuild, then start Live again."
        )
    if len(candidates) == 1:
        name, path = candidates[0]
        return str(name), str(path), ""
    preferred = [item for item in candidates
                 if str(item[0]).lower() in {"live_model", "live", "model"}]
    if len(preferred) == 1:
        name, path = preferred[0]
        return str(name), str(path), ""
    return "", "", (
        "This project publishes more than one MJCF model ({:s}). Keep one or "
        "name the Live model 'live_model'.".format(
            ", ".join(str(item[0]) for item in candidates)
        )
    )


def discover_mjcf(scene):
    from . import cadex_backend

    root = cadex_backend.project_root(scene)
    accepted = cadex_backend.last_accepted(root)
    candidates = mjcf_candidates(
        accepted.get("display") or {}, root, manifest=_project_manifest(root)
    )
    name, path, error = choose_mjcf(candidates)
    return {
        "root": os.path.abspath(root),
        "output": name,
        "path": path,
        "error": error,
        "candidates": candidates,
    }


def has_mjcf(scene):
    try:
        return bool(discover_mjcf(scene)["path"])
    except Exception:
        return False


def has_mjcf_artifacts(scene):
    try:
        return bool(discover_mjcf(scene)["candidates"])
    except Exception:
        return False


def last_error():
    return _last_error


def _set_last_error(message):
    global _last_error
    _last_error = str(message or "")


class LiveSession:
    def __init__(self, scene, root, output, xml_path, process, connection,
                 suspended_actions):
        self.scene_name = str(scene.name)
        self.root = os.path.abspath(str(root))
        self.output = str(output)
        self.xml_path = os.path.abspath(str(xml_path))
        self.process = process
        self.connection = connection
        self.suspended_actions = list(suspended_actions or [])
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.ready_event = threading.Event()
        self.reader_done = threading.Event()
        self.ready = {}
        self.latest_state = None
        self.state_sequence = 0
        self.applied_sequence = 0
        self.error = ""
        self.time_s = 0.0
        self.paused = False
        self.bodies = []
        self.dynamic_bodies = []

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def send(self, message):
        payload = json.dumps(
            dict(message), ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        with self.send_lock:
            self.connection.sendall(payload)


def _reader(session):
    try:
        stream = session.connection.makefile("rb")
        for raw in stream:
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            kind = str(message.get("type") or "")
            with session.lock:
                if kind == "ready":
                    session.ready = dict(message)
                    session.bodies = [str(name) for name in message.get("bodies") or []]
                    session.dynamic_bodies = [
                        str(name) for name in message.get("dynamic_bodies") or []
                    ]
                    session.ready_event.set()
                elif kind == "state":
                    session.latest_state = dict(message)
                    session.state_sequence += 1
                elif kind == "error":
                    session.error = str(message.get("message") or "Live sidecar error")
    except OSError as error:
        with session.lock:
            if session.alive():
                session.error = "Live connection failed: {!s}".format(error)
    finally:
        session.ready_event.set()
        session.reader_done.set()


def _read_start_banner(process):
    frames = queue.Queue(maxsize=1)

    def read_one():
        try:
            frames.put(process.stdout.readline())
        except Exception:
            frames.put(b"")

    threading.Thread(target=read_one, name="cadex-live-start", daemon=True).start()
    try:
        raw = frames.get(timeout=START_TIMEOUT_SECONDS)
    except queue.Empty:
        raise RuntimeError("Live sidecar did not become ready in time")
    if not raw:
        raise RuntimeError("Live sidecar exited before its ready banner")
    try:
        banner = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise RuntimeError("Live sidecar emitted an invalid ready banner") from error
    if not isinstance(banner, dict) or banner.get("ready") is not True:
        raise RuntimeError(str((banner or {}).get("error") or "Live sidecar failed"))
    port = int(banner.get("port") or 0)
    if not 1 <= port <= 65535:
        raise RuntimeError("Live sidecar returned an invalid localhost port")
    return port


def _process_error(process):
    try:
        if process.stderr is not None and process.poll() is not None:
            raw = process.stderr.read() or b""
            text = raw.decode("utf-8", errors="replace").strip()
            if text:
                return text.splitlines()[-1]
    except OSError:
        pass
    return ""


def _close_process(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _engine_command():
    from . import cadex_backend
    from . import cadexd_client

    freecadcmd, module_dir = cadex_backend.resolved_engine()
    if not freecadcmd or not module_dir:
        ok, reason, remedy = cadex_backend.preflight()
        raise RuntimeError((reason + " " + remedy).strip() if not ok else
                           "Cadex engine is unavailable")
    python = cadexd_client.engine_python(freecadcmd)
    if not python:
        raise RuntimeError("The Cadex engine payload has no Python executable")
    stepper = os.path.join(module_dir, "cadex_live_stepper.py")
    if not os.path.isfile(stepper):
        raise RuntimeError("The Cadex engine payload has no Live stepper module")
    return freecadcmd, python, stepper


def _tag_redraw():
    try:
        import bpy
        windows = bpy.context.window_manager.windows
    except Exception:
        return
    for window in windows:
        for area in window.screen.areas:
            if area.type in {'VIEW_3D', 'CADEX_PARAMS'}:
                area.tag_redraw()


def _register_pump():
    import bpy
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=0.0, persistent=True)


def start(scene, xml_path=None):
    """Start Live for ``scene``. Returns ``(ok, report)``."""

    global _session
    from . import cadex_animate
    from . import cadexd_client

    stop(restore=True)
    discovered = discover_mjcf(scene)
    if xml_path:
        discovered["path"] = os.path.abspath(str(xml_path))
        discovered["error"] = "" if os.path.isfile(discovered["path"]) else (
            "Live MJCF does not exist: " + discovered["path"]
        )
    if not discovered["path"]:
        _set_last_error(discovered["error"])
        return False, discovered["error"]

    try:
        freecadcmd, python, stepper = _engine_command()
    except RuntimeError as error:
        _set_last_error(str(error))
        return False, str(error)

    suspended = cadex_animate.suspend_for_live()
    command = [
        python,
        stepper,
        "--xml",
        discovered["path"],
        "--port",
        "0",
        "--display-hz",
        str(DISPLAY_HZ),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    process = None
    connection = None
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=discovered["root"],
            env=cadexd_client.engine_child_env(freecadcmd),
            creationflags=creationflags,
        )
        port = _read_start_banner(process)
        connection = socket.create_connection(
            ("127.0.0.1", port), timeout=CONNECT_TIMEOUT_SECONDS
        )
        connection.settimeout(None)
        session = LiveSession(
            scene,
            discovered["root"],
            discovered["output"],
            discovered["path"],
            process,
            connection,
            suspended,
        )
        threading.Thread(
            target=_reader, args=(session,), name="cadex-live-reader", daemon=True
        ).start()
        if not session.ready_event.wait(CONNECT_TIMEOUT_SECONDS):
            raise RuntimeError("Live sidecar connected but sent no ready state")
        if not session.ready:
            raise RuntimeError(session.error or "Live sidecar connection closed")
    except Exception as error:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        detail = _process_error(process)
        _close_process(process)
        cadex_animate.restore_after_live(suspended)
        report = str(error)
        if detail and detail not in report:
            report = report + ": " + detail
        _set_last_error(report)
        _tag_redraw()
        return False, report

    with _session_lock:
        _session = session
    _set_last_error("")
    _register_pump()
    _tag_redraw()
    return True, "Live started from {:s}.".format(
        os.path.basename(discovered["path"])
    )


def get_session():
    with _session_lock:
        return _session


def is_running():
    session = get_session()
    return session is not None and session.alive()


def needs_rebind(scene):
    session = get_session()
    if session is None:
        return False
    try:
        from . import cadex_backend
        current = os.path.abspath(cadex_backend.project_root(scene))
    except Exception:
        return True
    return current != session.root


def stop(restore=True):
    """Stop Live, clear forces, and optionally restore baked action bindings."""

    global _session
    from . import cadex_animate

    with _session_lock:
        session = _session
        _session = None
    if session is None:
        return "Live is already stopped."
    try:
        session.send({"type": "clear_forces"})
        session.send({"type": "shutdown"})
    except OSError:
        pass
    try:
        session.connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        session.connection.close()
    except OSError:
        pass
    try:
        session.process.wait(timeout=2.0)
    except (OSError, subprocess.TimeoutExpired):
        _close_process(session.process)
    else:
        _close_process(session.process)
    if restore:
        cadex_animate.restore_after_live(session.suspended_actions)
    else:
        cadex_animate.discard_after_live(session.suspended_actions)
    _tag_redraw()
    return "Live stopped."


def set_paused(paused):
    session = get_session()
    if session is None or not session.alive():
        return False, "Live is not running."
    try:
        session.send({"type": "pause" if paused else "resume"})
    except OSError as error:
        return False, "Could not update Live: {!s}".format(error)
    with session.lock:
        session.paused = bool(paused)
    _tag_redraw()
    return True, "Live paused." if paused else "Live resumed."


def toggle_pause():
    session = get_session()
    if session is None:
        return False, "Live is not running."
    return set_paused(not session.paused)


def reset():
    session = get_session()
    if session is None or not session.alive():
        return False, "Live is not running."
    try:
        session.send({"type": "reset"})
    except OSError as error:
        return False, "Could not reset Live: {!s}".format(error)
    return True, "Live reset to the solved keyframe."


def apply_force(body, force_n, point_m=None, torque_nm=None):
    session = get_session()
    if session is None or not session.alive():
        return False
    message = {
        "type": "apply_force",
        "body": str(body),
        "force_N": [float(value) for value in force_n],
        "torque_Nm": [float(value) for value in (torque_nm or (0.0, 0.0, 0.0))],
        "point_m": None if point_m is None else [float(value) for value in point_m],
    }
    try:
        session.send(message)
    except OSError:
        return False
    return True


def set_drag_target(body, target_m, stiffness=40.0, damping=12.0, max_force=25.0):
    """Update the live cursor spring (evaluated each MuJoCo substep)."""
    session = get_session()
    if session is None or not session.alive():
        return False
    message = {
        "type": "set_drag_target",
        "body": str(body),
        "target_m": [float(value) for value in target_m],
        "stiffness": float(stiffness),
        "damping": float(damping),
        "max_force": float(max_force),
    }
    try:
        session.send(message)
    except OSError:
        return False
    return True


def clear_drag():
    session = get_session()
    if session is None:
        return
    try:
        session.send({"type": "clear_drag"})
    except OSError:
        pass


def clear_forces():
    session = get_session()
    if session is None:
        return
    try:
        session.send({"type": "clear_forces"})
    except OSError:
        pass


def can_drag(body=None):
    session = get_session()
    if session is None or not session.alive():
        return False
    if body is None:
        return bool(session.dynamic_bodies)
    return str(body) in session.dynamic_bodies


def status(scene=None):
    session = get_session()
    if session is None:
        return {
            "active": False,
            "alive": False,
            "paused": False,
            "time_s": 0.0,
            "bodies": 0,
            "dynamic_bodies": 0,
            "output": "",
            "model": "",
            "error": last_error(),
        }
    with session.lock:
        return {
            "active": True,
            "alive": session.alive(),
            "paused": bool(session.paused),
            "time_s": float(session.time_s),
            "bodies": len(session.bodies),
            "dynamic_bodies": len(session.dynamic_bodies),
            "output": session.output,
            "model": session.xml_path,
            "error": session.error,
        }


def _pump():
    session = get_session()
    if session is None:
        return None
    if not session.alive() or session.reader_done.is_set():
        report = session.error or _process_error(session.process) or (
            "Live sidecar exited unexpectedly."
        )
        stop(restore=True)
        _set_last_error(report)
        return None
    try:
        import bpy
        from . import cadex_backend
        from . import cadex_hydrate
        scene = bpy.data.scenes.get(session.scene_name) or bpy.context.scene
        if os.path.abspath(cadex_backend.project_root(scene)) != session.root:
            stop(restore=False)
            _set_last_error("Live stopped because the project changed.")
            return None
    except Exception as error:
        stop(restore=False)
        _set_last_error("Live stopped: {!s}".format(error))
        return None

    with session.lock:
        sequence = session.state_sequence
        state = dict(session.latest_state or {})
        already_applied = session.applied_sequence
    if sequence != already_applied and state:
        placements = {}
        for name, pose in (state.get("placements") or {}).items():
            matrix = pose_to_matrix(pose)
            if matrix is not None:
                placements[str(name)] = matrix
        cadex_hydrate.apply_placements(placements)
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass
        with session.lock:
            session.applied_sequence = sequence
            session.time_s = float(state.get("t") or 0.0)
            session.paused = bool(state.get("paused"))
        _tag_redraw()
    return 1.0 / DISPLAY_HZ


def register():
    _set_last_error("")


def unregister():
    stop(restore=True)
