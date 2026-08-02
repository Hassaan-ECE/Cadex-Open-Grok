# SPDX-FileCopyrightText: 2026 Mesh Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Audit Open Grok session transcripts for Cadex mesh-tool usage.

Chats for an embedded Cadex project live under::

    %USERPROFILE%\\.opengrok\\sessions\\<url-encoded-cwd>\\<session-id>\\

where ``cwd`` is the project home ``<stem>.cadex/.cadex_terminal``.

Examples::

    python session_audit.py
    python session_audit.py --project "D:\\Projects\\Cadex_Projects\\testing2\\test.cadex"
    python session_audit.py --session-dir "C:\\Users\\...\\.opengrok\\sessions\\...\\019f..."

Prints user prompts, mesh MCP tool counts, and whether the session looks
healthy (describe_cad_api before writes, etc.).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote


_MESH_TOOLS = (
    "describe_cad_api",
    "get_script",
    "write_script",
    "edit_script",
    "set_params",
    "rebuild_model",
    "inspect_model",
    "scene_summary",
    "viewport_screenshot",
    "collision_view",
    "export_stl",
    "import_geometry",
    "focus_view",
    "get_attached_image",
    "restore_version",
)

_TOOL_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _MESH_TOOLS) + r")\b"
)
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_DYNAMICS_RE = re.compile(
    r"\b(?:assembly\.(?:dynamics|body|collision|mjcf|joint|component|solve)"
    r"|density_kg_m3|paint_faces)\b"
)


def _sessions_root() -> Path:
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    return Path(home) / ".opengrok" / "sessions"


def _encode_cwd(cwd: str) -> str:
    # Open Grok stores sessions under a URL-encoded absolute path.
    abs_cwd = os.path.abspath(cwd)
    return quote(abs_cwd, safe="")


def project_terminal_home(project_or_terminal: str) -> Path:
    path = Path(project_or_terminal).resolve()
    if path.name == ".cadex_terminal":
        return path
    terminal = path / ".cadex_terminal"
    if terminal.is_dir():
        return terminal
    # sibling of .blend style: test.cadex already is the project root
    if path.suffix == ".cadex" or path.name.endswith(".cadex"):
        return path / ".cadex_terminal"
    return path


