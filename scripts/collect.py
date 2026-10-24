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

import attach
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

# When a reply is being held back for the rest of a multi-attachment send.
# Read by the leader so it can stop listening in time to deliver it.
_hold_until = 0.0

# Returned from on_wake to break out of a listen whose deadline is now too
# far away. A hold is decided INSIDE the wake handler, by which point listen()
# is already waiting on the full window -- so without this the leader comes
# back whenever the next unrelated event happens to arrive, which was measured
# at 28 seconds for a 12-second hold.
_HOLD = object()

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

# Several Cliq messages that are really one reply.
#
# Sending three images at once arrives as three separate messages, each with
# its own file. A waiting session consumes the first and the rest sit in the
# spool until they go stale -- so two of three pictures silently never arrive,
# and a later session risks being handed one as a stray.
#
# Replies this close together, to the same session, are one thing the person
# said. Deliberately short: gluing a reply from ten minutes ago onto a fresh
# one would be a different and worse bug.
COALESCE_WINDOW = 90.0


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
            answers: list[dict] | None = None, hint: str = "") -> bool:
    """Returns whether it actually went out.

    The return value matters: a caller that records "I asked" after a send
    that was REJECTED never asks again. That is exactly how a chooser whose
    button labels were two characters too long became permanent silence.
    """
    try:
        relay.notify(kind=kind, text=text, request_id=new_request_id(),
                     answers=answers, hint=hint)
        return True
    except RelayError as exc:
        log(f"collect: notify failed: {exc}")
        return False


def _plain(text: str | None) -> str:
    """Flatten a preview to one line of unformatted text.

    A topic is whatever Claude last said, markdown and all. Dropped into an
    italic preview it nests emphasis inside emphasis, which renders as
    stray asterisks -- so the markers come out and the lines come together.
    """
    flat = " ".join((text or "").split())
    for mark in ("***", "**", "*", "__", "_", "`"):
        flat = flat.replace(mark, "")
    return flat


def _ask_which(relay: Relay, item: dict, armed: list[dict]) -> None:
    """A plain reply with several sessions waiting: ask, never guess.

    `armed` must be only the sessions the held text could legitimately answer
    -- which excludes permissions, since those are decided with the Allow/Deny
    buttons on their own message and never by typed text. Offering one here
    would invite a tap that cannot mean what it looks like.

    One button per waiting session, each carrying THAT session's request id --
    which is what makes a tap unambiguous by construction rather than by
    inference. The typed text stays in the spool, so answering costs one tap
    and nothing is retyped.

    The leader that asks may well be gone by the time the tap arrives. That is
    fine: a tap is just another queued reply, and it names its target, so
    whichever hook collects it next can apply it.
    """
    # TWENTY characters, measured: Cliq rejects the whole message with
    # input_maxlength_reached on the key 'label'. "claude-cliq (question)" is
    # 22 and silently cost a chooser. The folder alone is the useful part, so
    # what kind of thing each session wants goes in the text instead.
    answers, lines, seen = [], [], set()
    for w in armed[:5]:
        # The session's own name first -- the one Claude Code's /resume picker
        # shows. The folder is the fallback, and for a session started in the
        # home directory it is useless ("nalin-20844"), which is what made the
        # buttons unrecognisable.
        where = (w.get("cwd") or "").rstrip("/").split("/")[-1] or "session"
        # Captured when the waiter was armed, but looked up again if it is
        # missing. Two reasons that happens: a session blocked since before
        # this feature existed, and -- the lasting one -- a BRAND-NEW session,
        # which has no ai-title until Claude Code gets round to writing one.
        # A waiter armed in that moment would otherwise show a folder name
        # for as long as it waits.
        title = " ".join((w.get("title") or "").split())
        if not title:
            title = " ".join(waiter.session_title(
                w.get("session_id", ""), w.get("cwd", "")).split())
        base = title or where
        label = base[:20]
        while label in seen:                 # two sessions, one name
            label = f"{base[:18]}·{len(seen)}"
        seen.add(label)
        answers.append({"label": label, "value": "pick", "req": w["request_id"]})
        # Twenty characters truncates most titles, so the full one goes here
        # along with what that session is actually waiting about.
        #
        # Indented with EM SPACES, not ordinary ones: Cliq collapses leading
        # whitespace in a continuation line, so spaces or a tab simply vanish
        # and the question runs flush against the name above it.
        topic = _plain(w.get("topic"))[:110]
        head = f"- *{title or where}*" + (f" ({where})" if title else "")
        lines.append(head + (f"\n\u2003\u2003_{topic}_" if topic else ""))
    more = "" if len(armed) <= 5 else f"\n\n({len(armed) - 5} more not shown)"
    body = "\n".join(lines)
    sent = _notify(relay,
                   f"Which one is this for?\n\n{body}{more}\n\n"
                   f"_{_plain(item['text'])[:160]}_",
                   kind="choose", answers=answers,
                   hint="tap one — I've kept what you typed")
    if not sent:
        # Do NOT record that we asked. An unsent question must be asked again
        # on the next fetch, not remembered as asked and never repeated.
        log("collect: could not send the chooser — will ask again")
        return
    spool.mark(item["id"], needs_pick=True, asked=True)
    log(f"collect: asked which of {[w['request_id'] for w in armed]} "
        f"a plain reply belongs to")


