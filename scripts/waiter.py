"""Waiter registry: how a blocked hook and the machine's single daemon meet.

A hook that wants an answer arms a waiter file and then watches that file.
The daemon holds the one long-poll for the whole machine and writes answers
into waiter files. Hooks never talk to the relay while waiting, which is
what keeps N concurrent sessions down to one in-flight relay request.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from cliqlib import DAEMON_LOCK, SESSIONS_DIR, WAITERS_DIR, log

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


def arm(request_id: str, *, session_id: str, kind: str, cwd: str,
        expires_at: float | None = None) -> Path:
    WAITERS_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(request_id)
    p.write_text(json.dumps({
        "request_id": request_id,
        "session_id": session_id,
        "kind": kind,
        "cwd": cwd,
        "pid": os.getpid(),
        "armed_at": time.time(),
        # Written down because a hook killed at its timeout never runs its
        # own cleanup. Without this, abandoned waiters pile up and make every
        # later bare reply ambiguous.
        "expires_at": expires_at,
    }))
    return p


def disarm(request_id: str) -> None:
    _path(request_id).unlink(missing_ok=True)


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
    for p in sorted(WAITERS_DIR.glob("*.json")):
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
    """Block until the daemon resolves this waiter, or the deadline passes."""
    p = _path(request_id)
    while time.time() < deadline:
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
        if "resolved" in data:
            disarm(request_id)
            return data["resolved"]
        time.sleep(tick)
    disarm(request_id)
    return None


def route(text: str, waiters: list[dict]) -> tuple[str | None, dict | None]:
    """Decide which armed waiter a Cliq reply belongs to.

    Returns (request_id, payload). A (None, None) result means nothing
    matched -- the caller should treat it as a late reply.
    """
    ids = {w["request_id"] for w in waiters}

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

    m = ADDRESSED_RE.match(text)
    if m and m.group(1).lower() in ids:
        return m.group(1).lower(), {"type": "answer", "text": m.group(2).strip()}

    if len(waiters) == 1:
        return waiters[0]["request_id"], {"type": "answer", "text": text.strip()}

    return None, None


def ensure_daemon() -> None:
    """Start the machine's daemon if it isn't already holding the lock."""
    if _daemon_alive():
        return
    script = Path(__file__).with_name("cliqd.py")
    try:
        subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        log(f"could not start daemon: {exc}")


def _daemon_alive() -> bool:
    import fcntl
    if not DAEMON_LOCK.exists():
        return False
    try:
        with DAEMON_LOCK.open("r+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True          # someone holds it: daemon is up
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
    except Exception:
        return False


# -- session liveness -------------------------------------------------------

def register_session(session_id: str, cwd: str) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SESSIONS_DIR / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id, "cwd": cwd,
        "pid": os.getppid(), "started": time.time(),
    }))


def unregister_session(session_id: str) -> None:
    (SESSIONS_DIR / f"{session_id}.json").unlink(missing_ok=True)


def session_live(session_id: str) -> bool:
    """True if that session still has a running process.

    Used to avoid `--bg --resume` against a live session, which forks a copy
    instead of continuing it.
    """
    p = SESSIONS_DIR / f"{session_id}.json"
    try:
        pid = json.loads(p.read_text())["pid"]
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        p.unlink(missing_ok=True)
        return False
