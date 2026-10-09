#!/usr/bin/env python3
"""Per-machine daemon: holds the single relay long-poll and routes replies.

One of these per laptop, guarded by a lock. Hooks never poll the relay
themselves -- every held long-poll costs a Catalyst concurrency slot and
GB-seconds, so N sessions polling separately would cost N times as much.

It also owns the late-reply path: a reply that arrives after a hook has
released its session is confirmed in Cliq and then fed back with
`claude --resume <id> --bg`, continuing that same session.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import waiter
from cliqlib import (DAEMON_LOCK, STATE_DIR, Config, RateLimited, Relay,
                     RelayError, backoff_sleep, load_config, log, new_request_id)

CURSOR_PATH = STATE_DIR / "cursor"
LAST_NOTIFIED = STATE_DIR / "last_notified.json"
PENDING_RESUME = STATE_DIR / "pending_resume.json"

# Keep watching this long after the last notification, so a reply that
# arrives once the session has gone quiet still gets picked up.
IDLE_GRACE = 30 * 60
# While nothing is actively waiting, poll at this interval instead of
# holding a continuous long-poll.
IDLE_POLL_EVERY = 30.0


def read_cursor() -> float:
    try:
        return float(CURSOR_PATH.read_text().strip())
    except Exception:
        return time.time()


def write_cursor(v: float) -> None:
    try:
        CURSOR_PATH.write_text(str(v))
    except Exception:
        pass


def note_notified(session_id: str, cwd: str) -> None:
    """Called by hooks via `record_notification` so the daemon knows which
    session a later unattributed reply most likely belongs to."""
    try:
        LAST_NOTIFIED.write_text(json.dumps(
            {"session_id": session_id, "cwd": cwd, "ts": time.time()}))
    except Exception:
        pass


def _last_notified() -> dict | None:
    try:
        return json.loads(LAST_NOTIFIED.read_text())
    except Exception:
        return None


def _take_pending_resume() -> dict | None:
    try:
        data = json.loads(PENDING_RESUME.read_text())
        PENDING_RESUME.unlink(missing_ok=True)
        return data
    except Exception:
        return None


def _set_pending_resume(data: dict) -> None:
    try:
        PENDING_RESUME.write_text(json.dumps(data))
    except Exception:
        pass


def resume_session(session_id: str, cwd: str, text: str) -> tuple[bool, str]:
    """Continue an idle session in the background with `text` as a new turn."""
    if waiter.session_live(session_id):
        return False, "that session is still open in a terminal — answer it there"
    cmd = ["claude", "--resume", session_id, "--bg", text]
    try:
        proc = subprocess.run(cmd, cwd=cwd or None, capture_output=True,
                              text=True, timeout=60)
    except FileNotFoundError:
        return False, "`claude` is not on PATH for the daemon"
    except subprocess.TimeoutExpired:
        return False, "resume timed out"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "resume failed").strip()
        if "not trusted" in detail.lower():
            # `--bg` refuses in an untrusted directory, and the home
            # directory never qualifies. Say what actually fixes it.
            return False, ("that folder isn't trusted yet — open it in a "
                           "terminal with `claude` once and accept the prompt")
        return False, detail[:300]
    return True, (proc.stdout or "").strip()[:300]


def handle_unmatched(relay: Relay, text: str) -> None:
    """A reply nothing was waiting for: the late-reply path."""
    pending = _take_pending_resume()
    if pending:
        rid, payload = waiter.route(text, [{"request_id": pending["request_id"],
                                            "kind": "permission"}])
        if payload and payload.get("type") == "verdict":
            if not payload["allow"]:
                _notify(relay, "Left it alone.", kind="ack")
                return
            ok, detail = resume_session(pending["session_id"], pending["cwd"],
                                        pending["text"])
            msg = ("Resumed in the background — I'll ping you when it's done."
                   if ok else f"Couldn't resume: {detail}")
            _notify(relay, msg, kind="ack")
            return
        # Not a yes/no: treat it as a fresh late reply and re-ask below.

    last = _last_notified()
    if not last or time.time() - last.get("ts", 0) > IDLE_GRACE:
        _notify(relay, "Nothing is waiting on a reply right now.", kind="ack")
        return

    session_id, cwd = last["session_id"], last.get("cwd", "")
    if waiter.session_live(session_id):
        _notify(relay, "That session is still open in your terminal — "
                       "answer it there and it'll keep going.", kind="ack")
        return

    rid = new_request_id()
    _set_pending_resume({"request_id": rid, "session_id": session_id,
                         "cwd": cwd, "text": text, "ts": time.time()})
    where = cwd.replace(str(Path.home()), "~") or "that session"
    _notify(relay,
            f"That session in {where} has gone idle.\n"
            f"Resume it in the background with your reply?",
            kind="confirm", request_id=rid,
            hint=f"reply `yes {rid}` or `no {rid}`")


def _notify(relay: Relay, text: str, *, kind: str, request_id: str = "",
            hint: str = "") -> None:
    try:
        relay.notify(kind=kind, text=text, request_id=request_id or new_request_id(),
                     hint=hint)
    except RelayError as exc:
        log(f"daemon notify failed: {exc}")


def should_keep_running() -> bool:
    if waiter.active():
        return True
    last = _last_notified()
    if last and time.time() - last.get("ts", 0) < IDLE_GRACE:
        return True
    return PENDING_RESUME.exists()


def main() -> int:
    cfg: Config = load_config()
    if not cfg.paired or not cfg.enabled:
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = DAEMON_LOCK.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0                      # another daemon already has it

    log("daemon up")
    relay = Relay(cfg)
    cursor = read_cursor()
    failures = 0
    last_idle_poll = 0.0

    try:
        while should_keep_running():
            armed = waiter.active()
            if not armed:
                # Nothing blocking: poll lazily rather than holding a leg open.
                if time.time() - last_idle_poll < IDLE_POLL_EVERY:
                    time.sleep(1.0)
                    continue
                last_idle_poll = time.time()
                wait_for = 1
            else:
                wait_for = cfg.poll_seconds

            try:
                res = relay.poll(since=cursor, wait=wait_for)
                failures = 0
            except RateLimited:
                log("relay rate limited; backing off")
                backoff_sleep(min(failures, 5), base=5.0, cap=120.0)
                failures += 1
                continue
            except RelayError as exc:
                log(f"poll failed: {exc}")
                backoff_sleep(min(failures, 5))
                failures += 1
                continue

            for msg in res.get("messages", []):
                cursor = max(cursor, float(msg.get("ts", cursor)))
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                armed = waiter.active()
                rid, payload = waiter.route(text, armed)
                if rid and waiter.resolve(rid, payload):
                    log(f"routed reply to {rid}: {payload.get('type')}")
                    continue
                if armed:
                    ids = ", ".join(f"`{w['request_id']}`" for w in armed)
                    _notify(relay,
                            f"Which one? Prefix your reply with an id: {ids}",
                            kind="ack")
                    continue
                handle_unmatched(relay, text)

            cursor = max(cursor, float(res.get("now", cursor)))
            write_cursor(cursor)
    finally:
        write_cursor(cursor)
        log("daemon down")
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