def _has_attachment(text: str) -> bool:
    """Did this reply bring a file? That is what makes more likely to follow."""
    return "[image attached:" in text or "[file attached:" in text \
        or "[document attached:" in text or "[attached:" in text


def _coalesce(first: dict, items: list[dict], armed: list[dict],
              taken: list[str]) -> list[dict]:
    """The other replies that belong with `first`, in arrival order.

    Only when exactly ONE session is waiting: with several, which reply
    belongs to which is precisely the question the chooser exists to ask, and
    joining them would answer it by guessing.

    Only unbound replies, too. One that names a request belongs to that
    request, and a file or a form answer is not part of someone's next
    sentence.
    """
    if len(armed) != 1:
        return []
    # `ts` -- when the PERSON sent it -- not `spooled_at`, which is when we
    # fetched it. Everything in one fetch shares a spool time, so keying on it
    # made the window admit a reply from ten minutes earlier and left the
    # order arbitrary. The clock that matters here is theirs.
    anchor_at = float(first.get("ts") or 0)
    out = []
    for other in items:
        if other["id"] == first["id"] or other["id"] in taken:
            continue
        if other.get("request_id") or other.get("needs_pick"):
            continue
        if abs(float(other.get("ts") or 0) - anchor_at) > COALESCE_WINDOW:
            continue
        out.append(other)
    return out


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
    held_logged = False          # one line per pass, not one per attachment
    for item in spool.items():
        armed = waiter.active()
        if not armed:
            break                       # nothing to give it to; leave it spooled
        text, bound_id = item["text"], item["request_id"]

        # A chooser tap: it names a session and carries no text of its own,
        # because the text it is about is the one we held when we asked. A
        # form submission also carries a bound id, but brings its own JSON, so
        # the emptiness test keeps the two apart.
        if bound_id and not text:
            held = spool.oldest_needing_pick()
            picked = next((w for w in armed if w["request_id"] == bound_id), None)
            if held and picked:
                rid, payload = waiter.route(held["text"], [picked], bound=True)
                if rid and waiter.resolve(rid, payload):
                    log(f"collect: chooser sent the held reply to {rid}")
                    # Dropped NOW, not with the rest at the end of the loop.
                    # oldest_needing_pick() re-reads from disk, so a deferred
                    # drop let a second tap find the same held reply and
                    # deliver it to a second session -- one answer landing in
                    # two places, which is the one thing this must never do.
                    spool.drop([item["id"], held["id"]])
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
            # Point at the live one when there is one. Re-asking a question
            # leaves two messages that look identical, both offering a button,
            # and tapping the older is the obvious mistake -- made, and
            # reported as "why did I receive this?". Saying only "nothing's
            # waiting" leaves someone to work out which message to use.
            # Unconditionally: the loop breaks above when nothing is armed,
            # so reaching here means some OTHER session is still waiting.
            _notify(relay, "Nothing's waiting on that one any more — it was "
                           "answered, cancelled, or its window closed. "
                           "There's a newer message still waiting — answer "
                           "that one.")
            continue

        if target:
            rid, payload = waiter.route(text, [target], bound=True)
        else:
            # One session waiting, several unbound replies that arrived
            # together: that is one message split by Cliq, not several
            # answers, so hand it over whole.
            siblings = _coalesce(item, spool.items(), armed, taken)
            if _has_attachment(text) and len(armed) == 1:
                # Wait for QUIET, not for a fixed window. Pictures sent in one
                # go arrive one at a time -- two were measured 23 seconds
                # apart, and the third of three a further 18 -- so a window
                # from the first released as soon as one sibling appeared and
                # split three into two and then one. Measuring from the most
                # recent arrival keeps extending while uploads continue.
                group = [item] + siblings
                newest = max(float(g.get("spooled_at") or 0) for g in group)
                first = min(float(g.get("spooled_at") or 0) for g in group)
                now = time.time()
                quiet_for = now - newest
                global _hold_until
                if (quiet_for < cfg.attachment_quiet_seconds
                        and now - first < cfg.attachment_grace_seconds):
                    _hold_until = min(newest + cfg.attachment_quiet_seconds,
                                      first + cfg.attachment_grace_seconds)
                    if not held_logged:
                        log(f"collect: holding {len(group)} attachment(s) for "
                            f"{_hold_until - now:.0f}s more in case another "
                            f"follows")
                        held_logged = True
                    continue
            if siblings:
                # Sorted as a whole, anchor included: the spool is iterated in
                # fetch order, which is not the order they were sent.
                group = sorted([item] + siblings, key=lambda i: float(i.get("ts") or 0))
                text = "\n\n".join(g["text"] for g in group)
                log(f"collect: joined {len(group)} replies that arrived together")
            rid, payload = waiter.route(text, armed)
            if rid:
                taken.extend(s["id"] for s in siblings)
                _hold_until = 0.0

        if not rid or not waiter.resolve(rid, payload):
            # Nothing matched. With one waiter that cannot happen, so this is
            # the several-sessions case: ask rather than guess, and mark the
            # reply so it can never drift onto a later question.
            # The same set route() considered: a typed reply cannot answer a
            # permission, so a permission must not appear as an option.
            candidates = [w for w in armed if w.get("kind") != "permission"]
            if not bound_id and len(candidates) > 1 and not item.get("asked"):
                _ask_which(relay, item, candidates)
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
        # An attachment is downloaded BEFORE the row is spooled, so the
        # durable record already names a real file. Failure only rewrites the
        # marker into an explanation -- the reply itself is never lost.
        attach.fetch_into(rows, relay)
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

    # The deadline the running listen was given, so on_wake can tell whether
    # a newly set hold is actually sooner.
    listening_until = [deadline]

    def on_wake(body):
        if not isinstance(body, dict) or body.get("w") != wake_id:
            return None                 # another account's bell
        got = _drain(cfg, relay, want)
        if got is not None:
            return got
        if _hold_until and _hold_until < listening_until[0]:
            # Something is being held for the rest of a multi-attachment send
            # and this socket would otherwise wait out the whole window.
            return _HOLD
        return None

    def renew(exc):
        """A fresh ticket, but only when the old one is genuinely spent.

        The note that used to sit here said never to re-mint, because a
        connection_name whose session is alive is refused with "Duplicate
        value for connection_name". That stopped being true when tickets
        started minting a RANDOM name each -- and keeping the rule cost the
        doorbell: after a server close the session is gone, the old ticket is
        rejected ("Data Streams rejected the ticket"), and the machine fell
        back to polling having retried with a credential that could not work.

        Still nothing for SESSION_EXPIRED, which fires on almost every first
        connect and is fixed by retrying with the ticket already in hand.
        """
        # Causes that genuinely spend the session. Deliberately NOT every
        # retryable error: a ticket costs a Data Store read, so retrying is
        # cheap but re-ticketing is not.
        reason = str(exc)
        spent = ("closed by the server" in reason
                 or "closed the connection" in reason
                 or "went quiet" in reason)
        if not spent:
            return None
        log("collect: the session is gone — minting a fresh stream ticket")
        return relay.stream_ticket()

    creds = {"url": ticket["url"], "key": ticket["key"], "wss_id": ticket["wss_id"]}
    while True:
        # Stop early when a reply is being held for the rest of a
        # multi-attachment send, so the hold can be handed over on time rather
        # than sitting until the whole window expires.
        until = min(deadline, _hold_until) if _hold_until else deadline
        listening_until[0] = until
        got = wake.listen(creds, deadline=until, on_wake=on_wake, log=log,
                          renew=renew)
        if got is not None and got is not _HOLD:
            return got
        if time.time() >= deadline:
            return None
        if got is _HOLD:
            continue              # re-listen, now with the hold's deadline
        if not _hold_until:
            return None
        # The quiet period is up. Route what is spooled WITHOUT fetching -- a
        # read here would buy nothing, since the doorbell never rang.
        got = _route_spooled(cfg, relay, want)
        if got is not None:
            return got
        # Still holding, so listen again -- but never tighter than the hold
        # itself. A real listen blocks until its deadline; one that returns
        # straight away (a deadline already passed, say) would otherwise spin
        # this loop at full speed.
        time.sleep(max(0.0, min(_hold_until, deadline) - time.time()))


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
