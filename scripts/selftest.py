#!/usr/bin/env python3
"""Laptop-side self-test. Stdlib only, no network.

    python3 scripts/selftest.py

The relay has had tests since the start; this side has had none, which is why
the daemon's failures kept being found in production. Runs against a throwaway
CLAUDE_CLIQ_HOME so it cannot touch a real pairing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["CLAUDE_CLIQ_HOME"] = tempfile.mkdtemp(prefix="cliq-selftest-")

import attach           # noqa: E402
import plan             # noqa: E402
import collect          # noqa: E402
import wake             # noqa: E402
import spool            # noqa: E402
import waiter           # noqa: E402
from cliqlib import Config, RelayError, notify_only_hint  # noqa: E402


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
        if kw.get("kind") in getattr(self, "reject_kinds", ()):
            from cliqlib import RelayError
            raise RelayError('cliq 400: {"code":"input_maxlength_reached"}')
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


@contextlib.contextmanager
def mock_download(payload: bytes):
    """Serve `payload` to attach's urlopen without touching the network."""
    class Resp:
        def __init__(self):
            self._buf = io.BytesIO(payload)

        def read(self, n=-1):
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    real = attach.urllib.request.urlopen
    attach.urllib.request.urlopen = lambda *a, **k: Resp()
    try:
        yield
    finally:
        attach.urllib.request.urlopen = real


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
        # Module state, so a test that holds a reply must not change what the
        # next test sees.
        collect._hold_until = 0.0
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

    def test_images_sent_together_arrive_as_one_reply(self):
        # Reported live: three images at once arrive as three separate Cliq
        # messages, each with its own file. The session consumed the first and
        # the other two sat in the spool -- so pictures silently never arrived,
        # and a later session risked being handed one as a stray.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "look at these"),
                            row("2", "[image attached: /tmp/a.png]"),
                            row("3", "[image attached: /tmp/b.png]")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNotNone(got, "the session is answered")
        for fragment in ("look at these", "/tmp/a.png", "/tmp/b.png"):
            self.assertIn(fragment, got["text"], f"{fragment} reached the session")
        self.assertEqual(spool.items(), [], "and nothing is left stranded")

    def test_an_image_is_held_briefly_for_the_ones_still_uploading(self):
        # "This was also a multi-image case. I sent those two together." They
        # did not ARRIVE together: a phone uploads one at a time, and two sent
        # in one go were measured 23 seconds apart. Answering the first
        # immediately is what made a pair look like a single image.
        self.arm("aaaaa")
        cfg = Config(stale_after_seconds=1800, attachment_grace_seconds=30.0)
        relay = FakeRelay([[row("1", "[image attached: /tmp/a.png]", age_s=1)]])
        got = collect._drain(cfg, relay, "aaaaa")
        self.assertIsNone(got, "not handed over while more may be coming")
        self.assertEqual(len(spool.items()), 1, "still held, not dropped")
        self.assertGreater(collect._hold_until, time.time(),
                           "and the leader knows when to come back")

    def test_the_held_image_goes_out_once_the_grace_is_up(self):
        self.arm("aaaaa")
        cfg = Config(stale_after_seconds=1800, attachment_grace_seconds=30.0)
        # Spooled a minute ago, so the grace has already passed.
        relay = FakeRelay()
        spool.add([row("1", "[image attached: /tmp/a.png]", age_s=60)])
        path = Path(os.environ["CLAUDE_CLIQ_HOME"]) / "spool.json"
        items = json.loads(path.read_text())
        items[0]["spooled_at"] = time.time() - 60
        path.write_text(json.dumps(items))
        got = collect._route_spooled(cfg, relay, "aaaaa")
        self.assertIsNotNone(got, "waited long enough; send it")
        self.assertIn("/tmp/a.png", got["text"])

    def test_a_third_picture_still_uploading_is_waited_for(self):
        # The 2+1 split, measured: two images arrived a second apart and went
        # out together, then the third landed 18 seconds later and arrived on
        # its own. A fixed window from the FIRST released as soon as one
        # sibling appeared; the wait has to measure from the LAST arrival.
        self.arm("aaaaa")
        cfg = Config(stale_after_seconds=1800, attachment_quiet_seconds=12.0,
                     attachment_grace_seconds=120.0)
        path = Path(os.environ["CLAUDE_CLIQ_HOME"]) / "spool.json"

        def age_spool(seconds):
            items = json.loads(path.read_text())
            for i in items:
                i["spooled_at"] = time.time() - seconds
            path.write_text(json.dumps(items))

        spool.add([row("1", "[image attached: /tmp/a.png]", age_s=2),
                   row("2", "[image attached: /tmp/b.png]", age_s=1)])
        relay = FakeRelay()
        self.assertIsNone(collect._route_spooled(cfg, relay, "aaaaa"),
                          "two held, not sent -- a third may be uploading")

        # The third lands while they are still held.
        spool.add([row("3", "[image attached: /tmp/c.png]", age_s=0)])
        self.assertIsNone(collect._route_spooled(cfg, relay, "aaaaa"),
                          "still held; uploads are evidently continuing")

        # Uploads stop and the quiet period passes.
        age_spool(20)
        got = collect._route_spooled(cfg, relay, "aaaaa")
        self.assertIsNotNone(got, "quiet for long enough; send it")
        for name in ("/tmp/a.png", "/tmp/b.png", "/tmp/c.png"):
            self.assertIn(name, got["text"], f"{name} travelled with the rest")
        self.assertEqual(spool.items(), [], "and nothing trails behind")

    def test_the_ceiling_stops_an_endless_trickle(self):
        # A picture every ten seconds forever must not hold the session for
        # the whole window.
        self.arm("aaaaa")
        cfg = Config(stale_after_seconds=1800, attachment_quiet_seconds=12.0,
                     attachment_grace_seconds=30.0)
        spool.add([row("1", "[image attached: /tmp/a.png]", age_s=0)])
        path = Path(os.environ["CLAUDE_CLIQ_HOME"]) / "spool.json"
        items = json.loads(path.read_text())
        items[0]["spooled_at"] = time.time() - 40        # past the ceiling
        path.write_text(json.dumps(items))
        self.assertIsNotNone(collect._route_spooled(cfg, FakeRelay(), "aaaaa"),
                             "held long enough; stop waiting")

    def test_a_typed_reply_is_never_held(self):
        # The grace must cost nothing for the ordinary case.
        self.arm("aaaaa")
        cfg = Config(stale_after_seconds=1800, attachment_grace_seconds=30.0)
        relay = FakeRelay([[row("1", "just words", age_s=1)]])
        got = collect._drain(cfg, relay, "aaaaa")
        self.assertIsNotNone(got, "a typed reply goes straight through")
        self.assertEqual(got["text"], "just words")

    def test_nothing_is_held_when_two_sessions_are_waiting(self):
        # Holding would delay a reply the chooser has to ask about anyway.
        self.arm("aaaaa")
        self.arm("bbbbb")
        cfg = Config(stale_after_seconds=1800, attachment_grace_seconds=30.0)
        relay = FakeRelay([[row("1", "[image attached: /tmp/a.png]", age_s=1)]])
        collect._drain(cfg, relay, "aaaaa")
        self.assertEqual(collect._hold_until, 0.0, "no hold with several waiting")

    def test_replies_are_joined_in_the_order_they_arrived(self):
        self.arm("aaaaa")
        relay = FakeRelay([[row("2", "second", age_s=10),
                            row("1", "first", age_s=20)]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertEqual(got["text"], "first\n\nsecond")

    def test_two_sessions_waiting_are_never_coalesced(self):
        # With several waiting, which reply belongs to which is exactly the
        # question the chooser exists to ask. Joining them would answer it by
        # guessing -- the one thing this must never do.
        self.arm("aaaaa")
        self.arm("bbbbb")
        relay = FakeRelay([[row("1", "one"), row("2", "two")]])
        collect._drain(self.cfg, relay, "aaaaa")
        self.assertFalse(handed_over("bbbbb"), "nothing was guessed at")
        joined = [i for i in spool.items() if "one" in i["text"] and "two" in i["text"]]
        self.assertEqual(joined, [], "the two were not glued together")

    def test_a_reply_from_long_ago_is_not_glued_onto_a_fresh_one(self):
        # The window is short on purpose: these are meant to be one message
        # split by Cliq, not a conversation stitched together.
        self.arm("aaaaa")
        spool.add([{"id": "old", "text": "from earlier", "request_id": "",
                    "ts": (time.time() - 600) * 1000.0}])
        for item in spool.items():
            self.assertEqual(item["id"], "old")
        relay = FakeRelay([[row("new", "just now", age_s=1)]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNotNone(got)
        # Which of the two is delivered first is the spool's existing
        # oldest-first behaviour and not what this is about. What matters is
        # that they are not JOINED: ten minutes apart is two things said, and
        # the one left over waits its turn rather than being absorbed.
        self.assertNotIn("\n\n", got["text"], "the two were not glued together")
        self.assertEqual(len(spool.items()), 1, "the other is still queued, not lost")

    def test_a_bound_reply_is_not_swept_into_someone_elses_answer(self):
        # A form answer or a chooser tap names its request; it is not part of
        # the next sentence somebody types.
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "plain words"),
                            row("2", '{"0":["picked"]}', request_id="bbbbb")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertNotIn("picked", got["text"], "the bound reply stayed out of it")

    def test_tapping_a_dead_question_points_at_the_live_one(self):
        # Reported from a phone: "Why did I receive this nothing is waiting
        # message?" -- a question had been asked, interrupted at the terminal,
        # then asked again, leaving two identical-looking messages each with
        # an Answer button. Tapping the older one is the obvious mistake, and
        # "nothing's waiting" alone leaves someone to work out which message
        # to use.
        self.arm("bbbbb")                                   # the live question
        relay = FakeRelay([[row("1", '{"0":["Image forwarding"]}',
                                 request_id="aaaaa")]])      # the dead one
        got = collect._drain(self.cfg, relay, "bbbbb")
        self.assertIsNone(got, "a dead binding must not answer the live one")
        self.assertTrue(waiter.armed("bbbbb"), "which is still waiting")
        said = " ".join(m["text"] for m in relay.sent)
        self.assertIn("Nothing's waiting on that one", said)
        self.assertIn("newer message", said, "and says where to go instead")

    def test_with_nothing_armed_a_reply_is_held_not_dropped(self):
        # Why the message above can always say "there's a newer one": the
        # routing loop breaks when no session is waiting, so a reply arriving
        # then stays spooled for the next hook rather than being answered or
        # refused. Reaching the drop branch therefore proves something else is
        # armed.
        relay = FakeRelay([[row("1", '{"0":["something"]}', request_id="aaaaa")]])
        got = collect._drain(self.cfg, relay, "aaaaa")
        self.assertIsNone(got)
        self.assertEqual([i["id"] for i in spool.items()], ["1"], "still spooled")
        self.assertEqual(relay.sent, [], "and nothing is said about it yet")

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
        self.assertEqual(sorted(labels), ["proj-a", "proj-b"])
        # TWENTY characters, measured: Cliq rejects the whole message with
        # input_maxlength_reached beyond that, and "claude-cliq (question)"
        # is 22 -- which cost a chooser in production.
        for a in ask[0]["answers"]:
            self.assertLessEqual(len(a["label"]), 20, a["label"])
        # No "— question" on every line: it distinguishes nothing, because a
        # permission can never be one of these options.
        self.assertNotIn("question", ask[0]["text"])
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

    def test_one_reply_can_only_ever_reach_one_session(self):
        # Observed in production: two taps arrived in the same fetch and the
        # SAME held reply was delivered to both sessions. spool.drop() ran
        # after the loop while oldest_needing_pick() re-read from disk, so the
        # second tap found the reply still there. One answer, two places --
        # the one thing this must never do.
        self.arm("aaaaa", "/x/a")
        self.arm("bbbbb", "/x/b")
        relay = FakeRelay([[row("1", "only for one of you")],
                           [row("2", "", request_id="aaaaa"),
                            row("3", "", request_id="bbbbb")]])
        collect._drain(self.cfg, relay, "zzzzz")      # asks
        collect._drain(self.cfg, relay, "zzzzz")      # both taps at once
        served = [r for r in ("aaaaa", "bbbbb") if handed_over(r)]
        self.assertEqual(len(served), 1, f"delivered to {served}")

    def test_buttons_prefer_the_sessions_own_name_over_its_folder(self):
        # Claude Code writes an ai-title into each transcript -- the name its
        # own /resume picker shows. It is the only human-readable name a
        # session has, and for one started in the home directory the folder
        # basename is "nalin-20844", which identifies nothing.
        for rid, title in (("aaaaa", "replace-polling-with-data-streams"),
                           ("bbbbb", "assured video recut")):
            waiter.arm(rid, session_id="s", kind="finished",
                       cwd="/Users/nalin-20844", expires_at=time.time() + 60)
            path = waiter._path(rid)
            import json as _j
            d = _j.loads(path.read_text()); d["title"] = title
            path.write_text(_j.dumps(d))
        relay = FakeRelay([[row("1", "yes")]])
        collect._drain(self.cfg, relay, "zzzzz")
        labels = sorted(a["label"] for a in relay.sent[0]["answers"])
        self.assertEqual(labels, ["assured video recut", "replace-polling-with"])
        for a in relay.sent[0]["answers"]:
            self.assertLessEqual(len(a["label"]), 20)
        # Truncated on the button, whole in the text.
        self.assertIn("replace-polling-with-data-streams", relay.sent[0]["text"])

    def test_a_title_that_appears_after_arming_is_still_found(self):
        # A brand-new session has no ai-title until Claude Code writes one, so
        # a waiter armed in that moment captures nothing. Without a second
        # look it would show a folder name for as long as it waits -- which is
        # exactly what happened to a session armed before this feature landed.
        import waiter as _w
        self.arm("aaaaa", "/x/proj-a")          # no title captured
        self.arm("bbbbb", "/x/proj-b")
        original = _w.session_title
        _w.session_title = lambda sid, cwd: {"/x/proj-a": "late title here"}.get(cwd, "")
        try:
            relay = FakeRelay([[row("1", "yes")]])
            collect._drain(self.cfg, relay, "zzzzz")
        finally:
            _w.session_title = original
        labels = sorted(a["label"] for a in relay.sent[0]["answers"])
        self.assertEqual(labels, ["late title here", "proj-b"],
                         "found on the second look, folder as fallback")

    def test_the_chooser_names_what_each_session_is_waiting_about(self):
        # A hook is given a session_id (a UUID), a cwd and the last message --
        # never a session name, because Claude Code does not expose one. The
        # folder is all a 20-char label can carry, so the question has to show
        # up in the text or the buttons are a guess.
        waiter.arm("aaaaa", session_id="s", kind="finished", cwd="/x/proj-a",
                   expires_at=time.time() + 60, topic="Shall I cut the video at 4K?")
        waiter.arm("bbbbb", session_id="s", kind="finished", cwd="/x/proj-b",
                   expires_at=time.time() + 60, topic="Ready to deploy the relay?")
        relay = FakeRelay([[row("1", "yes")]])
        collect._drain(self.cfg, relay, "zzzzz")
        text = relay.sent[0]["text"]
        self.assertIn("cut the video at 4K", text)
        self.assertIn("deploy the relay", text)

    def test_a_permission_is_never_offered_as_an_option(self):
        # A typed reply cannot approve anything -- route() filters permissions
        # out before deciding -- so offering one as a button would invite a tap
        # that cannot mean what it appears to. With one question and one
        # permission there is no ambiguity at all, and the text just routes.
        self.arm("ccccc", "/x/a")
        waiter.arm("ddddd", session_id="s", kind="permission", cwd="/x/b",
                   expires_at=time.time() + 60)
        relay = FakeRelay([[row("1", "go ahead")]])
        got = collect._drain(self.cfg, relay, "ccccc")
        self.assertEqual(got["text"], "go ahead", "routed to the only candidate")
        self.assertEqual([m for m in relay.sent if m.get("kind") == "choose"], [],
                         "and nothing was asked")
        self.assertTrue(waiter.armed("ddddd"), "the permission still waits")

    def test_a_rejected_chooser_is_asked_again_not_remembered_as_asked(self):
        # The second half of the same production failure: the send was
        # rejected, the code recorded "asked" anyway, and the reply was never
        # mentioned again. Silence, permanently.
        self.arm("aaaaa", "/x/a")
        self.arm("bbbbb", "/x/b")
        relay = FakeRelay([[row("1", "hello")], [], []])
        relay.reject_kinds = ("choose",)
        collect._drain(self.cfg, relay, "aaaaa")
        self.assertFalse(spool.items()[0].get("asked"),
                         "an unsent question has not been asked")
        relay.reject_kinds = ()                      # Cliq recovers
        collect._drain(self.cfg, relay, "aaaaa")
        self.assertEqual(len([m for m in relay.sent if m.get("kind") == "choose"]), 1,
                         "and is asked on the next fetch")

    def test_labels_stay_distinct_when_two_sessions_share_a_folder_name(self):
        self.arm("aaaaa", "/one/work")
        self.arm("bbbbb", "/two/work")
        relay = FakeRelay([[row("1", "hello")]])
        collect._drain(self.cfg, relay, "aaaaa")
        labels = [a["label"] for a in relay.sent[0]["answers"]]
        self.assertEqual(len(set(labels)), 2, f"indistinguishable buttons: {labels}")

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


class Attachments(unittest.TestCase):
    """Bringing a picture sent from Cliq down to a path Claude can read.

    The reply must survive every failure here. A screenshot is the whole point
    of replying from a phone, but a reply that arrives without its picture is
    still a reply -- one that never arrives is a session stuck waiting.
    """

    def setUp(self):
        attach.FILES_DIR.mkdir(parents=True, exist_ok=True)
        for f in attach.FILES_DIR.iterdir():
            if f.is_file():
                f.unlink()

    def test_a_marker_becomes_a_real_path(self):
        png = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]) + b"body"

        class R:
            released = []

            def file_url(self, mid):
                return "http://example.invalid/x"

            def file_done(self, mid):
                R.released.append(mid)

        rows = [{"id": "m1", "text": "this bit is broken\n[attached: shot.png]"}]
        with mock_download(png):
            attach.fetch_into(rows, R())

        text = rows[0]["text"]
        self.assertIn("this bit is broken", text, "the words are kept")
        self.assertNotIn("[attached:", text, "the marker is replaced, not left")
        path = Path(text.split("[image attached: ")[1].rstrip("]"))
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes(), png, "byte-for-byte")
        self.assertEqual(R.released, ["m1"], "and the relay is told to drop it")

    def test_the_marker_names_what_actually_arrived(self):
        # A spreadsheet was announced as "[image attached: ...]", which tells a
        # session the wrong thing about what it can do with the path.
        import zipfile
        cases = {
            "shot.png": bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
            "photo.jpg": b"\xff\xd8\xff\xe0" + b"0" * 20,
            "notes.pdf": b"%PDF-1.4\n" + b"0" * 20,
        }
        for name, payload in cases.items():
            path = attach.FILES_DIR / name
            path.write_bytes(payload)
            want = "image" if name.endswith((".png", ".jpg")) else "document"
            self.assertEqual(attach.kind_of(path), want, name)

        book = attach.FILES_DIR / "trades.xlsx"
        with zipfile.ZipFile(book, "w") as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
        self.assertEqual(attach.kind_of(book), "file",
                         "a spreadsheet is not an image")

        # And an unreadable path must not raise on the delivery path.
        self.assertEqual(attach.kind_of(attach.FILES_DIR / "nope.bin"), "file")

    def test_an_attachment_with_no_caption_still_arrives(self):
        class R:
            def file_url(self, mid):
                return "http://example.invalid/x"

            def file_done(self, mid):
                pass

        rows = [{"id": "m2", "text": "[attached: shot.png]"}]
        with mock_download(b"\x89PNG\r\n\x1a\n"):
            attach.fetch_into(rows, R())
        self.assertTrue(rows[0]["text"].startswith("[image attached: "))

    def test_a_failed_download_keeps_the_reply_and_says_why(self):
        class R:
            def file_url(self, mid):
                raise RelayError("bucket is on fire")

        rows = [{"id": "m3", "text": "look at this\n[attached: shot.png]"}]
        attach.fetch_into(rows, R())
        self.assertIn("look at this", rows[0]["text"], "the reply is NOT lost")
        self.assertIn("could not be downloaded", rows[0]["text"])
        self.assertIn("bucket is on fire", rows[0]["text"], "with the reason")

    def test_a_reply_with_no_marker_is_untouched(self):
        class R:
            def file_url(self, mid):
                raise AssertionError("must not ask for a file")

        rows = [{"id": "m4", "text": "just words"}]
        attach.fetch_into(rows, R())
        self.assertEqual(rows[0]["text"], "just words")

    def test_a_filename_cannot_escape_the_download_directory(self):
        # The name comes from whichever device sent it. The relay sanitises
        # too, but the laptop is what actually opens a file.
        for hostile in ("../../.ssh/id_rsa", "/etc/passwd", "..\\..\\x.dll"):
            dest = attach._unique(hostile)
            self.assertEqual(dest.parent, attach.FILES_DIR,
                             f"{hostile} landed outside the download directory")

    def test_an_oversized_download_is_refused_mid_stream(self):
        # Checked as it arrives, so a lying Content-Length buys nothing.
        class R:
            def file_url(self, mid):
                return "http://example.invalid/x"

        rows = [{"id": "m5", "text": "big\n[attached: huge.png]"}]
        with mock_download(b"x" * (attach.MAX_BYTES + 1)):
            attach.fetch_into(rows, R())
        self.assertIn("could not be downloaded", rows[0]["text"])
        self.assertEqual([f for f in attach.FILES_DIR.iterdir() if f.is_file()], [],
                         "and nothing half-written is left behind")

    def test_old_downloads_are_pruned_and_recent_ones_kept(self):
        old = attach.FILES_DIR / "old.png"
        new = attach.FILES_DIR / "new.png"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        past = time.time() - attach.KEEP_SECONDS - 60
        os.utime(old, (past, past))
        attach.prune()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists(), "someone may still be talking about it")

    def test_two_files_with_the_same_name_do_not_collide(self):
        a = attach._unique("shot.png")
        a.write_bytes(b"first")
        b = attach._unique("shot.png")
        self.assertNotEqual(a, b)
        self.assertTrue(str(b.name).endswith(".png"), "the extension is kept")


