#!/usr/bin/env python3
"""Error and stuck notifications. Fire-and-forget: never blocks a session.

    on_trouble.py failure     <- PostToolUseFailure
    on_trouble.py idle        <- Notification (idle_prompt)
"""

from __future__ import annotations

import sys

import hookutil as H
from cliqlib import is_away, new_request_id

LIMIT = 600


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "failure"
    payload = H.read_payload()
    if not is_away():
        return 0

    cfg, relay = H.ready()
    if not cfg:
        return 0

    cwd = payload.get("cwd", "")
    ctx = H.context_line(cwd)

    if mode == "idle":
        text = f"⏳ Claude has been idle and may be waiting on you — {ctx}"
    else:
        tool = payload.get("tool_name", "a tool")
        err = payload.get("tool_response") or payload.get("error") or ""
        if isinstance(err, dict):
            err = err.get("stderr") or err.get("error") or str(err)
        text = f"⚠️ {tool} failed — {ctx}\n\n{str(err)[:LIMIT]}"

    H.send(relay, kind=mode, text=text, request_id=new_request_id(),
           session_id=payload.get("session_id", ""), cwd=cwd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
