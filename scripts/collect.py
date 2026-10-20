#!/usr/bin/env python3
"""Collect replies for this machine, with exactly one socket open.

Replaces the daemon. A blocked hook either becomes the machine's *fetch
leader* -- holding the one Data Streams connection and fetching on its behalf
-- or waits on its own waiter file while whoever is leader resolves it. The
handoff mechanism is the waiter files, which is what the daemon used too, so
nothing about routing changes.

Why a leader rather than a socket per hook: the wake label is per ACCOUNT, so
every blocked hook on the machine sees a matching wake and would fetch, which
is N reads per reply instead of one. And `/await` is destructive, so the
loser of that race gets nothing while the winner holds someone else's reply in
process memory. One leader, one socket, one read.

Why not a background process: Claude Code reaps what a hook spawns, which is
the cause of every "I replied and got no reply" in docs/FINDINGS.md. A leader
that *is* a blocked hook cannot be reaped without the thing it serves going
away too.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

import spool
import waiter
import wake
from cliqlib import STATE_DIR, Config, Relay, RelayError, log, new_request_id

LEADER_LOCK = STATE_DIR / "fetch.lock"
# Remembers that the stream could not be reached, so a laptop behind a proxy
# that blocks wss:// stops paying a connect timeout on every blocked hook.
STREAM_DOWN = STATE_DIR / "stream-down"
# Which transport actually SERVED the last reply. Reporting the configured one
# is close to useless: the poll fallback works, so a broken stream looks
# exactly like a working one until someone reads the log. That is how a
# per-device connection name silently cost a hundred times the reads.
SERVED_PATH = STATE_DIR / "served.json"

_mode = "?"

# How long a follower waits on its own waiter file before checking whether
# leadership has fallen vacant. Short enough that a leader dying is noticed
# quickly, long enough to cost nothing.
LEADER_RETRY = 10.0


class _BudgetSpent(Exception):
    """The fallback has asked often enough and is giving up for this window.

    Raised rather than returned, because wait_for() re-enters the collector on
    every leadership iteration -- a per-call counter was simply reset each
    time, and 3 fetches became 385.
    """

# Socket budget for a non-blocking fetch. Not derived from the server's hold
# time: a cold Catalyst container loading the vendored Data Streams SDK can
# take longer than a few seconds, and a hook that gives up here releases the
# whole session to the terminal.
FETCH_TIMEOUT = 45


class _Leadership:
    """Non-blocking flock. Absent means someone else is leading."""

    def __init__(self):
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = LEADER_LOCK.open("a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def __exit__(self, *_):
        if self._fh:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
            except Exception:
                pass
        return False


def _stream_available(cfg: Config) -> bool:
    """False while the stream is in its cooldown after a failure."""
    try:
        failed_at = float(STREAM_DOWN.read_text().strip())
    except Exception:
        return True
    if time.time() - failed_at < cfg.stream_retry_after_seconds:
        return False
    STREAM_DOWN.unlink(missing_ok=True)      # cooldown served; try again
    return True


def _mark_stream_down(reason: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STREAM_DOWN.write_text(str(time.time()))
    except Exception:
        pass
    log(f"collect: stream marked unusable ({reason}); polling until it is retried")


def _mark_stream_up() -> None:
    if STREAM_DOWN.exists():
        STREAM_DOWN.unlink(missing_ok=True)
        log("collect: stream reachable again")


def _notify(relay: Relay, text: str, kind: str = "ack", *,
            answers: list[dict] | None = None, hint: str = "") -> None:
    try:
        relay.notify(kind=kind, text=text, request_id=new_request_id(),
                     answers=answers, hint=hint)
    except RelayError as exc:
        log(f"collect: notify failed: {exc}")


def _ask_which(relay: Relay, item: dict, armed: list[dict]) -> None:
    """A plain reply with several sessions waiting: ask, never guess.

    One button per waiting session, each carrying THAT session's request id --
    which is what makes a tap unambiguous by construction rather than by
    inference. The typed text stays in the spool, so answering costs one tap
    and nothing is retyped.

    The leader that asks may well be gone by the time the tap arrives. That is
    fine: a tap is just another queued reply, and it names its target, so
    whichever hook collects it next can apply it.
    """
    answers = []
    for w in armed[:5]:
        where = (w.get("cwd") or "").rstrip("/").split("/")[-1] or "session"
        kind = "permission" if w.get("kind") == "permission" else "question"
        answers.append({"label": f"{where} ({kind})", "value": "pick",
                        "req": w["request_id"]})
    more = "" if len(armed) <= 5 else f"\n\n({len(armed) - 5} more not shown)"
    preview = item["text"][:160]
    _notify(relay,
            f"Which one is this for?{more}\n\n_{preview}_",
            kind="choose", answers=answers,
            hint="tap one — I've kept what you typed")
    spool.mark(item["id"], needs_pick=True, asked=True)
    log(f"collect: asked which of {[w['request_id'] for w in armed]} "
        f"a plain reply belongs to")


def _route_spooled(cfg: Config, relay: Relay, want: str) -> dict | None:
    """Hand everything spooled to whichever waiter it belongs to.

    Returns the payload for `want` if one of them answered it, so the leader
    does not have to read back its own file.
    """
    stale = spool.prune(cfg.stale_after_seconds)
    if stale:
        preview = "\n".join(f"- _{s['text'][:120]}_" for s in stale[:3])
        mins = int(cfg.stale_after_seconds // 60)
        _notify(relay,
                f"These came in more than {mins} minutes ago, so I haven't "
                f"passed them on — answering a question with them now would "
                f"be worse than dropping them:\n\n{preview}\n\n"
                f"Send them again if they're still what you meant.")

    mine = None
    taken: list[str] = []
    for item in spool.items():
        armed = waiter.active()
        if not armed:
            break                       # nothing to give it to; leave it spooled
        text, bound_id = item["text"], item["request_id"]

        # A chooser tap: it names a session and carries no text of its own,
        # because the text it is about is the one we held when we asked.
        if bound_id and not text:
            held = spool.oldest_needing_pick()
            picked = next((w for w in armed if w["request_id"] == bound_id), None)
            if held and picked:
                rid, payload = waiter.route(held["text"], [picked], bound=True)
                if rid and waiter.resolve(rid, payload):
                    log(f"collect: chooser sent the held reply to {rid}")
                    taken += [item["id"], held["id"]]
                    if rid == want:
                        mine = payload
                        waiter.disarm(rid)
                    continue
            log(f"collect: a pick for {bound_id} matched nothing to apply")
            taken.append(item["id"])
            continue

        # Ambiguous and already asked about: it may ONLY be resolved by a tap.
        # Routing it now is exactly the silent misdelivery the flag prevents.
        if item.get("needs_pick"):
            continue

        target = next((w for w in armed if w["request_id"] == bound_id), None)
        if bound_id and not target:
            # Named a request that is no longer waiting. Dropped rather than
            # re-routed: a reply that named one request and missed is not
            # evidence about any other, and re-routing it can put one
            # question's answer into another's waiter.
            log(f"collect: {bound_id} is no longer waiting — dropping")
            taken.append(item["id"])
            _notify(relay, "Nothing's waiting on that one any more — it was "
                           "answered, or its window closed.")
            continue

        if target:
            rid, payload = waiter.route(text, [target], bound=True)
        else:
            rid, payload = waiter.route(text, armed)

        if not rid or not waiter.resolve(rid, payload):
            # Nothing matched. With one waiter that cannot happen, so this is
            # the several-sessions case: ask rather than guess, and mark the
            # reply so it can never drift onto a later question.
            if not bound_id and len(armed) > 1 and not item.get("asked"):
                _ask_which(relay, item, armed)
            continue
        log(f"collect: routed to {rid}: {payload.get('type')}")
        taken.append(item["id"])
        if rid == want:
            mine = payload
            # Our own waiter is cleared here rather than left resolved. Only
            # the hook that is WAITING on a file disarms it, and we are not
            # waiting -- we are the leader, taking the payload directly. Left
            # armed it would keep showing up in active(), making the next
            # reply look ambiguous and firing the chooser for no reason.
            waiter.disarm(rid)
    spool.drop(taken)
    return mine


def _drain(cfg: Config, relay: Relay, want: str) -> dict | None:
    """One fetch, spooled before it is routed."""
    try:
        # wait=0: the doorbell tells us when to look, so the server must not
        # hold the request. The generous socket timeout is for cold starts.
        res = relay.poll(since=0, wait=0, timeout=FETCH_TIMEOUT)
    except RelayError as exc:
        log(f"collect: fetch failed: {exc}")
        return None
    rows = res.get("messages", [])
    if rows:
        log(f"collect: fetched {len(rows)}")
        try:
            SERVED_PATH.write_text(json.dumps({"mode": _mode, "ts": time.time(),
                                               "count": len(rows)}))
        except Exception:
            pass
        spool.add(rows)                 # durable BEFORE anything is routed
    return _route_spooled(cfg, relay, want)


def _lead(cfg: Config, relay: Relay, want: str, deadline: float) -> dict | None:
    """Hold the socket and fetch for the machine until `want` is answered."""
    try:
        ticket = relay.stream_ticket()
    except RelayError as exc:
        # No ticket means no doorbell, but the reply may already be queued and
        # the window has barely started. Polling is strictly better than
        # handing the session back.
        log(f"collect: no stream ticket ({exc}) — polling instead")
        return _poll_until(cfg, relay, want, deadline)
    wake_id = ticket.get("wake_id") or ""

    # Fetch once before listening. This covers every gap in one read: a wake
    # published while nothing was connected, a publish that failed after the
    # insert succeeded, and anything spooled but unrouted by a previous leader.
    got = _drain(cfg, relay, want)
    if got:
        return got

    global _mode
    _mode = "stream"
    _mark_stream_up()               # we are connected, so clear any cooldown

    def on_wake(body):
        if not isinstance(body, dict) or body.get("w") != wake_id:
            return None                 # another account's bell
        return _drain(cfg, relay, want)

    # No ticket renewal on a retry, deliberately. Re-minting for the same
    # connection_name is refused -- "Duplicate value for connection_name" --
    # because close() preserves the subscriber session by design, so the name
    # is still taken. Retrying with the SAME ticket is what actually works,
    # which is what the transient SESSION_EXPIRED needs anyway.
    return wake.listen({"url": ticket["url"], "key": ticket["key"],
                        "wss_id": ticket["wss_id"]},
                       deadline=deadline, on_wake=on_wake, log=log)


def wait_for(cfg: Config, relay: Relay, request_id: str,
             deadline: float) -> dict | None:
    """Block until `request_id` is answered, or `deadline` passes.

    Becomes the machine's fetch leader if the post is vacant; otherwise waits
    on its own waiter file and lets the leader deliver. If the leader dies its
    lock is released, and the next follower to come round takes over -- so a
    leader going away costs one `LEADER_RETRY`, not a lost reply.
    """
    while time.time() < deadline:
        with _Leadership() as leading:
            if leading:
                use_stream = cfg.transport == "stream" and _stream_available(cfg)
                try:
                    got = (_lead(cfg, relay, request_id, deadline) if use_stream
                           else _poll_until(cfg, relay, request_id, deadline))
                except _BudgetSpent:
                    return None         # done asking for this window
                except wake.WakeError as exc:
                    # Never strand the hook on a stream problem. Polling is
                    # more expensive, not broken, and the cooldown stops the
                    # next hook rediscovering the same blocked port.
                    _mark_stream_down(str(exc))
                    _notify(relay, "⚠️ The Cliq doorbell isn't reachable, so "
                                   "I'm falling back to polling. Replies still "
                                   "work but cost far more, and I'll stop "
                                   "asking after a while.")
                    got = _poll_until(cfg, relay, request_id, deadline)
                if got:
                    return got
                if not waiter.armed(request_id):
                    return None         # answered at the terminal, or expired
                continue

        # Someone else is leading. Watch our own file; they will resolve it.
        got = waiter.wait(request_id, min(deadline, time.time() + LEADER_RETRY))
        if got:
            return got
        if not waiter.armed(request_id):
            return None
    return None


def _poll_until(cfg: Config, relay: Relay, want: str,
                deadline: float) -> dict | None:
    """The escape hatch, and `transport: "poll"`.

    Deliberately the same leader doing the same routing -- only the trigger
    differs. Keeping the daemon for this would have meant keeping everything
    the daemon dragged with it.
    """
    global _mode
    _mode = "poll"
    gap = cfg.poll_fallback_seconds
    spent = 0
    while time.time() < deadline:
        got = _drain(cfg, relay, want)
        spent += 1
        if got:
            return got
        if not waiter.armed(want):
            return None
        if spent >= cfg.poll_fallback_budget:
            # Stop rather than keep spending. A degraded mode that quietly
            # drains the month's allowance is how this design's predecessor
            # died; the window is handed back to the terminal instead.
            log(f"collect: poll budget spent ({spent} fetches) — releasing to "
                f"the terminal rather than burning reads")
            _notify(relay, "I can't reach the Cliq doorbell, and I've used up "
                           "the fallback's budget asking. Carry on at the "
                           "terminal — replying here won't reach me until the "
                           "connection is back.")
            raise _BudgetSpent()
        time.sleep(min(gap, max(0.0, deadline - time.time())))
        gap = min(gap * 2, cfg.poll_fallback_max_seconds)
    return None
