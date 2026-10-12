#!/usr/bin/env python3
"""Stop hook -- "it's done" and "it asked you something" are the same event.

Armed, a reply comes back as {"decision":"block"}, which puts Claude back to
work; it will stop again later and this hook fires again. That is the whole
loop -- nothing external ever needs to re-trigger a hook.
"""

from __future__ import annotations

import sys

import hookutil as H
from cliqlib import is_away, new_request_id


def main() -> int:
    payload = H.read_payload()
    # True on the Stop immediately after we pushed Claude back to work with no
    # tool call in between. It must stop us BLOCKING again -- otherwise the
    # session spins -- but it must not stop us reporting the result, or every
    # answer to a follow-up asked from Cliq vanishes silently.
    resumed = bool(payload.get("stop_hook_active"))

    cfg, relay = H.ready()
    if not cfg:
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    body = H.summarize(payload.get("last_assistant_message", ""))
    if not body:
        return 0

    # Never hold the session twice in a row: that is what `resumed` guards.
    armed = is_away() and not resumed
    rid = new_request_id()
    ctx = H.context_line(cwd)
    lead = "💬 Answered" if resumed else "✅ Claude finished"
    text = f"{lead} — {ctx}\n\n{body}" if ctx else f"{lead}\n\n{body}"

    if resumed:
        # Released rather than held, so say how to keep going: a reply now
        # takes the late-reply path and resumes the session in the background.
        hint = "reply to pick this up again"
    elif armed:
        hint = f"reply to keep it going (id `{rid}`)"
    else:
        hint = ""

    H.send(relay, kind="finished", text=text, request_id=rid,
           session_id=session_id, cwd=cwd, expects_reply=armed, hint=hint)

    if not armed:
        return 0

    reply = H.block_for_reply(cfg, rid, session_id=session_id, kind="finished",
                              cwd=cwd)
    if not reply:
        return 0        # window lapsed; the daemon owns it from here

    answer = (reply.get("text") or "").strip()
    if not answer:
        return 0
    H.emit({"decision": "block",
            "reason": f'The user replied via Zoho Cliq: "{answer}"'})
    return 0


if __name__ == "__main__":
    sys.exit(main())
