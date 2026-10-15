#!/usr/bin/env python3
"""Durable holding area for replies fetched from the relay but not yet accepted.

Two jobs, both learned the hard way.

**Durability.** `/await` is destructive: it returns rows and deletes them in
the same call. So a process that dies between the HTTP response and
`waiter.resolve()` loses the reply permanently. The old daemon covered this
with `held_replies.json`; a per-hook fetcher has nothing unless it writes
first. Everything fetched lands here *before* it is routed, and leaves only
once a waiter has actually taken it.

**Age.** A reply that arrives with nothing waiting used to be held by the
daemon and bounded by its idle grace. With the daemon gone a row can sit in
the relay for its full 24-hour TTL, and `waiter.route()` with a single armed
waiter will happily hand it over -- so a reply typed hours ago becomes the
answer to a brand-new question. Observed, not theorised: a reply from twenty
minutes earlier was collected as current on the first end-to-end run.

Stdlib only. Guarded by `flock` because several blocked hooks may touch it at
once, and the leader can change hands mid-flight.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path

from cliqlib import STATE_DIR, log

SPOOL_PATH = STATE_DIR / "spool.json"
LOCK_PATH = STATE_DIR / "spool.lock"

# Keep at most this many. A spool that grows without bound is a spool nobody
# ever looks at; the cap is generous next to any real conversation.
MAX_ITEMS = 50


class _Lock:
    """Serialise spool access across processes."""

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = LOCK_PATH.open("a+")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except Exception:
            pass
        return False


def _read() -> list[dict]:
    try:
        data = json.loads(SPOOL_PATH.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(items: list[dict]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SPOOL_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(items))
        tmp.replace(SPOOL_PATH)
    except Exception as exc:
        log(f"spool: could not write: {exc}")


def add(rows: list[dict]) -> None:
    """Record fetched rows before anything is done with them.

    Keyed by the relay's row id, so a re-fetch of the same row (a retry, or a
    leader handover) does not duplicate it.
    """
    if not rows:
        return
    with _Lock():
        items = _read()
        seen = {i.get("id") for i in items}
        for r in rows:
            if r.get("id") in seen:
                continue
            items.append({
                "id": r.get("id"),
                "text": r.get("text") or "",
                "request_id": (r.get("request_id") or "").lower(),
                # The relay's timestamp, not ours: the age that matters is how
                # long ago the person typed it, not when we got round to it.
                "ts": float(r.get("ts") or 0) / 1000.0,
                "spooled_at": time.time(),
            })
        _write(items[-MAX_ITEMS:])


def items() -> list[dict]:
    with _Lock():
        return _read()


def drop(ids) -> None:
    """Remove rows a waiter has accepted."""
    gone = {i for i in ids if i}
    if not gone:
        return
    with _Lock():
        _write([i for i in _read() if i.get("id") not in gone])


def prune(max_age_seconds: float) -> list[dict]:
    """Remove and return anything too old to be an answer to anything.

    Age is measured from when the reply was typed. A row with no usable
    timestamp is kept rather than discarded -- dropping a reply because its
    clock looked odd is worse than delivering it late.
    """
    now = time.time()
    with _Lock():
        items_, keep, stale = _read(), [], []
        for i in items_:
            ts = float(i.get("ts") or 0)
            if ts and now - ts > max_age_seconds:
                stale.append(i)
            else:
                keep.append(i)
        if stale:
            _write(keep)
    for i in stale:
        log(f"spool: dropping a reply typed {int(now - float(i['ts']))}s ago: "
            f"{i.get('text', '')[:60]!r}")
    return stale
