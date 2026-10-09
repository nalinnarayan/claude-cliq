#!/usr/bin/env python3
"""A local stand-in for the Catalyst relay.

Implements the same four routes with the same semantics -- including the
one that matters most: the recipient and the message owner are always
derived from the device token, never from the request body. Lets the whole
laptop side be proven before any Zoho credential exists.

  python3 mockrelay.py                 # serve on 127.0.0.1:8899
  python3 mockrelay.py --say "go"      # act as the user typing in Cliq
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8899
LOCK = threading.Lock()

# Stands in for the relay's NoSQL tables.
PAIR_CODES: dict[str, str] = {}        # code -> cliq_user_id
DEVICES: dict[str, dict] = {}          # device_token -> {user, name}
PENDING: dict[str, list[dict]] = {}    # cliq_user_id -> inbound messages
SENT: list[dict] = []                  # everything the bot "DM'd"

DEMO_USER = "demo@zohocorp.com"


def _now() -> float:
    return time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quieter console
        pass

    # -- helpers ---------------------------------------------------------
    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _owner(self) -> str | None:
        """Resolve the caller's Cliq user from the bearer token alone."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        with LOCK:
            dev = DEVICES.get(token)
        return dev["user"] if dev else None

    # -- routes ----------------------------------------------------------
    def do_POST(self):
        route = self.path.split("?")[0]
        body = self._body()

        if route == "/pair":
            code = str(body.get("code", "")).strip().lower()
            with LOCK:
                user = PAIR_CODES.pop(code, None)
            if not user:
                return self._send(403, {"error": "bad or expired code"})
            token = secrets.token_hex(32)
            with LOCK:
                DEVICES[token] = {"user": user,
                                  "name": body.get("device_name", "laptop")}
            print(f"  [relay] paired {user} <- device {body.get('device_name')}")
            return self._send(200, {"device_token": token, "cliq_user": user})

        if route == "/unpair":
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            with LOCK:
                DEVICES.pop(token, None)
            return self._send(200, {"ok": True})

        if route == "/notify":
            user = self._owner()
            if not user:
                return self._send(401, {"error": "unpaired device"})
            msg = {
                "to": user,                       # derived, never from body
                "kind": body.get("kind"),
                "text": body.get("text", ""),
                "request_id": body.get("request_id"),
                "session_id": body.get("session_id"),
                "cwd": body.get("cwd"),
                "expects_reply": bool(body.get("expects_reply")),
                "hint": body.get("hint", ""),
                "ts": _now(),
            }
            with LOCK:
                SENT.append(msg)
            tag = f"[{msg['request_id']}]" if msg["request_id"] else ""
            print(f"\n  ┌─ Cliq DM to {user} {tag}")
            for line in (msg["text"] or "").splitlines() or [""]:
                print(f"  │ {line}")
            if msg["hint"]:
                print(f"  │ … {msg['hint']}")
            print("  └─")
            return self._send(200, {"ok": True, "request_id": msg["request_id"]})

        if route == "/await":
            user = self._owner()
            if not user:
                return self._send(401, {"error": "unpaired device"})
            since = float(body.get("since") or 0)
            wait = min(int(body.get("wait") or 25), 30)
            deadline = _now() + wait
            while True:
                with LOCK:
                    # Only ever this token-owner's messages.
                    msgs = [m for m in PENDING.get(user, []) if m["ts"] > since]
                if msgs or _now() >= deadline:
                    return self._send(200, {"messages": msgs, "now": _now()})
                time.sleep(0.25)

        if route == "/cliq-inbound":
            # Stands in for the Deluge message_handler's invokeurl.
            user = body.get("user") or DEMO_USER
            msg = {"id": secrets.token_hex(6), "text": body.get("text", ""),
                   "user": user, "ts": _now()}
            with LOCK:
                PENDING.setdefault(user, []).append(msg)
            print(f"  [relay] inbound from {user}: {msg['text']!r}")
            return self._send(200, {"ok": True})

        self._send(404, {"error": "no such route"})

    def do_GET(self):
        if self.path.startswith("/newcode"):
            code = secrets.token_hex(3)
            with LOCK:
                PAIR_CODES[code] = DEMO_USER
            return self._send(200, {"code": code, "user": DEMO_USER})
        self._send(404, {"error": "no such route"})


def say(text: str) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/cliq-inbound",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        print(r.read().decode())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--say", help="simulate the user replying in Cliq")
    args = ap.parse_args()
    if args.say:
        return say(args.say)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"mock relay on http://127.0.0.1:{PORT}  (user {DEMO_USER})")
    print(f"  pair:  curl -s localhost:{PORT}/newcode")
    print(f"  reply: python3 mockrelay.py --say 'your text'")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