def find_session_dirs(project_or_terminal: str | None = None) -> list[Path]:
    root = _sessions_root()
    if not root.is_dir():
        return []
    if project_or_terminal:
        home = project_terminal_home(project_or_terminal)
        key = _encode_cwd(str(home))
        bucket = root / key
        if not bucket.is_dir():
            # also try without forcing .cadex_terminal
            key2 = _encode_cwd(str(Path(project_or_terminal).resolve()))
            bucket = root / key2
        if not bucket.is_dir():
            return []
        return sorted(
            [p for p in bucket.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    # newest session dirs across all projects
    found: list[Path] = []
    for bucket in root.iterdir():
        if not bucket.is_dir() or bucket.name.startswith("session_"):
            continue
        for child in bucket.iterdir():
            if child.is_dir() and (child / "chat_history.jsonl").is_file():
                found.append(child)
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def audit_session(session_dir: Path) -> dict:
    chat = session_dir / "chat_history.jsonl"
    events = session_dir / "events.jsonl"
    summary_path = session_dir / "summary.json"

    summary = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            summary = {"_error": str(exc)}

    roles: collections.Counter[str] = collections.Counter()
    tool_hits: collections.Counter[str] = collections.Counter()
    dynamics_hits: collections.Counter[str] = collections.Counter()
    user_prompts: list[str] = []
    mcp_from_events: collections.Counter[str] = collections.Counter()

    if chat.is_file():
        for line in chat.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = str(obj.get("type") or "")
            roles[typ] += 1
            for match in _TOOL_RE.findall(line):
                tool_hits[match] += 1
            for match in _DYNAMICS_RE.findall(line):
                dynamics_hits[match] += 1
            if typ == "user":
                m = _USER_QUERY_RE.search(line)
                if m:
                    user_prompts.append(re.sub(r"\s+", " ", m.group(1)).strip()[:200])

    if events.is_file():
        for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = str(obj.get("type") or "")
            if typ.startswith("mcp_tool_call_"):
                name = obj.get("tool_name") or obj.get("name") or "?"
                phase = "completed" if "completed" in typ else (
                    "started" if "started" in typ else typ
                )
                mcp_from_events[f"{phase}:{name}"] += 1

    describe = mcp_from_events.get("completed:describe_cad_api", 0) + tool_hits.get(
        "describe_cad_api", 0
    )
    writes = mcp_from_events.get("completed:write_script", 0) + tool_hits.get(
        "write_script", 0
    )
    # Prefer event-completed counts when present
    describe_c = mcp_from_events.get("completed:describe_cad_api", 0)
    write_c = mcp_from_events.get("completed:write_script", 0)
    edit_c = mcp_from_events.get("completed:edit_script", 0)

    health = []
    if describe_c or describe:
        health.append("used describe_cad_api")
    else:
        health.append("WARNING: no describe_cad_api seen")
    if write_c or edit_c or writes:
        health.append("wrote or edited script")
    else:
        health.append("no write_script/edit_script completed")
    if any(k.startswith("completed:") for k in mcp_from_events):
        health.append("mesh MCP tools completed via events log")

    info = (summary.get("info") or {}) if isinstance(summary, dict) else {}
    return {
        "session_dir": str(session_dir),
        "session_id": info.get("id") or session_dir.name,
        "cwd": info.get("cwd"),
        "session_summary": summary.get("session_summary") if isinstance(summary, dict) else None,
        "model": summary.get("current_model_id") if isinstance(summary, dict) else None,
        "user_prompts": user_prompts,
        "roles": dict(roles.most_common()),
        "tool_keyword_hits": dict(tool_hits.most_common()),
        "dynamics_or_paint_hits": dict(dynamics_hits.most_common()),
        "mcp_events": dict(mcp_from_events.most_common()),
        "health": health,
    }


def _print_report(report: dict) -> None:
    print("session:", report["session_id"])
    print("  dir:", report["session_dir"])
    if report.get("cwd"):
        print("  cwd:", report["cwd"])
    if report.get("session_summary"):
        print("  summary:", report["session_summary"])
    if report.get("model"):
        print("  model:", report["model"])
    print("  health:", "; ".join(report["health"]))
    if report["user_prompts"]:
        print("  user prompts:")
        for p in report["user_prompts"]:
            print("   -", p)
    if report["mcp_events"]:
        print("  mcp events (started/completed):")
        for k, v in list(report["mcp_events"].items())[:24]:
            print(f"   - {k}: {v}")
    elif report["tool_keyword_hits"]:
        print("  tool keyword hits in chat_history:")
        for k, v in list(report["tool_keyword_hits"].items())[:20]:
            print(f"   - {k}: {v}")
    if report["dynamics_or_paint_hits"]:
        print("  dynamics/paint API mentions:")
        for k, v in report["dynamics_or_paint_hits"].items():
            print(f"   - {k}: {v}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        help="Cadex project root (…/foo.cadex) or .cadex_terminal path",
    )
    parser.add_argument(
        "--session-dir",
        help="Exact Open Grok session directory containing chat_history.jsonl",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="How many recent sessions to show (default 3)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of text",
    )
    args = parser.parse_args(argv)

    if args.session_dir:
        dirs = [Path(args.session_dir)]
    else:
        dirs = find_session_dirs(args.project)[: max(1, args.limit)]

    if not dirs:
        print(
            "No sessions found. Looked under:",
            _sessions_root(),
            file=sys.stderr,
        )
        if args.project:
            home = project_terminal_home(args.project)
            print(
                "Expected bucket for:",
                home,
                "→",
                _sessions_root() / _encode_cwd(str(home)),
                file=sys.stderr,
            )
        return 1

    reports = [audit_session(d) for d in dirs]
    if args.json:
        json.dump(reports, sys.stdout, indent=2)
        print()
    else:
        print("Open Grok sessions root:", _sessions_root())
        print()
        for report in reports:
            _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
