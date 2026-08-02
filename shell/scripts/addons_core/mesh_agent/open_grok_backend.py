# SPDX-License-Identifier: GPL-2.0-or-later

"""
Open Grok backend: runs one ``open-grok -p`` headless turn per chat message.

Auth is **OAuth only** — Open Grok reads tokens from ``~/.opengrok``
(``open-grok login --oauth`` for Grok, ``open-grok login --codex`` for
ChatGPT). Cadex never sets API keys.

Cadex tools are exposed as a project-scoped MCP server named ``mesh`` via
``mcp_shim.py`` → the localhost bridge (same path Claude Code used).
Stdout is ``--output-format streaming-json`` (NDJSON); events are normalized
into the shapes ``agent._on_stream`` already understands.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading


_OPEN_GROK_CANDIDATES = (
    r"~\.opengrok\bin\open-grok.exe",
    r"~\.opengrok\bin\open-grok",
    r"~\.grok\bin\grok.exe",
    r"~\.grok\bin\grok",
    "/usr/local/bin/open-grok",
    "/usr/local/bin/grok",
    "/opt/homebrew/bin/open-grok",
    "/opt/homebrew/bin/grok",
)

# Built-in tools to strip so the model prefers mesh MCP for CAD work.
# MCP tools stay available unless explicitly denied (Open Grok headless docs).
# GrokBuild:agent_swarm fails session build with "Requirements unsatisfied"
# on some project configs — always deny it for Cadex headless turns.
_DISALLOWED_BUILTINS = ",".join((
    "run_terminal_cmd",
    "search_replace",
    "write",
    "edit_file",
    "web_search",
    "web_fetch",
    "Agent",
    "GrokBuild:agent_swarm",
    "agent_swarm",
))

# Sentinel: omit ``-m`` and use ~/.opengrok config default.
MODEL_CONFIG_DEFAULT = "__default__"


def find_open_grok(explicit_path=""):
    """Locate the ``open-grok`` (or stock ``grok``) CLI, or return None."""
    if explicit_path:
        path = os.path.expanduser(explicit_path)
        return path if os.path.isfile(path) else None
    for name in ("open-grok", "open-grok.exe", "grok", "grok.exe"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _OPEN_GROK_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return path
    return None


def _toml_escape(value):
    """Minimal TOML basic-string escape for paths and tokens."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def _normalize_event(obj):
    """Map Open Grok streaming-json → Claude-like stream objects for Agent."""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    session_id = obj.get("sessionId") or obj.get("session_id")

    if kind == "text":
        text = obj.get("data") or ""
        event = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": text},
            },
        }
        if session_id:
            event["session_id"] = session_id
        return event

    if kind == "thought":
        # Show reasoning so the user can follow (and cancel) the turn.
        # Clean markdown bold that models stuff into thoughts (**foo**).
        text = str(obj.get("data") or "").strip()
        if not text:
            return None
        text = text.replace("**", "").replace("__", "")
        # Collapse internal whitespace from multi-chunk blips.
        text = " ".join(text.split())
        event = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {
                    "type": "text_delta",
                    "text": text,
                },
            },
            "cadex_thought": True,
        }
        if session_id:
            event["session_id"] = session_id
        return event

    if kind == "end":
        stop = str(obj.get("stopReason") or "")
        # EndTurn is success; anything else still ends the turn but may flag.
        is_error = stop not in ("", "EndTurn", "end_turn", "EndTurnMaxTurns")
        event = {
            "type": "result",
            "is_error": is_error and stop not in ("MaxTurns", "max_turns_reached"),
            "result": stop or "ok",
        }
        if session_id:
            event["session_id"] = session_id
        return event

    if kind == "error":
        event = {
            "type": "result",
            "is_error": True,
            "result": obj.get("message") or obj.get("data") or "Open Grok error",
        }
        if session_id:
            event["session_id"] = session_id
        return event

    if kind in ("tool_call", "tool_use"):
        name = (
            obj.get("name")
            or obj.get("title")
            or (obj.get("tool") or {}).get("name")
            or "tool"
        )
        short = str(name).rsplit("__", 1)[-1]
        return {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": short}],
            },
        }

    # Unknown event: ignore (stream is non-exhaustive per Open Grok docs).
    if session_id:
        return {"type": "system", "subtype": "session", "session_id": session_id}
    return None


