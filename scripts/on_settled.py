#!/usr/bin/env python3
"""Cancel a Cliq permission wait once its tool call has been settled.

A permission answered at the terminal never reaches the hook still waiting on
Cliq, so without this the waiter lingers for its whole window -- and while any
waiter is armed, every bare reply from Cliq is ambiguous and gets bounced.

Runs on PostToolUse and PostToolUseFailure: either outcome means the question
was decided without us.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import hookutil as H
import waiter
from cliqlib import is_away, log


def main() -> int:
    # Only the waiting mode ever arms a waiter, so only it has anything to
    # disarm. Checked first because this runs after EVERY tool call.
    if not is_away():
        return 0

    payload = H.read_payload()

    # This tool call has settled, so a permission still waiting on it in this
    # session is answered -- at the terminal, since nothing from Cliq reached
    # the hook. Clearing it stops a later tap on that Cliq message handing a
    # decision to a call that already ran.
    #
    # By SESSION, not by tool_use_id. `PermissionRequest` carries no
    # tool_use_id for any tool -- measured: `tool_use_id=NONE` for Bash, and
    # the same for AskUserQuestion -- so no waiter has ever had one to match,
    # and disarm_for_tool could never fire. It is kept below for the day a
    # payload does carry one, but it is not what does the work.
    session_id = payload.get("session_id", "")
    if session_id:
        try:
            gone = waiter.disarm_for_session(session_id, kinds=("permission",))
            if gone:
                log(f"settled: cleared {gone} permission waiter(s) answered "
                    f"at the terminal")
        except Exception:
            pass

    tool_use_id = payload.get("tool_use_id", "")
    if tool_use_id:
        try:
            waiter.disarm_for_tool(tool_use_id)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
