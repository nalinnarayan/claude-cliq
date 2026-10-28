"""Three sessions blocked at once. Does any of them give up early?"""
import json, os, sys, tempfile, time
from multiprocessing import Process, Queue

HOME = tempfile.mkdtemp(prefix="three-")
os.environ["CLAUDE_CLIQ_HOME"] = HOME
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

def session(rid, q, home):
    os.environ["CLAUDE_CLIQ_HOME"] = home
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
    import collect, waiter
    from cliqlib import Config

    class R:                       # never answers anybody
        def poll(self, since=0, wait=0, timeout=None): return {"messages": []}
        def notify(self, **kw): pass
        def stream_ticket(self): raise RuntimeError("no stream in this test")

    cfg = Config(transport="poll", poll_fallback_seconds=1.0,
                 poll_fallback_max_seconds=2.0, poll_fallback_budget=99)
    waiter.arm(rid, session_id=f"s-{rid}", kind="finished", cwd="/x/" + rid,
               expires_at=time.time() + 60)
    t0 = time.time()
    got = collect.wait_for(cfg, R(), rid, time.time() + 30)
    q.put((rid, round(time.time() - t0, 1), got, waiter.armed(rid)))

if __name__ == "__main__":
    q = Queue()
    ps = [Process(target=session, args=(r, q, HOME)) for r in ("aaaaa", "bbbbb", "ccccc")]
    for p in ps:
        p.start(); time.sleep(1.5)          # stagger, as real sessions do
    for p in ps: p.join(45)
    out = sorted(q.get() for _ in range(len(ps)))
    print(f"{'session':9} {'waited':>7}  gave up early?")
    for rid, secs, got, still in out:
        early = secs < 25
        print(f"  {rid:7} {secs:6.1f}s  {'YES — BUG' if early else 'no'}"
              f"   (returned {got!r}, still armed={still})")
