#!/usr/bin/env python3
"""Minimal WebSocket client for Catalyst Data Streams -- the doorbell receiver.

Stdlib only, like everything else on the laptop side: a teammate installs the
plugin with no pip step, so `websocket-client` is not an option and neither is
running Node here.

Written against the real protocol, recovered by building Zoho's own
`@zcatalyst/datastreams` from source -- the package is documented but not
published to npm. What it does:

    connect    wss://<host>/wsconnect?prd=CY&zuid=<wss_id>&key=<key>
    subscribe  {"type":"con","value":"subscribe","streamingId":"0"}
    ack        {"type":"ack","streamingId":"<id>"}
    keepalive  a literal "-" every 15s, {"type":"con","value":"ping"} every 60s

Every control payload but the ack carries `type: "con"`. Leaving it off is not
rejected as malformed -- the server answers with SESSION_EXPIRED (-11), which
sends you hunting for a stale ticket instead of a missing field.

Incoming frames are a JSON **array**; `[0].mtype` says what kind. An empty
payload is a pong. Data events carry `msg.opr` of "event" or "api" and
`msg.data.{streamingId,data}`.

Acks matter more than they look: delivery is strictly sequential per
subscriber, so an unacknowledged message withholds every later one. Measured --
a subscriber that never acked received exactly one message while another
received three. So this acks on RECEIPT, before the caller processes anything:
a wake is content-free, so losing one to a crash is recoverable from the
Pending table, while a stalled cursor is not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import ssl
import struct
import time
from urllib.parse import urlencode

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"      # RFC 6455, section 1.3

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

# Message types, from the SDK's own enum. The negative ones are all fatal.
MT_SWITCH_URL = "-1"
MT_MISSING_KEY = "-2"
MT_AUTH_FAILED = "-5"
MT_SESSION_EXPIRED = "-11"
MT_AUTH_SUCCESS = "0"
MT_BLOCKED = "660"
MT_DATA_EVENT = "650"
MT_SERVER_DOWN = "670"

_FATAL = {
    MT_MISSING_KEY: "the ticket carried no key",
    MT_AUTH_FAILED: "Data Streams rejected the ticket",
    MT_BLOCKED: "this connection is blocked by Zoho; contact support",
}

# Say "reconnect", not "give up" -- whatever their wording suggests.
#
# SESSION_EXPIRED is the liar here. It fires on the FIRST connect with a
# brand-new connection name, with a ticket minted seconds earlier, and the
# very same ticket then works on the next attempt. Taking it at its word sends
# you hunting for a stale credential while the real answer is "try again".
_RETRYABLE = {
    MT_SESSION_EXPIRED: "session not ready yet",
    MT_SERVER_DOWN: "Data Streams reported a server error",
    MT_SWITCH_URL: "the server asked us to move host",
}

PING_EVERY = 15.0           # a literal "-" frame
SERVER_PING_EVERY = 60.0    # {"value":"ping"}


class WakeError(Exception):
    """Anything that makes the socket unusable.

    `retryable` means reconnecting is likely to work; the caller should not
    surface it to a human on the first occurrence.
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class _Conn:
    """Just enough of RFC 6455 to be a client, and nothing more.

    Only the client half: every frame we send is masked (the spec requires it)
    and we never mask what we receive.
    """

    def __init__(self, host: str, path: str, port: int = 443, timeout: float = 20.0):
        self.host, self.path, self.port = host, path, port
        raw = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(raw, server_hostname=host)
        self._buf = b""
        self._handshake(timeout)

    # -- handshake ---------------------------------------------------------

    def _handshake(self, timeout: float) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())

        deadline = time.time() + timeout
        head = b""
        while b"\r\n\r\n" not in head:
            if time.time() > deadline:
                raise WakeError("timed out waiting for the upgrade response")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WakeError("server closed during the handshake")
            head += chunk
        header_blob, _, rest = head.partition(b"\r\n\r\n")
        self._buf = rest                      # frames may already be in here

        lines = header_blob.decode("latin-1").split("\r\n")
        if "101" not in lines[0]:
            raise WakeError(f"upgrade refused: {lines[0]}")

        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        got = ""
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                got = value.strip()
        if got != want:
            # A proxy that answers 101 without understanding WebSocket would
            # otherwise look like a working connection that never delivers.
            raise WakeError("Sec-WebSocket-Accept did not match; not a real WebSocket peer")

    # -- raw frames --------------------------------------------------------

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        while len(self._buf) < n:
            if time.time() > deadline:
                raise TimeoutError
            self._wait_readable(deadline)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WakeError("connection closed by the server")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _wait_readable(self, deadline: float) -> None:
        # SSL buffers internally, so select() can report "nothing to read"
        # while bytes are already decrypted and waiting.
        if self.sock.pending():
            return
        remaining = max(0.0, deadline - time.time())
        if not select.select([self.sock], [], [], remaining)[0]:
            raise TimeoutError

    def send(self, opcode: int, payload: bytes = b"") -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        mask = os.urandom(4)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 1 << 16:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def send_text(self, text: str) -> None:
        self.send(OP_TEXT, text.encode())

    def read_message(self, deadline: float) -> tuple[int, bytes]:
        """One complete application message, reassembling fragments.

        Control frames are answered here and never surface to the caller --
        except CLOSE, which is returned so the caller can stop.
        """
        opcode = None
        chunks: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2, deadline)
            fin = bool(b0 & 0x80)
            op = b0 & 0x0F
            masked = bool(b1 & 0x80)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2, deadline))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8, deadline))[0]
            mask = self._recv_exact(4, deadline) if masked else None
            payload = self._recv_exact(n, deadline) if n else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if op == OP_PING:
                self.send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                return OP_CLOSE, payload
            if op != OP_CONT:
                opcode = op
                chunks = [payload]
            else:
                chunks.append(payload)
            if fin:
                return (opcode if opcode is not None else OP_TEXT), b"".join(chunks)

    def close(self) -> None:
        try:
            self.send(OP_CLOSE, struct.pack("!H", 1000))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def connect(ticket: dict, *, streaming_id: str = "0", timeout: float = 20.0) -> _Conn:
    """Open a subscriber connection and subscribe.

    `ticket` is what the relay's /stream-ticket returns: {url, key, wss_id}.
    `streaming_id` is "0" for live events only, which is what this bridge
    wants -- gaps are covered by one cheap fetch when a hook starts waiting,
    not by replaying 48 hours of every account's wakes through a shared bus.
    """
    url = str(ticket["url"])
    key = str(ticket["key"])
    zuid = str(ticket.get("wss_id") or ticket.get("wss-id") or "")
    query = urlencode({"prd": "CY", "zuid": zuid, "key": key})
    conn = _Conn(url, f"/wsconnect?{query}", timeout=timeout)
    conn.send_text(json.dumps(
        {"type": "con", "value": "subscribe", "streamingId": str(streaming_id)}))
    return conn