class OpenGrokBackend:
    """One Open Grok headless process per chat turn; OAuth via ~/.opengrok."""

    is_mock = False

    def __init__(self, open_grok_path, model, system_prompt, tool_names,
                 bridge_port, bridge_token, max_turns=25):
        self.open_grok_path = open_grok_path
        self.model = model or MODEL_CONFIG_DEFAULT
        self.system_prompt = system_prompt
        self.tool_names = list(tool_names or [])
        self.bridge_port = bridge_port
        self.bridge_token = bridge_token
        self.max_turns = max(1, int(max_turns or 25))
        self.session_id = None
        self._process = None
        self._workdir = tempfile.mkdtemp(prefix="cadex_opengrok_")
        self._write_project_mcp_config()

    def _shim_path(self):
        return os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "mcp_shim.py")

    def _write_project_mcp_config(self):
        """Project-scoped MCP so the mesh server is only for this turn cwd."""
        conf_dir = os.path.join(self._workdir, ".opengrok")
        os.makedirs(conf_dir, exist_ok=True)
        # Prefer Blender's Python (sys.executable) — mcp_shim is stdlib-only.
        python = sys.executable
        shim = self._shim_path()
        # Forward slashes work on Windows for most spawn paths; TOML-safe.
        py = _toml_escape(python.replace("\\", "/"))
        sh = _toml_escape(shim.replace("\\", "/"))
        port = str(self.bridge_port)
        token = _toml_escape(self.bridge_token)
        body = (
            "# Generated by Cadex mesh_agent — do not commit.\n"
            "# OAuth tokens still come from ~/.opengrok (login --oauth / --codex).\n"
            "[mcp_servers.mesh]\n"
            'command = "{python}"\n'
            "args = [\n"
            '  "{shim}",\n'
            '  "--port", "{port}",\n'
            '  "--token", "{token}",\n'
            "]\n"
            "enabled = true\n"
            "startup_timeout_sec = 60\n"
            "tool_timeout_sec = 600\n"
        ).format(python=py, shim=sh, port=port, token=token)
        path = os.path.join(conf_dir, "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        # Light project marker so open-grok treats the cwd as a project root.
        readme = os.path.join(self._workdir, "CADEX_AGENT_WORKDIR.txt")
        with open(readme, "w", encoding="utf-8") as handle:
            handle.write(
                "Cadex temporary workdir for Open Grok MCP mesh tools.\n"
            )
        return path

    def _command(self, prompt):
        # Tool roster for the model (MCP names as Open Grok will present them).
        tool_list = ", ".join(self.tool_names) if self.tool_names else "(mesh MCP tools)"
        system = (
            self.system_prompt
            + "\n\nYou must act only through the mesh MCP tools "
            "(server name mesh): "
            + tool_list
            + ". Do not use shell or file-edit tools for CAD geometry."
        )
        command = [
            self.open_grok_path,
            "-p", prompt,
            "--output-format", "streaming-json",
            "--always-approve",
            "--permission-mode", "bypassPermissions",
            "--system-prompt-override", system,
            "--cwd", self._workdir,
            "--disallowed-tools", _DISALLOWED_BUILTINS,
            "--no-subagents",
            "--max-turns", str(self.max_turns),
            "--verbatim",
        ]
        model = (self.model or "").strip()
        if model and model != MODEL_CONFIG_DEFAULT:
            command.extend(["-m", model])
        if self.session_id:
            command.extend(["--resume", str(self.session_id)])
        return command

    def start_turn(self, prompt, events):
        thread = threading.Thread(
            target=self._run_with_resume_fallback,
            args=(prompt, events),
            daemon=True,
            name="mesh-agent-open-grok",
        )
        thread.start()

    def _run_with_resume_fallback(self, prompt, events):
        resuming = bool(self.session_id)
        produced = self._run(prompt, events, swallow_exit=resuming)
        if not resuming or produced:
            return
        self.session_id = None
        events.put(("stream", {"type": "system", "subtype": "resume_failed"}))
        self._run(prompt, events)

    def _scrub_env(self):
        """Child env: inherit user OAuth home; never inject API keys."""
        env = os.environ.copy()
        # Ensure Open Grok still finds ~/.opengrok for auth even if cwd is temp.
        # Do not set OPENAI_API_KEY / XAI_API_KEY / ANTHROPIC_API_KEY ourselves.
        return env

    def _run(self, prompt, events, swallow_exit=False):
        try:
            process = subprocess.Popen(
                self._command(prompt),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=self._workdir,
                env=self._scrub_env(),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as ex:
            events.put((
                "error",
                "Failed to launch Open Grok: {!s}. "
                "Install open-grok and run `open-grok login --oauth` "
                "and/or `open-grok login --codex` (OAuth; no API keys).".format(ex),
            ))
            return False

        self._process = process
        stderr_tail = []
        produced = False

        def read_stderr():
            for line in process.stderr:
                stderr_tail.append(line)
                del stderr_tail[:-60]

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_sid = None
                if isinstance(obj, dict):
                    raw_sid = obj.get("sessionId") or obj.get("session_id")
                if raw_sid:
                    self.session_id = str(raw_sid)
                    produced = True
                normalized = _normalize_event(obj)
                if normalized is None:
                    continue
                sid = normalized.get("session_id")
                if sid:
                    self.session_id = str(sid)
                produced = True
                events.put(("stream", normalized))
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass
            returncode = process.wait()
            stderr_thread.join(timeout=2.0)
            self._process = None
            err_text = "".join(stderr_tail).strip()
            if not (swallow_exit and not produced):
                # Helpful hint when auth is missing.
                if returncode != 0 and not produced:
                    low = err_text.lower()
                    if any(
                        token in low
                        for token in (
                            "login",
                            "auth",
                            "unauthor",
                            "not signed",
                            "not logged",
                            "oauth",
                        )
                    ):
                        err_text = (
                            (err_text + "\n") if err_text else ""
                        ) + (
                            "Open Grok OAuth required: run "
                            "`open-grok login --oauth` (Grok) and/or "
                            "`open-grok login --codex` (ChatGPT) in a terminal. "
                            "Cadex does not use API keys."
                        )
                events.put(("exit", returncode, err_text))
        return produced

    def cancel(self):
        process = self._process
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
