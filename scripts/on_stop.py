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
    # Claude Code sets this once we have already pushed it back to work with
    # no tool call in between. Returning `block` again here would spin.
    if payload.get("stop_hook_active"):
        return 0

    cfg, relay = H.ready()
    if not cfg:
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    body = H.summarize(payload.get("last_assistant_message", ""))
    if not body:
        return 0

    armed = is_away()
    rid = new_request_id()
    ctx = H.context_line(cwd)
    text = f"✅ Claude finished — {ctx}\n\n{body}" if ctx else f"✅ Claude finished\n\n{body}"

    H.send(relay, kind="finished", text=text, request_id=rid,
           session_id=session_id, cwd=cwd, expects_reply=armed,
           hint=(f"reply to keep it going (id `{rid}`)" if armed else ""))

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