class LongMessages(unittest.TestCase):
    """A long turn must reach Cliq whole, not cut with an ellipsis.

    summarize() trimmed to 1400 characters "to stay well under" Cliq's 5000,
    which cut the end off any substantial turn -- the part a message usually
    builds to. The relay splits long text across several messages now, so this
    side only has to stay inside what the relay will actually deliver.
    """

    def test_an_ordinary_long_turn_is_passed_through_whole(self):
        import hookutil as H
        text = "\n\n".join(f"Paragraph {i}. " + "x" * 200 for i in range(40))
        self.assertGreater(len(text), 5000, "longer than one Cliq message")
        self.assertEqual(H.summarize(text), text, "nothing trimmed")

    def test_the_backstop_says_what_it_did_instead_of_trailing_off(self):
        import hookutil as H
        out = H.summarize("word " * 6000)
        self.assertIn("cut here", out, "names itself as a cut")
        self.assertIn("at the terminal", out, "and says where the rest is")
        self.assertFalse(out.endswith("…"), "an ellipsis explains nothing")


class PlanMode(unittest.TestCase):
    """Approving a plan from Cliq.

    The plan used to stay on the laptop: Cliq showed "Approve the plan and
    start work" and Allow/Deny, so you approved something you could not read
    with choices that did not match the terminal's.
    """

    def test_the_plan_is_found_even_though_the_schema_is_undocumented(self):
        self.assertEqual(plan.plan_of({"tool_input": {"plan": "# Title\nbody"}}),
                         "# Title\nbody")
        # No `plan` key: the longest string is overwhelmingly the plan.
        self.assertEqual(
            plan.plan_of({"tool_input": {"note": "x", "content": "a much longer thing"}}),
            "a much longer thing")
        for junk in ({}, {"tool_input": None}, {"tool_input": {}}, {"tool_input": []}):
            self.assertEqual(plan.plan_of(junk), "")

    def test_the_choices_parse_with_the_relay_grammar(self):
        # render_choices() must invert in askform.parseQuestions, or tapping
        # Answer produces an empty form. The relay's own test asserts the
        # other direction; this one pins the shape here.
        text = plan.render_choices()
        self.assertTrue(text.startswith("*") and "*" in text[1:])
        numbered = [l for l in text.splitlines() if l[:2] in ("1.", "2.", "3.")]
        self.assertEqual(len(numbered), len(plan.CHOICES))

    def test_approving_sets_the_mode_the_option_describes(self):
        payload = {"tool_input": {"plan": "# P"}}
        auto = plan.decision(payload, "auto", "")["hookSpecificOutput"]["decision"]
        self.assertEqual(auto["behavior"], "allow")
        self.assertEqual(auto["updatedInput"], {"plan": "# P"},
                         "updatedInput REPLACES the input, so it must be carried")
        self.assertEqual(auto["updatedPermissions"],
                         [{"type": "setMode", "mode": "acceptEdits",
                           "destination": "session"}])

        review = plan.decision(payload, "review", "")["hookSpecificOutput"]["decision"]
        self.assertEqual(review["updatedPermissions"][0]["mode"], "default",
                         "sent explicitly -- the session is still in plan mode")

    def test_keep_planning_carries_the_feedback_to_claude(self):
        # The old deny sent the fixed string "Denied from Zoho Cliq", so
        # "keep planning" reached Claude with nothing to act on.
        d = plan.decision({"tool_input": {}}, "keep",
                          "split step 3 in two")["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "deny")
        self.assertEqual(d["message"], "split step 3 in two")
        self.assertNotIn("interrupt", d, "keep planning, do not stop the turn")
        self.assertNotIn("reason", d, "`reason` is silently dropped on this branch")

    def test_keep_planning_without_feedback_still_says_something(self):
        d = plan.decision({"tool_input": {}}, "keep", "")["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "deny")
        self.assertTrue(d["message"].strip(), "an empty message explains nothing")

    def test_a_caption_describes_the_document(self):
        self.assertEqual(plan.caption_for("# Sending a plan\n\nbody"), "Sending a plan")
        self.assertEqual(plan.caption_for("\n\n   \n"), "A plan to look over.")
        self.assertLessEqual(len(plan.caption_for("#  " + "x" * 900)), plan.MAX_CAPTION)


class NotifyOnlyHint(unittest.TestCase):
    """Telling someone their reply cannot land, before they type it.

    In notify-only mode a reply from Cliq reaches nothing: no hook is
    listening, so it sits in the relay until it expires and the person hears
    back nothing at all. A message they can answer and one they cannot look
    identical in the chat, which is exactly how someone ends up replying into
    the void -- reported after it happened.
    """

    def test_every_notify_only_message_names_the_mode_and_the_fix(self):
        for hint in (notify_only_hint(), notify_only_hint(waiting_at_terminal=True)):
            self.assertIn("notify-only", hint, "says which mode it is in")
            self.assertIn("/claude-cliq:away on", hint, "and how to change it")
            self.assertIn("won't reach", hint, "and that a reply goes nowhere")

    def test_the_terminal_wording_does_not_repeat_itself(self):
        hint = notify_only_hint(waiting_at_terminal=True)
        self.assertEqual(hint.count("terminal"), 1,
                         "said twice it reads like a mistake")

    def test_all_three_hooks_use_the_same_wording(self):
        # ask, permission and stop each had their own string; two of them said
        # only "waiting for you at the terminal", which does not tell anyone
        # that replying is pointless or how to fix it.
        src = {name: (Path(__file__).parent / f"{name}.py").read_text()
               for name in ("ask", "on_permission", "on_stop")}
        for name, text in src.items():
            self.assertIn("notify_only_hint", text,
                          f"{name} should not carry its own wording")
            self.assertNotIn('hint="waiting for you at the terminal"', text,
                             f"{name} still has the old hint")


class SessionTitle(unittest.TestCase):
    """Finding the name Claude Code's /resume picker shows."""

    def setUp(self):
        import tempfile
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.root = self.home / ".claude" / "projects"
        # A session STARTED in the home directory, so its transcript lives
        # under that path -- whatever the cwd becomes later.
        d = self.root / "-Users-someone"
        d.mkdir(parents=True)
        (d / "abc-123.jsonl").write_text(
            '{"type":"user"}\n'
            '{"type":"ai-title","aiTitle":"first guess","sessionId":"abc-123"}\n'
            '{"type":"assistant"}\n'
            '{"type":"ai-title","aiTitle":"the current one","sessionId":"abc-123"}\n')
        self._home = pathlib.Path.home
        pathlib.Path.home = staticmethod(lambda: self.home)

    def tearDown(self):
        pathlib.Path.home = self._home

    def test_the_newest_title_wins(self):
        # Claude Code rewrites ai-title as the conversation moves on.
        self.assertEqual(waiter.session_title("abc-123", "/Users/someone"),
                         "the current one")

    def test_a_cwd_that_moved_still_finds_the_transcript(self):
        # The bug a user hit: any `cd` during a session changes the cwd the
        # hook reports, but the transcript stays where the session STARTED.
        # Deriving the path from the current cwd missed silently, and the
        # chooser fell back to a folder name.
        for cwd in ("/Users/someone/project", "/Users/someone/project/deep", ""):
            self.assertEqual(waiter.session_title("abc-123", cwd),
                             "the current one", f"cwd={cwd!r}")

    def test_an_unknown_session_is_not_an_error(self):
        self.assertEqual(waiter.session_title("nope", "/Users/someone"), "")
        self.assertEqual(waiter.session_title("", "/Users/someone"), "")


class AwayMode(unittest.TestCase):
    """Three states: off, notify, on."""

    def setUp(self):
        clean()

    def tearDown(self):
        from cliqlib import set_mode
        set_mode("off")

    def test_each_mode_answers_the_two_questions_hooks_ask(self):
        # Every hook needs one of two thresholds: "should Cliq hear about
        # this at all" and "should the session wait".
        from cliqlib import set_mode, mode, is_away, notifies
        for m, says, waits in (("off", False, False),
                               ("notify", True, False),
                               ("on", True, True)):
            set_mode(m)
            self.assertEqual(mode(), m)
            self.assertEqual(notifies(), says, f"{m}: notifies")
            self.assertEqual(is_away(), waits, f"{m}: waits")

    def test_off_sends_nothing_and_returns_at_once(self):
        # Away mode used to gate only the BLOCKING and notify regardless, so
        # "off" still filled someone's Cliq with pings they had asked to stop.
        import subprocess, json as _j
        from cliqlib import set_mode
        set_mode("off")
        r = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).parent / "on_stop.py")],
            input=_j.dumps({"session_id": "t", "cwd": "/tmp/x",
                            "last_assistant_message": "an answer",
                            "stop_hook_active": False}),
            capture_output=True, text=True, env=dict(os.environ), timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", "no decision, and nothing sent")

    def test_an_old_timestamp_file_still_means_on(self):
        # The file used to hold a timestamp and its existence meant "on".
        # Anyone upgrading mid-session keeps the behaviour they had.
        from cliqlib import AWAY_PATH, mode
        AWAY_PATH.write_text("1792058070")
        self.assertEqual(mode(), "on")

    def test_an_unknown_mode_is_refused_rather_than_guessed(self):
        from cliqlib import set_mode
        with self.assertRaises(ValueError):
            set_mode("sometimes")

    def test_leaving_the_waiting_mode_revokes_blanket_approvals(self):
        # A blanket "allow all" was a concession to being away from the
        # keyboard. Notify-only means you are back at it.
        from cliqlib import set_mode, grant_blanket_approval, blanket_approval_until
        for leaving_to in ("notify", "off"):
            set_mode("on")
            grant_blanket_approval("sess", 600)
            self.assertTrue(blanket_approval_until("sess"))
            set_mode(leaving_to)
            self.assertFalse(blanket_approval_until("sess"), leaving_to)


