#!/usr/bin/env python3
"""PermissionRequest hook -- approve or deny from Cliq.

Returns a decision only when away mode is armed. Disarmed, it exits silently
so the terminal prompt behaves exactly as normal: you are sitting right
there, and a Cliq ping would be pure noise.

ExitPlanMode arrives here too, which is what makes "release the next phase"
work without any special casing.
"""

from __future__ import annotations

import json
import sys

import hookutil as H
from cliqlib import is_away, new_request_id

PREVIEW_CHARS = 700


def describe(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        return (tool_input.get("command") or "")[:PREVIEW_CHARS]
    if tool == "ExitPlanMode":
        return "Approve the plan and start work"
    for key in ("file_path", "path", "url", "pattern"):
        if key in tool_input:
            return f"{key}: {tool_input[key]}"
    try:
        return json.dumps(tool_input)[:PREVIEW_CHARS]
    except Exception:
        return ""


def main() -> int:
    payload = H.read_payload()
    if not is_away():
        return 0

    cfg, relay = H.ready()
    if not cfg:
        return 0

    tool = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")

    rid = new_request_id()
    ctx = H.context_line(cwd)
    detail = describe(tool, tool_input)
    text = (f"🔐 Permission needed — {tool}"
            + (f"\n{ctx}" if ctx else "")
            + (f"\n\n{detail}" if detail else ""))

    if not H.send(relay, kind="permission", text=text, request_id=rid,
                  session_id=session_id, cwd=cwd, expects_reply=True,
                  hint=f"reply `yes {rid}` or `no {rid}`"):
        return 0        # relay down: fall through to the terminal prompt

    reply = H.block_for_reply(cfg, rid, session_id=session_id,
                              kind="permission", cwd=cwd)

    if not reply or reply.get("type") != "verdict":
        # No answer in time: hand it back to the terminal rather than
        # guessing. "ask" is the safe default, never deny.
        H.emit({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "ask"}}})
        return 0

    if reply.get("allow"):
        decision = {"behavior": "allow",
                    "reason": "Approved from Zoho Cliq"}
    else:
        decision = {"behavior": "deny",
                    "reason": "Denied from Zoho Cliq"}
    H.emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": decision}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
