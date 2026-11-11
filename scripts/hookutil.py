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


# Where the Claude desktop app runs a chat that has no project folder. The
# folder is created per chat and named by ids, so it identifies nothing.
SCRATCH_MARKER = "/Library/Application Support/Claude/scratch-workspaces/"
TITLE_MAX = 60


def context_line(cwd: str, payload: dict | None = None) -> str:
    """The line that says which session a message came from.

    A folder and branch for a real project. For a desktop-app scratch folder
    the path was a screenful of ids -- reported from a phone -- so the
    session's own title is shown instead, the one Claude Code's /resume picker
    uses. Untitled sessions say only "Desktop session": the scratch folder's
    id was asked to be left out, at the cost of two untitled sessions looking
    alike until they get titles.
    """
    if cwd and SCRATCH_MARKER in cwd:
        payload = payload or {}
        title = waiter.session_title(payload.get("session_id", ""), cwd,
                                     payload.get("transcript_path", ""))
        title = " ".join(title.split())
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX].rstrip() + "…"
        return f"Desktop · {title}" if title else "Desktop session"

    # A desktop session in a project folder: named as its sidebar shows it,
    # the folder as the group and then the session, since five sessions
    # under one folder were otherwise indistinguishable. A terminal session
    # is never in the desktop app's files, so it keeps its path.
    title = waiter.desktop_title((payload or {}).get("session_id", "")) if cwd else ""
    if title:
        title = " ".join(title.split())
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX].rstrip() + "…"
        bits = [Path(cwd).name, title]
    else:
        bits = [short_cwd(cwd)] if cwd else []
    branch = git_branch(cwd)
    if branch:
        bits.append(branch)
    return " · ".join(bits)


def send(relay: Relay, *, kind: str, text: str, request_id: str,
         session_id: str = "", cwd: str = "", expects_reply: bool = False,
         hint: str = "", answers: list[dict] | None = None,
         timeout: int = 20) -> bool:
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
                     hint=defuse_cliq_markup(hint), answers=answers,
                     timeout=timeout)
        return True
    except RelayError as exc:
        log(f"{kind} notify failed: {exc}")
        return False


ACK_PREVIEW = 80


def acknowledge(relay: Relay, answer: str, *, session_id: str = "",
                cwd: str = "") -> bool:
    """Tell the person their reply has reached Claude, the moment it does.

    Without this, a reply that reached the session and one that went nowhere
    look identical from the phone: nothing comes back until Claude's answer
    does, which may be minutes. This is sent by the laptop at the instant the
    reply is handed to Claude -- not by the relay on receipt -- so it is the
    one signal that actually means "Claude is working on it".

    It quotes what arrived, because what arrived is not always what was sent:
    a swipe-reply to one's own message delivers only the new text, and the
    collector joins messages sent close together.

    Called just BEFORE the emit that hands the reply over. Claude Code resumes
    only when the hook exits, so nothing can usefully happen after it.
    """
    import collect                        # lazily, as block_for_reply does
    preview = collect._plain(answer)
    if len(preview) > ACK_PREVIEW:
        preview = preview[:ACK_PREVIEW].rstrip() + "…"
    # Five seconds, not the usual twenty. The relay is warm -- it served this
    # very reply a moment ago -- so this only bounds the case where it dies in
    # between, which must not hold Claude up. A lost ack costs nothing; send()
    # already swallows the error, and the reply goes through regardless.
    return send(relay, kind="ack", request_id=new_request_id(),
                text=f'👀 Got it — working on: "{preview}"',
                session_id=session_id, cwd=cwd, timeout=5)


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
