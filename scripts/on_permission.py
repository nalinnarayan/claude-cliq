#!/usr/bin/env python3
"""PermissionRequest hook -- approve or deny from Cliq.

Returns a decision only when away mode is armed. Disarmed, it exits silently
so the terminal prompt behaves exactly as normal: you are sitting right
there, and a Cliq ping would be pure noise.

ExitPlanMode arrives here too, which is what makes "release the next phase"
work.
"""

from __future__ import annotations

import json
import sys

import ask
import hookutil as H
from cliqlib import (blanket_approval_until, grant_blanket_approval,
                     is_away, new_request_id, notifies, notify_only_hint)

PREVIEW_CHARS = 700

# Claude Code throws away an `allow` that carries no `updatedInput` when the
# tool is one it would otherwise have put a dialog in front of. The guard is
# `if (!decision.updatedInput && tool.requiresUserInteraction?.()) return null`,
# and the exempt set is exactly these two tools. Echoing the input straight
# back is what makes the approval stick; without it the decision is discarded
# and the terminal prompt opens with nobody sitting there.
ECHOES_INPUT = {"ExitPlanMode", "AskUserQuestion"}


def allow_decision(tool: str, tool_input: dict) -> dict:
    """The allow half of the decision union.

    There is no `reason` field on this branch -- the union is
    {behavior, updatedInput?, updatedPermissions?} -- so anything explanatory
    has to go to Cliq instead of into the decision.
    """
    decision: dict = {"behavior": "allow"}
    if tool in ECHOES_INPUT:
        decision["updatedInput"] = tool_input
    return decision


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
    if not notifies():
        return 0

    cfg, relay = H.ready()
    if not cfg:
        return 0

    tool = payload.get("tool_name", "?")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")

    # A question is not a permission, and "allow everything" was never a
    # statement about questions. Left to the blanket check below, an
    # AskUserQuestion would be "approved" with updatedInput set to the
    # UNMODIFIED input -- no answers -- which satisfies Claude Code's guard, so
    # the decision stands and the tool runs having answered nothing. Before
    # ECHOES_INPUT existed the decision was discarded and the dialog appeared,
    # which was wrong but harmless; now it is silent and wrong.
    if tool == "AskUserQuestion":
        return ask.handle(cfg, relay, payload)

    # Already said "all" for this session: approve silently. Pinging Cliq
    # again would defeat the entire point of having asked.
    if session_id and blanket_approval_until(session_id):
        H.emit({"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": allow_decision(tool, tool_input)}})
        return 0

    rid = new_request_id()
    ctx = H.context_line(cwd)
    detail = describe(tool, tool_input)
    text = (f"🔐 Permission needed — {tool}"
            + (f"\n{ctx}" if ctx else "")
            + (f"\n\n{detail}" if detail else ""))

    if not is_away():
        # notify-only. Say a permission came up, then get out of the way: the
        # terminal prompt is already in front of the person and nothing sent
        # here could approve anything, so buttons would be a lie. Emitting no
        # decision is what lets that prompt proceed normally.
        H.send(relay, kind="permission", text=text, request_id=rid,
               session_id=session_id, cwd=cwd, expects_reply=False,
               hint=notify_only_hint(waiting_at_terminal=True))
        return 0

    mins = int(cfg.blanket_approval_seconds // 60)
    if not H.send(relay, kind="permission", text=text, request_id=rid,
                  session_id=session_id, cwd=cwd, expects_reply=True,
                  hint="tap a button — typed replies can't approve anything",
                  answers=[{"label": "Allow", "value": "allow"},
                           {"label": "Deny", "value": "deny"},
                           {"label": f"Allow all ({mins}m)", "value": "all"}]):
        return 0        # relay down: fall through to the terminal prompt

    reply = H.block_for_reply(cfg, rid, session_id=session_id,
                              kind="permission", cwd=cwd,
                              tool_use_id=payload.get("tool_use_id", ""),
                              topic=f"{tool}: {detail}")

    if not reply or reply.get("type") != "verdict":
        # Say nothing at all, which is how a hook declines to decide and hands
        # the call back to the terminal prompt. There is deliberately no
        # {"behavior": "ask"} here: the union is allow-or-deny, and Claude
        # Code rejects "ask" with 'PermissionRequest decision must be
        # {"behavior": "allow"} or {"behavior": "deny", "message": "..."}'.
        return 0

    if reply.get("allow"):
        if reply.get("scope") == "session" and session_id:
            grant_blanket_approval(session_id, cfg.blanket_approval_seconds)
            mins = int(cfg.blanket_approval_seconds // 60)
            # Say plainly what was just granted -- a blanket approval the
            # user has forgotten about is the dangerous kind.
            H.send(relay, kind="ack", request_id=new_request_id(),
                   text=(f"👍 Approving everything in this session for {mins} minutes.\n"
                         f"Run `/claude-cliq:away off` to revoke it immediately."),
                   session_id=session_id, cwd=cwd)
        decision = allow_decision(tool, tool_input)
    else:
        # `message` is the field name on this branch; `reason` is silently
        # dropped, which is why every denial used to reach Claude unexplained.
        decision = {"behavior": "deny", "message": "Denied from Zoho Cliq"}
    H.emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": decision}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
