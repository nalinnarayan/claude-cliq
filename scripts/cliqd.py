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

# The daemon outlives any single session, so it happily keeps running code
# that has since been edited -- which reads as a fix not working. It watches
# its own sources instead and steps aside when they change; the next hook
# starts a fresh one.
_SOURCES = sorted(Path(__file__).parent.glob("*.py"))
_SOURCE_STAMP = {f: f.stat().st_mtime for f in _SOURCES if f.exists()}


def sources_changed() -> str | None:
    for f, was in _SOURCE_STAMP.items():
        try:
            if f.stat().st_mtime != was:
                return f.name
        except OSError:
            return f.name
    return None


CURSOR_PATH = STATE_DIR / "cursor"
LAST_NOTIFIED = STATE_DIR / "last_notified.json"
PENDING_RESUME = STATE_DIR / "pending_resume.json"
HELD_REPLIES = STATE_DIR / "held_replies.json"

# Keep watching this long after the last notification, so a reply that
# arrives once the session has gone quiet still gets picked up.
IDLE_GRACE = 30 * 60
# While nothing is actively waiting, poll at this interval instead of
# holding a continuous long-poll.
IDLE_POLL_EVERY = 30.0
# How long a reply sent while Claude was busy waits for it to pause. Long
# enough to cover a tool call or two, short enough that it can't resurface
# against some unrelated later question.
HOLD_FOR = 10 * 60


def read_cursor() -> float:
    """Opaque cursor, in whatever unit the relay hands back (epoch ms).

    Zero on a fresh daemon is deliberate, not a fallback: rows are deleted
    the moment they are delivered, so anything still queued is undelivered
    and should reach us. Seeding from the local clock would silently drop a
    reply that arrived while no daemon was running.
    """
    try:
        return float(CURSOR_PATH.read_text().strip())
    except Exception:
        return 0.0


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


def _read_held() -> list[dict]:
    try:
        held = json.loads(HELD_REPLIES.read_text())
    except Exception:
        return []
    now = time.time()
    return [h for h in held if now - h.get("ts", 0) < HOLD_FOR]


def _write_held(held: list[dict]) -> None:
    try:
        if held:
            HELD_REPLIES.write_text(json.dumps(held))
        else:
            HELD_REPLIES.unlink(missing_ok=True)
    except Exception:
        pass


def hold_reply(text: str, session_id: str) -> None:
    """Keep a reply that arrived while Claude was busy, to hand over later."""
    held = _read_held()
    held.append({"text": text, "session_id": session_id, "ts": time.time()})
    _write_held(held[-10:])


def deliver_held(relay: Relay) -> None:
    """Hand any held reply to a waiter that has since armed."""
    held = _read_held()
    if not held:
        return
    remaining = []
    for item in held:
        armed = waiter.active()
        rid, payload = waiter.route(item["text"], armed) if armed else (None, None)
        if rid and waiter.resolve(rid, payload):
            log(f"delivered held reply to {rid}")
            _notify(relay, f"Passed that on now that it's paused:\n\n_{item['text'][:200]}_",
                    kind="ack")
        else:
            remaining.append(item)
    _write_held(remaining)


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
        # bound=True: this id was matched explicitly against the pending
        # confirmation, so it is not a guess. Without it the verdict is
        # filtered out as an unbound permission reply and the confirmation
        # becomes unanswerable.
        rid, payload = waiter.route(text, [{"request_id": pending["request_id"],
                                            "kind": "permission"}], bound=True)
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
        # Alive but nothing armed: Claude is mid-task rather than waiting.
        # Hold the reply and hand it over the moment it next pauses, instead
        # of throwing it away and telling them to use the terminal.
        hold_reply(text, session_id)
        # Deliberately does not claim to know whether Claude is mid-task or
        # idle at the prompt -- we cannot tell from here, and guessing wrong
        # reads as the bridge being confused.
        _notify(relay, "Nothing's waiting on a reply just now — I've kept "
                       "that and will pass it on when Claude next pauses.",
                kind="ack")
        return

    rid = new_request_id()
    _set_pending_resume({"request_id": rid, "session_id": session_id,
                         "cwd": cwd, "text": text, "ts": time.time()})
    where = cwd.replace(str(Path.home()), "~") or "that session"
    _notify(relay,
            f"That session in {where} has gone idle.\n"
            f"Resume it in the background with your reply?",
            kind="confirm", request_id=rid,
            answers=[{"label": "Resume", "value": "allow"},
                     {"label": "Leave it", "value": "deny"}],
            hint="tap one")


