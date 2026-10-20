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

# How long a follower waits on its own waiter file before checking whether
# leadership has fallen vacant. Short enough that a leader dying is noticed
# quickly, long enough to cost nothing.
LEADER_RETRY = 10.0

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


def _notify(relay: Relay, text: str, kind: str = "ack") -> None:
    try:
        relay.notify(kind=kind, text=text, request_id=new_request_id())
    except RelayError as exc:
        log(f"collect: notify failed: {exc}")


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
            continue                    # ambiguous; the chooser handles it
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
                except wake.WakeError as exc:
                    # Never strand the hook on a stream problem. Polling is
                    # more expensive, not broken, and the cooldown stops the
                    # next hook rediscovering the same blocked port.
                    _mark_stream_down(str(exc))
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
    while time.time() < deadline:
        got = _drain(cfg, relay, want)
        if got:
            return got
        if not waiter.armed(want):
            return None
        time.sleep(min(cfg.poll_fallback_seconds, max(0.0, deadline - time.time())))
    return None
