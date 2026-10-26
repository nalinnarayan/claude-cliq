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
                     send_allowed,
                     new_request_id)

# Was 1400 "to stay well under" Cliq's 5000-character message cap -- which cut
# the end off any substantial turn and appended an ellipsis, losing the part a
# message usually builds to. The relay now splits a long message across several
# Cliq messages, so this only has to stay inside what the relay will actually
# deliver: MAX_PART * MAX_PARTS, less room for the heading and the hint.
MAX_SUMMARY = 20000


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
    """Prepare a turn for Cliq. Only a backstop truncates now.

    The relay splits anything long across several messages, so this no longer
    trims to one message's worth. The cut that remains is a guard against
    something pathological, not a formatting decision, and it says what it is
    rather than trailing off into an ellipsis.
    """
    text = defuse_cliq_markup(text).strip()
    if len(text) <= limit:
        return text
    return (text[:limit].rsplit(" ", 1)[0]
            + f"\n\n[cut here — the message was {len(text)} characters, "
              f"past the {limit} this bridge will carry. The rest is at the "
              f"terminal.]")


def context_line(cwd: str) -> str:
    bits = [short_cwd(cwd)] if cwd else []
    branch = git_branch(cwd)
    if branch:
        bits.append(branch)
    return " · ".join(bits)


def send(relay: Relay, *, kind: str, text: str, request_id: str,
         session_id: str = "", cwd: str = "", expects_reply: bool = False,
         hint: str = "", answers: list[dict] | None = None) -> bool:
    if not send_allowed(cfg_cache().local_sends_per_min):
        # Shed rather than queue. A notification that arrives minutes late is
        # worse than none: the thing it was about has already moved on.
        log(f"{kind} notify shed: this machine has hit its own per-minute pace")
        return False
    try:
        # Defused HERE, at the one place everything outbound passes through,
        # rather than at each call site. It was only applied in summarize(),
        # which only on_stop uses -- so a permission showed its Bash command
        # raw, and a command containing two `~` (two `~/.claude/...` paths, say)
        # had everything between them struck through and both tildes eaten.
        # Reported from Cliq with a screenshot of exactly that.
        relay.notify(kind=kind, text=defuse_cliq_markup(text),
                     request_id=request_id,
                     session_id=session_id, cwd=cwd,
                     expects_reply=expects_reply,
                     hint=defuse_cliq_markup(hint), answers=answers)
        return True
    except RelayError as exc:
        log(f"{kind} notify failed: {exc}")
        return False


_CFG: Config | None = None


def cfg_cache() -> Config:
    """send() predates taking a Config, and threading one through every call
    site for a pacing limit is not worth the churn."""
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def relay_for(cfg: Config) -> Relay:
    return Relay(cfg)


def block_for_reply(cfg: Config, request_id: str, *, session_id: str,
                    kind: str, cwd: str, tool_use_id: str = "",
                    topic: str = "") -> dict | None:
    """Arm a waiter and hold this session until the reply arrives.

    The ONE place that knows which transport is live. Everything downstream --
    waiting on the file, routing, resolving, the spool -- is shared, so the
    half that differs is "what wakes the collector" and nothing else.
    """
    window = cfg.permission_wait_seconds if kind == "permission" else cfg.wait_seconds
    deadline = time.time() + window
    waiter.arm(request_id, session_id=session_id, kind=kind, cwd=cwd,
               expires_at=deadline, tool_use_id=tool_use_id, topic=topic)
    try:
        import collect
        reply = collect.wait_for(cfg, relay_for(cfg), request_id, deadline)
        if reply is None:
            # Only here is the window genuinely over, so only here is it safe
            # to clear the waiter. wait() deliberately leaves it alone.
            waiter.disarm(request_id)
        return reply
    except Exception as exc:
        # A hook that raises here takes the whole turn down. Releasing the
        # session to the terminal is always better than that.
        log(f"collect failed, releasing to the terminal: {exc}")
        waiter.disarm(request_id)
        return None


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
