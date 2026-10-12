"""Shared hook entry plumbing.

The rule every hook here follows: never break the session. Any failure --
unpaired, relay down, bad payload -- exits 0 and lets Claude Code carry on
exactly as it would without this plugin installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cliqd
import waiter
from cliqlib import (Config, Relay, RelayError, is_away, load_config, log,
                     new_request_id)

MAX_SUMMARY = 1400          # Cliq caps a message at 5000 chars; stay well under


def read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def ready() -> tuple[Config, Relay] | tuple[None, None]:
    cfg = load_config()
    if not cfg.enabled or not cfg.paired:
        return None, None
    return cfg, Relay(cfg)


def short_cwd(cwd: str) -> str:
    try:
        return cwd.replace(str(Path.home()), "~")
    except Exception:
        return cwd


def git_branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=cwd or None, capture_output=True, text=True,
                             timeout=3)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


def summarize(text: str, limit: int = MAX_SUMMARY) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def context_line(cwd: str) -> str:
    bits = [short_cwd(cwd)] if cwd else []
    branch = git_branch(cwd)
    if branch:
        bits.append(branch)
    return " · ".join(bits)


def send(relay: Relay, *, kind: str, text: str, request_id: str,
         session_id: str = "", cwd: str = "", expects_reply: bool = False,
         hint: str = "") -> bool:
    try:
        relay.notify(kind=kind, text=text, request_id=request_id,
                     session_id=session_id, cwd=cwd,
                     expects_reply=expects_reply, hint=hint)
        if session_id:
            cliqd.note_notified(session_id, cwd)
        return True
    except RelayError as exc:
        log(f"{kind} notify failed: {exc}")
        return False


def block_for_reply(cfg: Config, request_id: str, *, session_id: str,
                    kind: str, cwd: str) -> dict | None:
    """Arm a waiter and hold this session until the daemon answers it."""
    deadline = time.time() + cfg.wait_seconds
    waiter.arm(request_id, session_id=session_id, kind=kind, cwd=cwd,
               expires_at=deadline)
    waiter.ensure_daemon()
    return waiter.wait(request_id, deadline=deadline)


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
