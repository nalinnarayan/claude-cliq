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


def main() -> int:
    payload = H.read_payload()
    tool_use_id = payload.get("tool_use_id", "")
    if tool_use_id:
        try:
            waiter.disarm_for_tool(tool_use_id)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
