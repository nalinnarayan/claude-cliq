#!/usr/bin/env python3
"""Stop hook -- "it's done" and "it asked you something" are the same event.

Armed, a reply comes back as {"decision":"block"}, which puts Claude back to
work; it will stop again later and this hook fires again. That is the whole
loop -- nothing external ever needs to re-trigger a hook.
"""

from __future__ import annotations

import sys

import hidden
import hookutil as H
import waiter
from cliqlib import (block_streak, is_away, log, new_request_id, notifies,
                     notify_only_hint)


def main() -> int:
    payload = H.read_payload()
    # True on the Stop immediately after we pushed Claude back to work with no
    # tool call in between. It must stop us BLOCKING again -- otherwise the
    # session spins -- but it must not stop us reporting the result, or every
    # answer to a follow-up asked from Cliq vanishes silently.
    resumed = bool(payload.get("stop_hook_active"))

    # Off means OFF. The hook still fires -- hooks.json is static in the
    # plugin and cannot be unregistered at runtime -- but it does nothing:
    # no relay call, no Cliq message, no block.
    if not notifies():
        return 0

    cfg, relay = H.ready()
    if not cfg:
        return 0

    session_id = payload.get("session_id", "")

    # Reaching here proves no question is pending in this session: a turn
    # cannot end while one is open. So anything still armed from an
    # AskUserQuestion or a plan was abandoned -- rejected at the terminal with
    # a typed reply, which leaves the hook RUNNING, so neither the dead-owner
    # sweep nor a tool_use_id can clear it. Left alone they sat for their full
    # window and offered the chooser dead choices: taps that reach a hook whose
    # tool call was already resolved.
    stale = waiter.disarm_for_session(session_id)
    if stale:
        log(f"stop: cleared {stale} abandoned question waiter(s) from this session")
    cwd = payload.get("cwd", "")
    body = H.summarize(payload.get("last_assistant_message", ""))
    if not body:
        # A turn can end with nothing to show -- a slash command, or work that
        # was all tool calls. Cliq rejects an empty message, which is why this
        # used to just return.
        #
        # But returning breaks what away mode promises. The session stops, no
        # ping is sent, nothing is logged, and the terminal reads "done" the
        # instant it says it is waiting -- while the one moment you most want
        # to hand it an instruction from your phone is right after turning
        # away mode on, which is itself a turn with no message.
        body = "_(the turn ended with nothing to show)_"

    # A tool call since the last block resets Claude Code's counter, so only
    # count the runs where it did not.
    streak = block_streak(session_id, bump=resumed) if session_id else 0
    # Blocking repeatedly is safe here because every block requires a human
    # reply -- this cannot spin on its own. The ceiling exists so the
    # back-and-forth ends with an explanation rather than Claude Code quietly
    # refusing to honour another block.
    at_ceiling = streak >= cfg.max_block_streak
    armed = is_away() and not at_ceiling
    rid = new_request_id()
    ctx = H.context_line(cwd)
    lead = "💬 Answered" if resumed else "✅ Claude finished"
    text = f"{lead} — {ctx}\n\n{body}" if ctx else f"{lead}\n\n{body}"

    if armed:
        # The id is CARRIED, not printed. A swipe-reply still binds, because
        # the relay reads the same zero-width encoding out of
        # replied_message.text -- which hands back the raw source. Printing
        # "(id atyxx)" put bookkeeping in front of the reader for no benefit
        # to them.
        hint = "reply to keep it going" + hidden.encode(rid)
    elif at_ceiling:
        # Say what the limit is and what to do, rather than just going quiet.
        hint = (f"that's {cfg.max_block_streak} replies in a row without me "
                "touching any files — the limit for back-and-forth here. "
                "Carry on at the terminal; working sessions aren't capped.")
    else:
        # notify-only. Say so on every message rather than waiting for someone
        # to reply into the void: a reply here reaches nothing, and the person
        # has no way of knowing that from a message that looks identical to
        # one they could answer.
        hint = notify_only_hint()

    H.send(relay, kind="finished", text=text, request_id=rid,
           session_id=session_id, cwd=cwd, expects_reply=armed, hint=hint)

    if not armed:
        return 0

    reply = H.block_for_reply(cfg, rid, session_id=session_id, kind="finished",
                              cwd=cwd, topic=body)
    if not reply:
        # Window lapsed. The session is released and cannot be fed from
        # outside again; a later reply is answered by the daemon with where
        # to pick the work up.
        return 0

    answer = (reply.get("text") or "").strip()
    if not answer:
        return 0
    H.emit({"decision": "block",
            "reason": f'The user replied via Zoho Cliq: "{answer}"'})
    return 0


if __name__ == "__main__":
    sys.exit(main())
