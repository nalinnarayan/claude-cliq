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
from cliqlib import (blanket_approval_until, grant_blanket_approval,
                     is_away, new_request_id)

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

    # Already said "all" for this session: approve silently. Pinging Cliq
    # again would defeat the entire point of having asked.
    if session_id and blanket_approval_until(session_id):
        H.emit({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow",
                         "reason": "Covered by an earlier 'all' from Zoho Cliq"}}})
        return 0

    rid = new_request_id()
    ctx = H.context_line(cwd)
    detail = describe(tool, tool_input)
    text = (f"🔐 Permission needed — {tool}"
            + (f"\n{ctx}" if ctx else "")
            + (f"\n\n{detail}" if detail else ""))

    if not H.send(relay, kind="permission", text=text, request_id=rid,
                  session_id=session_id, cwd=cwd, expects_reply=True,
                  hint=f"`yes {rid}` · `no {rid}` · `all {rid}` "
                       f"to approve everything for a while"):
        return 0        # relay down: fall through to the terminal prompt

    reply = H.block_for_reply(cfg, rid, session_id=session_id,
                              kind="permission", cwd=cwd,
                              tool_use_id=payload.get("tool_use_id", ""))

    if not reply or reply.get("type") != "verdict":
        # No answer in time: hand it back to the terminal rather than
        # guessing. "ask" is the safe default, never deny.
        H.emit({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "ask"}}})
        return 0

    if reply.get("allow"):
        reason = "Approved from Zoho Cliq"
        if reply.get("scope") == "session" and session_id:
            until = grant_blanket_approval(session_id, cfg.blanket_approval_seconds)
            mins = int(cfg.blanket_approval_seconds // 60)
            reason = f"Approved from Zoho Cliq; everything in this session for {mins}m"
            # Say plainly what was just granted -- a blanket approval the
            # user has forgotten about is the dangerous kind.
            H.send(relay, kind="ack", request_id=new_request_id(),
                   text=(f"👍 Approving everything in this session for {mins} minutes.\n"
                         f"Run `/claude-cliq:away off` to revoke it immediately."),
                   session_id=session_id, cwd=cwd)
        decision = {"behavior": "allow", "reason": reason}
    else:
        decision = {"behavior": "deny",
                    "reason": "Denied from Zoho Cliq"}
    H.emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": decision}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
