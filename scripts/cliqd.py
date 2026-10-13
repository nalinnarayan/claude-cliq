#!/usr/bin/env python3
"""Per-machine daemon: holds the single relay long-poll and routes replies.

One of these per laptop, guarded by a lock. Hooks never poll the relay
themselves -- every held long-poll costs a Catalyst concurrency slot and
GB-seconds, so N sessions polling separately would cost N times as much.

A reply only ever reaches a session whose hook is still waiting. There is
no way to feed an idle interactive session from outside, so a reply that
arrives too late is answered with "pick it up at the terminal" rather than
with machinery that pretends otherwise.
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import waiter
from cliqlib import (DAEMON_LOCK, STATE_DIR, Config, RateLimited, Relay,
                     RelayError, backoff_sleep, is_away, load_config, log,
                     new_request_id)

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
HOLD_FOR = 30 * 60


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


def handle_unmatched(relay: Relay, text: str) -> None:
    """A reply that nothing is waiting for.

    An interactive session sitting at its prompt cannot be fed from outside,
    so there is nothing to offer here but the truth. The one case worth
    holding for is a session that is still alive and merely busy: its `Stop`
    hook fires at the end of the turn, which is usually seconds away.
    """
    last = _last_notified()
    session_id = (last or {}).get("session_id", "")
    fresh = bool(last) and time.time() - last.get("ts", 0) <= IDLE_GRACE

    if fresh and session_id and waiter.session_live(session_id):
        hold_reply(text, session_id)
        # Deliberately does not claim to know whether Claude is mid-task or
        # idle at the prompt -- we cannot tell from here, and guessing wrong
        # reads as the bridge being confused.
        _notify(relay, "Nothing's waiting on a reply just now — I've kept "
                       "that and will pass it on when Claude next pauses.",
                kind="ack")
        return

    where = ((last or {}).get("cwd") or "").replace(str(Path.home()), "…")
    # Naming the folder only helps when there is one to name; "that session"
    # with nothing behind it reads as the bridge having lost track.
    what = f"The session in {where} isn't listening any more" if where else \
           "Nothing here is waiting on a reply"
    _notify(relay,
            f"{what} — a session can only take a reply while it's waiting on "
            f"one. Pick the work up at the terminal and I'll ping you the next "
            f"time it pauses.", kind="ack")


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
    # Away mode is the user saying "I've stepped out, expect replies". Exiting
    # then is exactly backwards: a reply arrives, nothing is listening, and it
    # sits in the relay until the user is back at the terminal -- which is the
    # situation away mode exists to avoid.
    if is_away():
        return True
    if waiter.active():
        return True
    last = _last_notified()
    if last and time.time() - last.get("ts", 0) < IDLE_GRACE:
        return True
    return bool(_read_held())


def _log_signal(signum, _frame):
    """Without this a kill leaves no trace, and a daemon that is simply gone
    looks identical to one that never started."""
    import signal as _sig
    log(f"daemon received {_sig.Signals(signum).name} — exiting")
    raise SystemExit(0)


def main() -> int:
    import signal
    for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_s, _log_signal)
        except Exception:
            pass

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
