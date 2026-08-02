# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Blend-scoped chat state mirrored into ``bpy.data.texts``.

Classic headless turns keep their transcript and resumable session id in the
``.blend``. The embedded Open Grok TUI is different: its full conversation
lives in Open Grok's project-scoped session store. Cadex does not scrape the
VT screen into a fake transcript; it stores only lifecycle notices and the
terminal workdir/session metadata that can be discovered reliably.
"""

import json

TEXT_BLOCK_NAME = "mesh_chat.json"
SCHEMA = "mesh-chat-v1"

# Roles: "user", "assistant", "status" (tool activity / notices, greyed out),
# "thought" (model reasoning while a turn is in flight, muted).


class ChatMessage:
    __slots__ = ("role", "text")

    def __init__(self, role, text=""):
        self.role = role
        self.text = text


class ChatHistory:
    def __init__(self):
        self.messages = []
        self._streaming = None  # message receiving streamed text
        #: Headless backend session to resume, or "" for a fresh conversation.
        self.session_id = ""
        self.terminal_provider = "open_grok"
        self.terminal_workdir = ""
        self.terminal_project_root = ""
        self.terminal_session_id = ""

    def add(self, role, text):
        self.messages.append(ChatMessage(role, text))

    def begin_assistant(self):
        self._streaming = ChatMessage("assistant", "")
        self.messages.append(self._streaming)

    def begin_thought(self):
        """Start (or reopen) a muted reasoning block for this turn."""
        if self._streaming is not None and self._streaming.role == "thought":
            return
        self.end_assistant()
        self._streaming = ChatMessage("thought", "")
        self.messages.append(self._streaming)

    def append_stream(self, text):
        if self._streaming is None:
            self.begin_assistant()
        self._streaming.text += text

    def append_thought(self, text):
        """Append one cleaned reasoning line (Open Grok thought events)."""
        line = str(text or "").strip()
        if not line:
            return
        self.begin_thought()
        # One bullet per thought event so chunks do not run together.
        if self._streaming.text:
            self._streaming.text += "\n"
        self._streaming.text += "· " + line

    def end_assistant(self):
        # Drop the placeholder if nothing was streamed.
        if self._streaming is not None and not self._streaming.text:
            try:
                self.messages.remove(self._streaming)
            except ValueError:
                pass
        self._streaming = None

    def clear(self):
        self.messages = []
        self._streaming = None
        self.session_id = ""
        self.terminal_provider = "open_grok"
        self.terminal_workdir = ""
        self.terminal_project_root = ""
        self.terminal_session_id = ""

    def set_terminal_state(self, workdir=None, project_root=None,
                           session_id=None):
        """Mirror discoverable Open Grok state without claiming a transcript."""
        if workdir is not None:
            self.terminal_workdir = str(workdir or "")
        if project_root is not None:
            self.terminal_project_root = str(project_root or "")
        if session_id is not None:
            self.terminal_session_id = str(session_id or "")

    def to_json(self):
        return json.dumps(
            {
                "schema": SCHEMA,
                "session_id": self.session_id,
                "terminal": {
                    "provider": self.terminal_provider,
                    "workdir": self.terminal_workdir,
                    "project_root": self.terminal_project_root,
                    "session_id": self.terminal_session_id,
                },
                "messages": [{"role": message.role, "text": message.text}
                             for message in self.messages],
            },
            ensure_ascii=False, indent=1)

    def from_json(self, text):
        self.clear()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(data, dict):
            self.session_id = str(data.get("session_id") or "")
            terminal = data.get("terminal") or {}
            if isinstance(terminal, dict):
                self.terminal_provider = str(
                    terminal.get("provider") or "open_grok")
                self.terminal_workdir = str(terminal.get("workdir") or "")
                self.terminal_project_root = str(
                    terminal.get("project_root") or "")
                self.terminal_session_id = str(
                    terminal.get("session_id") or "")
            items = data.get("messages") or []
        else:
            # Transcripts written before the session id was carried.
            items = data
        for item in items:
            if isinstance(item, dict) and "role" in item:
                self.add(item["role"], item.get("text", ""))

    def save_to_text_block(self):
        import bpy
        text_block = bpy.data.texts.get(TEXT_BLOCK_NAME)
        if text_block is None:
            text_block = bpy.data.texts.new(TEXT_BLOCK_NAME)
        text_block.clear()
        text_block.write(self.to_json())
        # Keep the transcript when the text datablock is otherwise unused.
        text_block.use_fake_user = True

    def load_from_text_block(self):
        import bpy
        text_block = bpy.data.texts.get(TEXT_BLOCK_NAME)
        if text_block is not None:
            self.from_json(text_block.as_string())
        else:
            self.clear()
