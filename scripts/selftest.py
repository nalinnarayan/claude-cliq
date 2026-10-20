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