class Transport(unittest.TestCase):
    """The escape hatch has to be real, not just configurable."""

    def setUp(self):
        clean()

    def arm(self, rid):
        waiter.arm(rid, session_id="s", kind="finished", cwd="/tmp",
                   expires_at=time.time() + 60)

    def test_a_quiet_stream_also_mints_a_fresh_ticket(self):
        # A silent subscription is as spent as a closed one. But NOT every
        # retryable error renews: a ticket costs a Data Store read, so
        # retrying stays cheap while re-ticketing is reserved for causes that
        # genuinely end the session.
        import collect as _collect
        src = (Path(__file__).parent / "collect.py").read_text()
        self.assertIn("went quiet", src, "a quiet stream should renew")
        self.assertIn("closed the connection", src, "so should a close frame")

    def test_a_spent_ticket_is_replaced_after_the_server_closes(self):
        # Reported as "I got this message again": the doorbell warning came
        # back the day after the close was made retryable. It HAD reconnected
        # -- "connection closed by the server - reconnecting (2/3)" -- and
        # then failed with "Data Streams rejected the ticket", because listen
        # retried with the same ticket and that session no longer existed.
        #
        # The note saying never to re-mint described the old per-device
        # connection name. Tickets have minted a random name each since the
        # lease bug, so renewing is safe and necessary.
        import wake as _wake
        calls = []

        def renew(exc):
            calls.append(str(exc))
            return {"url": "u", "key": "k2", "wss_id": "w2"}

        tickets = []

        def fake_once(ticket, **kw):
            tickets.append(ticket["key"])
            if len(tickets) == 1:
                raise _wake.WakeError("connection closed by the server",
                                      retryable=True)
            return "delivered"

        original = _wake._listen_once
        _wake._listen_once = fake_once
        try:
            got = _wake.listen({"url": "u", "key": "k1", "wss_id": "w1"},
                               deadline=time.time() + 5, on_wake=lambda b: None,
                               renew=renew)
        finally:
            _wake._listen_once = original

        self.assertEqual(got, "delivered")
        self.assertEqual(tickets, ["k1", "k2"], "the retry used a FRESH ticket")
        self.assertIn("closed by the server", calls[0], "renew saw the reason")

    def test_the_first_connect_keeps_its_ticket(self):
        # SESSION_EXPIRED fires on almost every first connect and is fixed by
        # retrying with the ticket already in hand. Minting a new one each
        # time would double the ticket calls for nothing.
        import wake as _wake
        tickets = []

        def renew(exc):
            return None if "not ready" in str(exc) else {"url": "u", "key": "new",
                                                         "wss_id": "w"}

        def fake_once(ticket, **kw):
            tickets.append(ticket["key"])
            if len(tickets) == 1:
                raise _wake.WakeError("session not ready yet", retryable=True)
            return "delivered"

        original = _wake._listen_once
        _wake._listen_once = fake_once
        try:
            _wake.listen({"url": "u", "key": "k1", "wss_id": "w1"},
                         deadline=time.time() + 5, on_wake=lambda b: None,
                         renew=renew)
        finally:
            _wake._listen_once = original
        self.assertEqual(tickets, ["k1", "k1"], "same ticket, as measured")

    def test_a_transport_failure_is_retryable_unless_argued_otherwise(self):
        # The default used to be fatal, and it cost the doorbell three times in
        # two days: a spent ticket after a close, a polite close frame, and a
        # subscription that went silent. Each was found only because replies
        # were late, since the polling fallback hides the loss.
        #
        # This is the test that would have caught all three at once.
        self.assertTrue(wake.WakeError("something nobody has classified").retryable)

        for message in ("connection closed by the server",
                        "server closed during the handshake",
                        "cannot reach host:443 (blocked)",
                        "timed out waiting for the upgrade response",
                        "the stream went quiet"):
            self.assertTrue(wake.WakeError(message).retryable, message)

    def test_the_fatal_cases_are_the_ones_retrying_cannot_help(self):
        # Credentials, authorisation, and something that is not a WebSocket
        # peer at all. Asking those again changes nothing.
        self.assertFalse(
            wake.WakeError("Data Streams rejected the ticket", retryable=False).retryable)
        src = (Path(__file__).parent / "wake.py").read_text()
        for marker in ("_FATAL[mtype], retryable=False",
                       "WebSocket peer\", retryable=False"):
            self.assertIn(marker, src, f"{marker} should be explicitly fatal")

    def test_a_polite_close_reconnects_but_a_policy_close_does_not(self):
        # 1001 "going away" and 1012 "service restart" mean come back; 1008
        # says we are unwelcome. Before this, EVERY close frame was fatal, so
        # a courteous server was the one thing that dropped us to polling.
        src = (Path(__file__).parent / "wake.py").read_text()
        self.assertIn("retryable=code != 1008", src)
        self.assertIn('payload[2:].decode', src,
                      "the close reason text should be read, not discarded")

    def test_poll_mode_never_opens_the_stream(self):
        # The whole point of the hatch: a surprise bill or a blocked port must
        # be escapable without a working stream to escape with.
        cfg = Config(transport="poll", poll_fallback_seconds=0.01)
        self.arm("aaaaa")
        relay = FakeRelay([[row("1", "polled", request_id="aaaaa")]])
        relay.stream_ticket = lambda: self.fail("poll mode asked for a ticket")
        got = collect.wait_for(cfg, relay, "aaaaa", time.time() + 5)
        self.assertEqual(got["text"], "polled")

    def test_a_hold_set_mid_listen_brings_the_leader_back_early(self):
        # The hold is decided INSIDE the wake handler, by which point listen()
        # is already waiting on the full window. Without a way out, the leader
        # returns whenever the next unrelated event happens to arrive -- a
        # 12-second hold was measured taking 28 seconds to deliver.
        # A short quiet period: the point is the DEADLINE the leader listens
        # on, not how long it is.
        cfg = Config(transport="stream", attachment_quiet_seconds=0.3,
                     attachment_grace_seconds=120.0, stale_after_seconds=1800)
        self.arm("aaaaa")
        deadline = time.time() + 1800
        # First fetch empty, then the picture, then nothing more.
        relay = FakeRelay([[], [row("1", "[image attached: /tmp/a.png]", age_s=0)], []])

        import wake as _wake
        original = _wake.listen
        deadlines = []

        def fake_listen(creds, *, deadline, on_wake, log=lambda *_: None, **kw):
            deadlines.append(deadline)
            if len(deadlines) == 1:
                return on_wake({"w": "deadbeef"})      # the picture arrives
            # A real listen blocks until its deadline; mimic that, or the
            # leader's loop would be measuring nothing.
            time.sleep(max(0.0, min(deadline - time.time(), 1.0)))
            return None

        _wake.listen = fake_listen
        try:
            collect._lead(cfg, relay, "aaaaa", deadline)
        finally:
            _wake.listen = original

        self.assertGreaterEqual(len(deadlines), 2, "it listened again")
        self.assertAlmostEqual(deadlines[0], deadline, delta=1,
                               msg="the first listen waits on the whole window")
        self.assertLess(deadlines[1], deadline - 1000,
                        "the second waits only for the hold, not the window")
        # And once the quiet passes, the held picture is actually delivered.
        self.assertEqual(spool.items(), [], "nothing left holding")

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
