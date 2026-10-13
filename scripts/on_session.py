#!/usr/bin/env python3
"""Session liveness registry.

A reply that nothing is waiting for is treated differently depending on
whether its session is still alive: alive means its `Stop` hook will fire
again shortly, so the reply is held for it, while a session that has gone
means saying so. These two hooks keep a pid file per session so the daemon
can tell the two apart.

    on_session.py start   <- SessionStart
    on_session.py end     <- SessionEnd
"""

from __future__ import annotations

import sys

import hookutil as H
import waiter


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "start"
    payload = H.read_payload()
    session_id = payload.get("session_id", "")
    if not session_id:
        return 0
    try:
        if mode == "start":
            waiter.register_session(session_id, payload.get("cwd", ""))
        else:
            waiter.unregister_session(session_id)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
