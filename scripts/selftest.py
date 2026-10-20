#!/usr/bin/env python3
"""Laptop-side self-test. Stdlib only, no network.

    python3 scripts/selftest.py

The relay has had tests since the start; this side has had none, which is why
the daemon's failures kept being found in production. Runs against a throwaway
CLAUDE_CLIQ_HOME so it cannot touch a real pairing.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["CLAUDE_CLIQ_HOME"] = tempfile.mkdtemp(prefix="cliq-selftest-")

import collect          # noqa: E402
import spool            # noqa: E402
import waiter           # noqa: E402
from cliqlib import Config  # noqa: E402


def row(id_, text, *, request_id="", age_s=0.0):
    """A relay row. `ts` is epoch MILLISECONDS, as the relay sends it."""
    return {"id": id_, "text": text, "request_id": request_id,
            "ts": (time.time() - age_s) * 1000.0}


class FakeRelay:
    def __init__(self, batches=None):
        self.batches = list(batches or [])
        self.sent = []

    def poll(self, since=0, wait=0, timeout=None):
        self.polls = getattr(self, "polls", 0) + 1
        # Mirrors the real signature, including that `timeout` is separate
        # from `wait` -- conflating them gave a non-blocking fetch a ten
        # second budget and released a session on a cold start.
        return {"messages": self.batches.pop(0) if self.batches else []}

    def notify(self, **kw):
        self.sent.append(kw)

    def stream_ticket(self):
        return {"url": "h", "key": "k", "wss_id": "w", "wake_id": "deadbeef"}


def handed_over(rid):
    """Has `rid` been answered but not yet collected by its own hook?

    resolve() writes the payload into the waiter file; the hook waiting on it
    is what deletes it. So "served" means the file exists AND carries a
    payload -- not that it is gone.
    """
    import json
    try:
        return "resolved" in json.loads((waiter._path(rid)).read_text())
    except Exception:
        return False


def clean():
    for p in Path(os.environ["CLAUDE_CLIQ_HOME"]).glob("**/*"):
        if p.is_file():
            p.unlink()


class Spool(unittest.TestCase):
    def setUp(self):
        clean()

    def test_survives_a_write_and_dedupes_by_id(self):
        spool.add([row("a", "one"), row("b", "two")])
        spool.add([row("a", "one again")])          # a retry, or a new leader
        self.assertEqual([i["id"] for i in spool.items()], ["a", "b"])

    def test_drop_removes_only_what_was_accepted(self):
        spool.add([row("a", "one"), row("b", "two")])
        spool.drop(["a"])
        self.assertEqual([i["id"] for i in spool.items()], ["b"])

    def test_prune_drops_old_replies_and_keeps_fresh_ones(self):
        spool.add([row("old", "typed hours ago", age_s=3 * 3600),
                   row("new", "just now", age_s=5)])
        stale = spool.prune(1800)
        self.assertEqual([s["id"] for s in stale], ["old"])
        self.assertEqual([i["id"] for i in spool.items()], ["new"])

    def test_prune_keeps_a_row_with_no_usable_timestamp(self):
        # Dropping a reply because its clock looked odd is worse than
        # delivering it late.
        spool.add([{"id": "x", "text": "hi", "request_id": "", "ts": 0}])
        self.assertEqual(spool.prune(1), [])
        self.assertEqual(len(spool.items()), 1)


class Leadership(unittest.TestCase):
    def setUp(self):
        clean()

    def test_only_one_holder_at_a_time(self):
        with collect._Leadership() as first:
            self.assertTrue(first)
            with collect._Leadership() as second:
                self.assertFalse(second, "two leaders means two sockets")
        # Released on exit, so the post is vacant again.
        with collect._Leadership() as third:
            self.assertTrue(third)


class Routing(unittest.TestCase):
    def setUp(self):
        clean()
        self.cfg = Config(stale_after_seconds=1800)

    def arm(self, rid, kind="finished"):
        waiter.arm(rid, session_id="s", kind=kind, cwd="/tmp",
                   expires_at=time.time() + 60)

    def test_a_bound_reply_reaches_its_own_waiter(self):
        self.arm("aaaaa")
        self.arm("bbbbb")
        relay = FakeRelay([[row("1", "the answer", request_id="bbbbb")]])
        got = collect._drain(self.cfg, relay, "bbbbb")
        self.assertEqual(got["text"], "the answer")
        self.assertFalse(waiter.armed("bbbbb"),
                         "the leader takes its own payload directly and clears "
                         "the file, or active() would keep reporting it")
        self.assertTrue(waiter.armed("aaaaa"), "the other is untouched")
        self.assertFalse(handed_over("aaaaa"), "and not answered by mistake")

    def test_a_stale_reply_is_refused_not_delivered(self):
        # The bug seen on the first end-to-end run: a reply from twenty
        # minutes earlier came back and would have answered a new question.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "yesterday's answer", age_s=3 * 3600)]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNone(got)
        self.assertTrue(waiter.armed("aaaaa"), "still waiting for a real answer")
        self.assertTrue(any("minutes ago" in (m.get("text") or "") for m in relay.sent),
                        "and the person is told why")

    def test_a_bound_reply_naming_nothing_is_dropped_not_rerouted(self):
        # Re-routing it would put one question's answer into another's waiter.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "late tap", request_id="zzzzz")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNone(got)
        self.assertTrue(waiter.armed("aaaaa"))
        self.assertEqual(spool.items(), [], "dropped, not left to retry forever")

    def test_a_reply_for_another_waiter_is_handed_over(self):
        # The leader fetches for the whole machine, so most of what it gets
        # belongs to someone else.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "for aaaaa", request_id="aaaaa")]])
        got = collect._drain(self.cfg, relay, "bbbbb")   # not ours
        self.assertIsNone(got, "we were not the target")
        self.assertTrue(handed_over("aaaaa"),
                        "their payload is waiting in their own file")

    def test_nothing_armed_leaves_the_reply_spooled(self):
        relay = FakeRelay([[row("1", "nobody home")]])
        self.assertIsNone(collect._drain(self.cfg, relay, "aaaaa"))
        self.assertEqual(len(spool.items()), 1, "kept for the next hook")

    def test_a_fetch_is_durable_before_it_is_routed(self):
        # /await deletes rows as it returns them, so a crash between the
        # response and resolve() must not lose the reply.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "precious", request_id="aaaaa")]])
        original = waiter.resolve
        waiter.resolve = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("died"))
        try:
            with self.assertRaises(RuntimeError):
                collect._drain(self.cfg, relay, "aaaaa")
        finally:
            waiter.resolve = original
        self.assertEqual([i["text"] for i in spool.items()], ["precious"],
                         "still recoverable by the next leader")


class Concurrency(unittest.TestCase):
    """Two sessions at once -- the case the single-collector daemon existed to
    prevent, and the one that broke first."""

    def setUp(self):
        clean()

    def arm(self, rid):
        waiter.arm(rid, session_id="s", kind="finished", cwd="/tmp",
                   expires_at=time.time() + 60)

    def test_a_follower_keeps_waiting_past_one_leadership_slice(self):
        # The reported bug: a second session said "waiting" and then "done"
        # about ten seconds later. wait() disarmed its own waiter on timeout
        # -- correct when its deadline WAS the window, wrong once it was
        # called in slices -- and wait_for read the missing file as "answered
        # elsewhere".
        self.arm("aaaaa")
        slice_end = time.time() + 0.3
        self.assertIsNone(waiter.wait("aaaaa", slice_end, tick=0.05))
        self.assertTrue(waiter.armed("aaaaa"),
                        "a slice timing out must not clear the waiter")

    def test_a_follower_gives_up_only_when_its_waiter_really_goes(self):
        # The other half: a waiter disarmed by on_settled.py (answered at the
        # terminal) still has to stop the follower promptly.
        self.arm("aaaaa")
        waiter.disarm("aaaaa")
        self.assertFalse(waiter.armed("aaaaa"))

    def test_a_resolved_waiter_is_cleared_by_the_hook_that_takes_it(self):
        self.arm("aaaaa")
        waiter.resolve("aaaaa", {"type": "answer", "text": "hi"})
        got = waiter.wait("aaaaa", time.time() + 2, tick=0.05)
        self.assertEqual(got["text"], "hi")
        self.assertFalse(waiter.armed("aaaaa"), "taken, so cleared")

    def test_only_one_of_two_hooks_leads_and_the_other_still_gets_served(self):
        # Leader serves a reply addressed to the follower's waiter; the
        # follower picks it up off its own file.
        self.arm("aaaaa")                      # the follower
        self.arm("bbbbb")                      # the leader
        cfg = Config(stale_after_seconds=1800)
        relay = FakeRelay([[row("1", "for the follower", request_id="aaaaa")]])
        with collect._Leadership() as leading:
            self.assertTrue(leading)
            collect._drain(cfg, relay, "bbbbb")
        got = waiter.wait("aaaaa", time.time() + 2, tick=0.05)
        self.assertEqual(got["text"], "for the follower")


class Chooser(unittest.TestCase):
    """A plain reply with several sessions waiting: ask, never guess."""

    def setUp(self):
        clean()
        self.cfg = Config(stale_after_seconds=1800)

    def arm(self, rid, cwd):
        waiter.arm(rid, session_id="s", kind="finished", cwd=cwd,
                   expires_at=time.time() + 60)

    def test_two_sessions_waiting_produces_a_question_not_a_guess(self):
        self.arm("aaaaa", "/x/proj-a")
        self.arm("bbbbb", "/x/proj-b")
        relay = FakeRelay([[row("1", "do the thing")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNone(got, "nothing is answered yet")
        self.assertTrue(waiter.armed("aaaaa") and waiter.armed("bbbbb"),
                        "neither session was guessed at")
        ask = [m for m in relay.sent if m.get("kind") == "choose"]
        self.assertEqual(len(ask), 1, "asked exactly once")
        labels = [a["label"] for a in ask[0]["answers"]]
        self.assertEqual(sorted(labels), ["proj-a (question)", "proj-b (question)"])
        # Each button names its own session: that is what makes a tap
        # unambiguous by construction rather than by inference.
        self.assertEqual(sorted(a["req"] for a in ask[0]["answers"]),
                         ["aaaaa", "bbbbb"])

    def test_it_asks_once_not_on_every_fetch(self):
        self.arm("aaaaa", "/x/a")
        self.arm("bbbbb", "/x/b")
        relay = FakeRelay([[row("1", "hello")], [], []])
        for _ in range(3):
            collect._drain(self.cfg, relay, "aaaaa")
        self.assertEqual(len([m for m in relay.sent if m.get("kind") == "choose"]), 1)

    def test_a_tap_delivers_the_held_text_to_the_session_named(self):
        self.arm("aaaaa", "/x/a")
        self.arm("bbbbb", "/x/b")
        relay = FakeRelay([[row("1", "run the tests")],
                           # /cliq-action maps a pick to an empty body bound
                           # to the chosen request id.
                           [row("2", "", request_id="bbbbb")]])
        collect._drain(self.cfg, relay, "aaaaa")          # asks
        got = collect._drain(self.cfg, relay, "aaaaa")    # the tap
        self.assertIsNone(got, "we were not the session picked")
        self.assertTrue(handed_over("bbbbb"), "the picked session got the text")
        import json as _j
        payload = _j.loads((waiter._path("bbbbb")).read_text())["resolved"]
        self.assertEqual(payload["text"], "run the tests", "and not retyped")
        self.assertTrue(waiter.armed("aaaaa"), "the other is untouched")
        self.assertEqual(spool.items(), [], "both the reply and the tap are done")

    def test_an_ambiguous_reply_never_drifts_onto_a_later_question(self):
        # The bug the needs_pick flag exists for. Two sessions waiting, reply
        # plainly, then one session goes away -- leaving exactly one waiter.
        # Without the flag the old text becomes the answer to whatever that
        # session asked, minutes later and about something else.
        self.arm("aaaaa", "/x/a")
        self.arm("bbbbb", "/x/b")
        relay = FakeRelay([[row("1", "about the FIRST thing")], [], []])
        collect._drain(self.cfg, relay, "aaaaa")
        waiter.disarm("bbbbb")                            # that session ends
        got = collect._drain(self.cfg, relay, "aaaaa")    # only aaaaa remains
        self.assertIsNone(got, "the stale reply must NOT answer this")
        self.assertTrue(waiter.armed("aaaaa"))
        self.assertEqual(len(spool.items()), 1, "still waiting to be named")

    def test_one_session_waiting_needs_no_chooser(self):
        self.arm("aaaaa", "/x/a")
        relay = FakeRelay([[row("1", "just do it")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertEqual(got["text"], "just do it")
        self.assertEqual([m for m in relay.sent if m.get("kind") == "choose"], [])


class Transport(unittest.TestCase):
    """The escape hatch has to be real, not just configurable."""

    def setUp(self):
        clean()

    def arm(self, rid):
        waiter.arm(rid, session_id="s", kind="finished", cwd="/tmp",
                   expires_at=time.time() + 60)

    def test_poll_mode_never_opens_the_stream(self):
        # The whole point of the hatch: a surprise bill or a blocked port must
        # be escapable without a working stream to escape with.
        cfg = Config(transport="poll", poll_fallback_seconds=0.01)
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "polled", request_id="aaaaa")]])
        relay.stream_ticket = lambda: self.fail("poll mode asked for a ticket")
        got = collect.wait_for(cfg, relay, "aaaaa", time.time() + 5)
        self.assertEqual(got["text"], "polled")

    def test_an_unreachable_stream_falls_back_instead_of_stranding(self):
        # A proxy blocking wss:// raises OSError from the socket, which used
        # to escape every handler and release the session to the terminal.
        cfg = Config(transport="stream", poll_fallback_seconds=0.01)
        self.arm("aaaaa")
        relay = FakeRelay([[], [row("1", "fell back", request_id="aaaaa")]])
        import wake as _wake
        original = _wake.listen
        _wake.listen = lambda *a, **k: (_ for _ in ()).throw(
            _wake.WakeError("cannot reach host:443 (blocked)"))
        try:
            got = collect.wait_for(cfg, relay, "aaaaa", time.time() + 5)
        finally:
            _wake.listen = original
        self.assertEqual(got["text"], "fell back")

    def test_the_fallback_backs_off_and_stops_rather_than_draining_reads(self):
        # A flat 20s poll over a 30-minute window is 90 fetches -- 180 reads --
        # which spends the whole monthly allowance at five questions a day.
        # That is how this design's predecessor died, so the fallback is
        # capped outright.
        cfg = Config(transport="poll", poll_fallback_seconds=0.01,
                     poll_fallback_max_seconds=0.02, poll_fallback_budget=3)
        self.arm("aaaaa")
        relay = FakeRelay([])                   # never answers
        got = collect.wait_for(cfg, relay, "aaaaa", time.time() + 5)
        self.assertIsNone(got)
        self.assertLessEqual(relay.polls, cfg.poll_fallback_budget,
                             "stopped at the budget instead of filling the window")
        self.assertTrue(any("budget" in (m.get("text") or "") for m in relay.sent),
                        "and said so, rather than going quiet")

    def test_a_failed_stream_is_not_retried_immediately(self):
        # Otherwise every blocked hook pays the connect timeout over again.
        cfg = Config(transport="stream", stream_retry_after_seconds=600)
        self.assertTrue(collect._stream_available(cfg))
        collect._mark_stream_down("blocked")
        self.assertFalse(collect._stream_available(cfg))
        # ...but the cooldown expires rather than latching forever.
        self.assertTrue(collect._stream_available(
            Config(stream_retry_after_seconds=0)))

    def test_connecting_clears_the_cooldown(self):
        collect._mark_stream_down("blocked")
        collect._mark_stream_up()
        self.assertTrue(collect._stream_available(Config()))

    def test_an_unknown_transport_falls_back_to_polling(self):
        import os as _os
        from cliqlib import load_config
        _os.environ["CLAUDE_CLIQ_TRANSPORT"] = "carrier-pigeon"
        try:
            self.assertEqual(load_config().transport, "poll")
        finally:
            del _os.environ["CLAUDE_CLIQ_TRANSPORT"]

    def test_the_env_var_overrides_the_config_file(self):
        # Flipping the hatch must not itself require a working stream, so it
        # has to be settable without editing a file the laptop may not reach.
        import os as _os
        from cliqlib import load_config
        _os.environ["CLAUDE_CLIQ_TRANSPORT"] = "poll"
        try:
            self.assertEqual(load_config().transport, "poll")
        finally:
            del _os.environ["CLAUDE_CLIQ_TRANSPORT"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
