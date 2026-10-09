#!/usr/bin/env python3
"""Session liveness registry.

The daemon must never run `--bg --resume` against a session that is still
open in a terminal -- that forks a copy instead of continuing it. These two
hooks keep a pid file per session so the daemon can tell the difference.

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
