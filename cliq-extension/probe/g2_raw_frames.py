import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import wake

tp = json.loads(pathlib.Path(sys.argv[1]).read_text())["steps"]["tokenPair"]["result"]
subscribe = len(sys.argv) > 2 and sys.argv[2] == "subscribe"

from urllib.parse import urlencode
q = urlencode({"prd": "CY", "zuid": tp["wss_id"], "key": tp["key"]})
print(f"{time.strftime('%H:%M:%S')} handshaking to {tp['url']}/wsconnect", flush=True)
c = wake._Conn(tp["url"], f"/wsconnect?{q}")
print(f"{time.strftime('%H:%M:%S')} handshake OK (101 + accept verified)", flush=True)

if subscribe:
    p = json.dumps({"type": "con", "value": "subscribe", "streamingId": "0"})
    c.send_text(p)
    print(f"{time.strftime('%H:%M:%S')} sent {p}", flush=True)

end = time.time() + 10
while time.time() < end:
    try:
        op, payload = c.read_message(min(end, time.time() + 3))
    except TimeoutError:
        print(f"{time.strftime('%H:%M:%S')} (quiet)", flush=True)
        continue
    except Exception as e:
        print(f"{time.strftime('%H:%M:%S')} ERR {type(e).__name__}: {e}", flush=True)
        break
    print(f"{time.strftime('%H:%M:%S')} op={op:#x} payload={payload[:1500]!r}", flush=True)
c.close()
