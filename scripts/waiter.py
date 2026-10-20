"""Waiter registry: how a blocked hook and the machine's fetch leader meet.

A hook that wants an answer arms a waiter file and then watches that file.
Exactly one blocked hook per machine -- the fetch leader, elected in
collect.py -- holds the Data Streams connection, fetches, and writes answers
into these files. Everyone else just watches their own. That keeps N
concurrent sessions down to one socket and one read per reply.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from cliqlib import WAITERS_DIR, log

# "yes abcde" / "no abcde" -- a permission verdict.
VERDICT_RE = re.compile(r"^\s*(y|yes|ok|approve|n|no|deny|reject)\b[\s:,]*([a-z]{5})?\s*$", re.I)
# "all" / "yes all" / "approve all abcde" -- approve everything in this
# session for a while, instead of answering each prompt one at a time.
BLANKET_RE = re.compile(r"^\s*(?:y|yes|ok|approve|allow)?\s*all\b[\s:,]*([a-z]{5})?\s*$", re.I)

# "abcde do the thing" -- an answer addressed to one waiter.
ADDRESSED_RE = re.compile(r"^\s*([a-z]{5})\b[\s:,-]+(.+)$", re.I | re.S)

_AFFIRMATIVE = {"y", "yes", "ok", "approve"}


def _path(request_id: str) -> Path:
    return WAITERS_DIR / f"{request_id}.json"


def session_title(session_id: str, cwd: str) -> str:
    """The name Claude Code's own /resume picker shows for this session.

    Claude Code writes `{"type":"ai-title","aiTitle":…,"sessionId":…}` into the
    session transcript at ~/.claude/projects/<cwd with / as ->/<id>.jsonl, and
    rewrites it as the conversation moves on. That is the only human-readable
    name a session has -- a hook is otherwise given a UUID and a folder.

    Read from the END of the file: these transcripts run to tens of megabytes
    and the title is written repeatedly, so the newest one is near the tail.
    Entirely best-effort -- a title is nice, never load-bearing.
    """
    if not session_id or not cwd:
        return ""
    try:
        path = (Path.home() / ".claude" / "projects"
                / cwd.replace("/", "-") / f"{session_id}.jsonl")
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256_000))
            tail = fh.read().decode("utf-8", "replace")
        for line in reversed(tail.splitlines()):
            if '"ai-title"' not in line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue            # a partial first line from the seek
            if data.get("type") == "ai-title" and data.get("aiTitle"):
                return str(data["aiTitle"])
    except Exception:
        pass
    return ""


def arm(request_id: str, *, session_id: str, kind: str, cwd: str,
        expires_at: float | None = None, tool_use_id: str = "",
        topic: str = "") -> Path:
    """Arm a waiter.

    `topic` is a short note on what this session is waiting about. Claude Code
    gives a hook a session_id (a UUID), a cwd, and the last message -- there is
    no session NAME to show anyone. The folder is recognisable for some
    projects and meaningless for others, so the question itself is carried
    here and shown in the chooser.
    """
    WAITERS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(request_id)
    p.write_text(json.dumps({
        "request_id": request_id,
        "session_id": session_id,
        "kind": kind,
        "cwd": cwd,
        "topic": (topic or "").strip()[:160],
        # Looked up once, here, rather than when the chooser runs: the
        # transcripts are large and a blocked hook has time to spare, whereas
        # the collector is on the path of every reply.
        "title": session_title(session_id, cwd),
        "pid": os.getpid(),
        "armed_at": time.time(),
        # Written down because a hook killed at its timeout never runs its
        # own cleanup. Without this, abandoned waiters pile up and make every
        # later bare reply ambiguous.
        "expires_at": expires_at,
        # Lets PostToolUse cancel this waiter: if the permission was answered
        # at the terminal instead, the tool runs and we must stop waiting on
        # Cliq -- otherwise the waiter lingers and poisons every later reply.
        "tool_use_id": tool_use_id,
    }))
    return p


def disarm(request_id: str) -> None:
    _path(request_id).unlink(missing_ok=True)


def disarm_for_tool(tool_use_id: str) -> int:
    """Drop any waiter whose tool call has since been settled elsewhere."""
    if not tool_use_id or not WAITERS_DIR.exists():
        return 0
    n = 0
    for p in WAITERS_DIR.glob("*.json"):
        try:
            if json.loads(p.read_text()).get("tool_use_id") == tool_use_id:
                p.unlink(missing_ok=True)
                n += 1
        except Exception:
            continue
    return n


def resolve(request_id: str, payload: dict) -> bool:
    """Daemon side: hand an answer to a waiting hook."""
    p = _path(request_id)
    try:
        data = json.loads(p.read_text())
    except Exception:
        return False
    data["resolved"] = payload
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)
    return True


# Belt and braces for a waiter with no deadline recorded, or whose owning
# process died without one.
_MAX_WAITER_AGE = 2 * 60 * 60


def active() -> list[dict]:
    """Armed waiters, with dead ones swept as we go."""
    out = []
    if not WAITERS_DIR.exists():
        return out
    now = time.time()
    for p in WAITERS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            p.unlink(missing_ok=True)
            continue
        deadline = data.get("expires_at")
        stale = (deadline is not None and now > deadline) or \
                (now - data.get("armed_at", now) > _MAX_WAITER_AGE)
        if stale or not _owner_alive(data):
            p.unlink(missing_ok=True)
            continue
        out.append(data)
    # Newest first: "the most recent request" is a meaningful notion, where
    # ordering by the random request id was not.
    out.sort(key=lambda w: w.get("armed_at", 0), reverse=True)
    return out


def _owner_alive(data: dict) -> bool:
    """False once the hook process that armed this waiter has gone."""
    pid = data.get("pid")
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def wait(request_id: str, deadline: float, tick: float = 0.2) -> dict | None:
    """Watch this waiter until it is resolved, or `deadline` passes.

    Used by a hook that is NOT the machine's fetch leader: the leader does the
    collecting and resolves this file.

    `deadline` is a MOMENT TO STOP WATCHING, not the end of the question's
    window -- collect.wait_for() calls this in short slices so it can re-check
    whether leadership has fallen vacant. So timing out here does NOT disarm:
    the caller owns the waiter's lifetime and knows whether the window is
    actually over.

    Getting that wrong was a real bug. This used to disarm on timeout, from
    when its deadline WAS the window. Once it was called in ten-second slices,
    the first slice disarmed the waiter, the caller read that as "answered
    elsewhere", and every second concurrent session stopped waiting after ten
    seconds.
    """
    p = _path(request_id)
    while time.time() < deadline:
        try:
            data = json.loads(p.read_text())
        except FileNotFoundError:
            # Cancelled: the question was settled without us, so stop holding
            # the session rather than waiting out the whole window.
            return None
        except Exception:
            data = {}
        if "resolved" in data:
            # Disarming on RESOLVE is right: the payload has been taken, so
            # the file has done its job.
            disarm(request_id)
            return data["resolved"]
        time.sleep(tick)
    return None


def armed(request_id: str) -> bool:
    """Is this waiter still waiting?

    Tells "my window lapsed" from "someone disarmed me" -- `wait()` returns
    None for both, and the caller must stop in the second case. A waiter file
    vanishes when the tool settles at the terminal (on_settled.py) or when it
    is resolved.
    """
    return _path(request_id).exists()


def route(text: str, waiters: list[dict], *, bound: bool = False
          ) -> tuple[str | None, dict | None]:
    """Decide which armed waiter a Cliq reply belongs to.

    `bound` means the reply already named its request -- a button tap, or a
    swipe-reply carrying the id back. Only a bound reply may answer a
    permission: a typed "yes" must never approve a command, because there is
    no way to be sure which request the person meant.

    An `ask` waiter is the mirror image. It is waiting for one of Claude's own
    options, so "no" is an answer and not a refusal, and "all" is an answer and
    not a blanket approval. Those words reach the verdict regexes below first,
    so an ask has to step around them.

    Returns (request_id, payload). A (None, None) result means nothing
    matched -- the caller decides what to ask.
    """
    ids = {w["request_id"] for w in waiters}
    if not bound:
        # Permissions are answered by tapping the buttons on the message.
        waiters = [w for w in waiters if w.get("kind") != "permission"]
        if not waiters:
            return None, None

    if len(waiters) == 1 and waiters[0].get("kind") == "ask":
        return waiters[0]["request_id"], {"type": "answer", "text": text.strip()}

    m = BLANKET_RE.match(text)
    if m:
        rid = (m.group(1) or "").lower()
        target = rid if rid in ids else None
        if target is None:
            perms = [w for w in waiters if w.get("kind") == "permission"]
            if len(perms) == 1:
                target = perms[0]["request_id"]
            elif len(waiters) == 1:
                target = waiters[0]["request_id"]
        if target:
            return target, {"type": "verdict", "allow": True,
                            "scope": "session", "text": text}
        return None, None

    m = VERDICT_RE.match(text)
    if m:
        word, rid = m.group(1).lower(), (m.group(2) or "").lower()
        allow = word in _AFFIRMATIVE
        if rid and rid in ids:
            return rid, {"type": "verdict", "allow": allow, "text": text}
        perms = [w for w in waiters if w.get("kind") == "permission"]
        if not rid and len(perms) == 1:
            return perms[0]["request_id"], {"type": "verdict", "allow": allow, "text": text}
        if not rid and len(waiters) == 1:
            return waiters[0]["request_id"], {"type": "verdict", "allow": allow, "text": text}
        return None, None

    if len(waiters) == 1:
        return waiters[0]["request_id"], {"type": "answer", "text": text.strip()}

    return None, None
