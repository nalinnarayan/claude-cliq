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
    """Shorten a path for display, without the tilde.

    Cliq reads ~text~ as strikethrough, so a message containing two paths
    written as ~/foo strikes through everything between them. Using an
    ellipsis sidesteps it entirely.
    """
    try:
        return cwd.replace(str(Path.home()), "…")
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


def defuse_cliq_markup(text: str) -> str:
    """Neutralise stray tildes.

    Cliq reads ~text~ as strikethrough. Claude's output contains tildes in
    paths and in approximations ("~5 minutes"), and any two of them in one
    message strike out everything between.
    """
    return (text or "").replace("~", "∼")        # U+223C, looks the same


def summarize(text: str, limit: int = MAX_SUMMARY) -> str:
    text = defuse_cliq_markup(text).strip()
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
         hint: str = "", answers: list[dict] | None = None) -> bool:
    try:
        relay.notify(kind=kind, text=text, request_id=request_id,
                     session_id=session_id, cwd=cwd,
                     expects_reply=expects_reply, hint=hint, answers=answers)
        return True
    except RelayError as exc:
        log(f"{kind} notify failed: {exc}")
        return False


def relay_for(cfg: Config) -> Relay:
    return Relay(cfg)


def block_for_reply(cfg: Config, request_id: str, *, session_id: str,
                    kind: str, cwd: str, tool_use_id: str = "") -> dict | None:
    """Arm a waiter and hold this session until the reply arrives.

    The ONE place that knows which transport is live. Everything downstream --
    waiting on the file, routing, resolving, the spool -- is shared, so the
    half that differs is "what wakes the collector" and nothing else.
    """
    window = cfg.permission_wait_seconds if kind == "permission" else cfg.wait_seconds
    deadline = time.time() + window
    waiter.arm(request_id, session_id=session_id, kind=kind, cwd=cwd,
               expires_at=deadline, tool_use_id=tool_use_id)
    try:
        import collect
        return collect.wait_for(cfg, relay_for(cfg), request_id, deadline)
    except Exception as exc:
        # A hook that raises here takes the whole turn down. Releasing the
        # session to the terminal is always better than that.
        log(f"collect failed, releasing to the terminal: {exc}")
        waiter.disarm(request_id)
        return None


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