def ask_which(relay: Relay, text: str, armed: list[dict]) -> None:
    """Several things are waiting: ask which one, with one tap.

    The typed text is held here rather than sent back, so answering costs a
    tap and nothing is retyped. Each button names its own request, so the
    answer cannot land on the wrong session.
    """
    hold_reply(text, "")
    answers = []
    for w in armed[:5]:                     # Cliq allows at most five buttons
        where = (w.get("cwd") or "").rstrip("/").split("/")[-1] or "session"
        kind = "permission" if w.get("kind") == "permission" else "question"
        answers.append({"label": f"{where} ({kind})", "value": "pick",
                        "req": w["request_id"]})
    more = "" if len(armed) <= 5 else f"\n\n({len(armed) - 5} more not shown)"
    _notify(relay,
            f"Which one is that for?{more}",
            kind="choose", answers=answers,
            hint="tap one — I've kept what you typed")


def _notify(relay: Relay, text: str, *, kind: str, request_id: str = "",
            hint: str = "", answers: list[dict] | None = None) -> None:
    try:
        relay.notify(kind=kind, text=text, request_id=request_id or new_request_id(),
                     hint=hint, answers=answers)
    except RelayError as exc:
        log(f"daemon notify failed: {exc}")


def should_keep_running() -> bool:
    if waiter.active():
        return True
    last = _last_notified()
    if last and time.time() - last.get("ts", 0) < IDLE_GRACE:
        return True
    return PENDING_RESUME.exists() or bool(_read_held())


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
            changed = sources_changed()
            if changed:
                log(f"{changed} changed on disk — exiting so the next hook "
                    f"starts a daemon running the current code")
                break
            deliver_held(relay)
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
                bound_id = str(msg.get("request_id") or "").strip().lower()
                # A chooser tap is deliberately empty: it names a request and
                # the text was held when we asked. Everything else with no
                # text is noise.
                if not text and not bound_id:
                    continue
                armed = waiter.active()

                # A swipe-reply carries the id of the message it answers, so
                # it binds exactly -- no guessing, and no id typed by hand.
                target = next((w for w in armed if w["request_id"] == bound_id), None)
                if bound_id and not target:
                    # The id came back but names nothing still waiting --
                    # usually a swipe-reply to a question already answered.
                    log(f"reply names {bound_id}, which is no longer waiting "
                        f"(armed: {[w['request_id'] for w in armed]})")
                elif not bound_id:
                    log("reply carried no id (plain message, or the swiped "
                        "message had no id in it)")

                # A chooser tap carries the id but no text -- the text is the
                # one we held when we asked.
                if target and not text:
                    held = _read_held()
                    if held:
                        item = held.pop()
                        _write_held(held)
                        rid, payload = waiter.route(item["text"], [target], bound=True)
                        if rid and waiter.resolve(rid, payload):
                            log(f"chooser applied held reply to {rid}")
                            continue

                if target:
                    rid, payload = waiter.route(text, [target], bound=True)
                    if rid and waiter.resolve(rid, payload):
                        log(f"bound reply to {rid}: {payload.get('type')}")
                        continue

                rid, payload = waiter.route(text, armed)
                if rid and waiter.resolve(rid, payload):
                    log(f"routed reply to {rid}: {payload.get('type')}")
                    continue
                if armed:
                    ask_which(relay, text, armed)
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
