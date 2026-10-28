"""The whole doorbell, end to end: listen for this account's wake, then fetch."""
import json, pathlib, sys, time, urllib.request
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import wake

t = json.loads(pathlib.Path(sys.argv[1]).read_text())
RELAY, TOKEN = sys.argv[2], sys.argv[3]
log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)

def fetch():
    req = urllib.request.Request(f"{RELAY}/await", method="POST",
                                 data=json.dumps({"since": 0, "wait": 0}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "X-Device-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def on_wake(body):
    if not isinstance(body, dict) or body.get("w") != t["wake_id"]:
        log(f"wake for another account, ignoring: {body}")
        return None
    log("MY WAKE -> fetching")
    msgs = fetch().get("messages", [])
    for m in msgs:
        log(f"COLLECTED: {m.get('text')!r} (request_id={m.get('request_id') or 'none'})")
    return msgs or None

log(f"listening for wake_id {t['wake_id']}")
def renew():
    req = urllib.request.Request(f"{RELAY}/stream-ticket", method="POST",
                                 data=b"{}",
                                 headers={"Content-Type": "application/json",
                                          "X-Device-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        n = json.loads(r.read())
    log("renewed the ticket")
    return {"url": n["url"], "key": n["key"], "wss_id": n["wss_id"]}

got = wake.listen({"url": t["url"], "key": t["key"], "wss_id": t["wss_id"]},
                  deadline=time.time() + 900, on_wake=on_wake, log=log,
                  renew=renew)
log(f"done: collected {len(got or [])} message(s)")