def listen(ticket: dict, *, deadline: float, on_wake, log=lambda *_: None,
           streaming_id: str = "0", renew=None, attempts: int = 4) -> str | None:
    """Watch for wakes until `deadline`, reconnecting when told to.

    Calls `on_wake(payload_dict)` for every data event. If it returns a truthy
    value, that value is returned from here and the socket is closed -- which
    is how a blocked hook stops as soon as its own wake arrives.

    `renew()` may return a fresh ticket, but note that re-minting for a
    connection name whose session is still alive is REFUSED ("Duplicate value
    for connection_name") -- close() keeps the session on purpose. Retrying
    with the same ticket is what works for the transient SESSION_EXPIRED, so
    callers normally pass nothing.

    Returns None on timeout.
    """
    delay = 1.0
    for attempt in range(1, attempts + 1):
        if time.time() >= deadline:
            return None
        try:
            return _listen_once(ticket, deadline=deadline, on_wake=on_wake,
                                log=log, streaming_id=streaming_id)
        except WakeError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            log(f"wake: {exc} — reconnecting ({attempt}/{attempts - 1})")
            time.sleep(min(delay, max(0.0, deadline - time.time())))
            delay *= 2
            if renew:
                try:
                    ticket = renew()
                except Exception as err:          # a failed renew is not fatal
                    log(f"wake: could not renew the ticket: {err}")
    return None


def _listen_once(ticket: dict, *, deadline: float, on_wake, log,
                 streaming_id: str) -> str | None:
    conn = connect(ticket, streaming_id=streaming_id)
    last_ping = last_server_ping = time.time()
    try:
        while time.time() < deadline:
            now = time.time()
            if now - last_ping >= PING_EVERY:
                conn.send_text("-")
                last_ping = now
            if now - last_server_ping >= SERVER_PING_EVERY:
                conn.send_text(json.dumps({"type": "con", "value": "ping"}))
                last_server_ping = now

            # Wake up often enough to keep the keepalives honest.
            slice_end = min(deadline, now + 5.0)
            try:
                op, payload = conn.read_message(slice_end)
            except TimeoutError:
                continue

            if op == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0
                raise WakeError(f"server closed the connection (code {code})")

            text = payload.decode("utf-8", "replace").strip()
            if not text:
                continue                      # an empty frame is a pong

            try:
                events = json.loads(text)
            except ValueError:
                log(f"wake: unparseable frame: {text[:120]}")
                continue
            if not isinstance(events, list) or not events:
                continue

            mtype = str(events[0].get("mtype", ""))
            if mtype in _FATAL:
                raise WakeError(_FATAL[mtype])
            if mtype in _RETRYABLE:
                raise WakeError(_RETRYABLE[mtype], retryable=True)
            if mtype == MT_AUTH_SUCCESS:
                log("wake: subscribed")
                continue
            if mtype != MT_DATA_EVENT:
                log(f"wake: ignoring message type {mtype}")
                continue

            for item in events:
                msg = item.get("msg") or {}
                data = msg.get("data") or {}
                sid = data.get("streamingId")
                # Ack FIRST. Sequential delivery means a late ack costs every
                # later message, and the payload here is never precious.
                if sid is not None:
                    conn.send_text(json.dumps({"type": "ack", "streamingId": sid}))
                if msg.get("opr") != "event":
                    continue
                body = data.get("data")
                if isinstance(body, str):
                    try:
                        body = json.loads(body)
                    except ValueError:
                        pass
                got = on_wake(body)
                if got:
                    return got
        return None
    finally:
        conn.close()
