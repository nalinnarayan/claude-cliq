#!/usr/bin/env python3
"""Diagnostic: record WHEN UserPromptSubmit fires.

The question this answers: does the hook fire when a prompt is SUBMITTED --
which would make it a usable "they are back at the terminal" signal for a
blocked hook -- or only when the prompt is DELIVERED, which is too late to be
useful?

Established already, by measurement: a queued prompt leaves NO trace on disk.
`history.jsonl` is written on delivery (observed seven seconds after the hook
released), and the tty's timestamps track Claude Code's own rendering rather
than input. So a hook we register ourselves is the last candidate.

Writes one line and exits 0 emitting nothing, so it cannot affect the prompt.
"""
import json
import pathlib
import sys
import time

try:
    payload = json.loads(sys.stdin.read() or "{}")
except Exception:
    payload = {}

line = (f"{time.strftime('%H:%M:%S')}  UserPromptSubmit fired  "
        f"session={str(payload.get('session_id', ''))[:8]}  "
        f"keys={sorted(payload)}\n")
path = pathlib.Path.home() / ".claude/cliq/prompt-probe.log"
try:
    with path.open("a") as fh:
        fh.write(line)
except OSError:
    pass
