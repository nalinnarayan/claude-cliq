"""Bringing an image sent from Cliq down to where Claude can read it.

A reply that came with a file arrives carrying a marker the RELAY appended --
`[attached: name.png]` -- because the Pending row has no column for it and the
message text is already a round-trip format this bridge relies on elsewhere.
The marker is set server-side rather than by the Cliq handler so that a caption
which happens to contain one cannot forge it.

Here that marker is turned into a real file on disk and rewritten to its path,
which is the only form Claude can act on: it reads images by path, and a Cliq
attachment URL is transient and expires within minutes.

A download that fails must never cost the reply. The text is always delivered;
on failure the marker becomes a line saying the picture could not be fetched,
so the session hears "look at this" together with the reason there is nothing
to look at.
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from cliqlib import STATE_DIR, Relay, RelayError, log

FILES_DIR = STATE_DIR / "files"
MARKER_RE = re.compile(r"\n?\[attached: ([^\]\n]{1,120})\]\s*$")

# Enough for a screenshot or a photo; anything larger is a mistake rather than
# a message, and the relay refuses it too.
MAX_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30

# Kept so a session can refer back to a picture a few replies later, but not
# forever: these are someone's screenshots sitting in a home directory.
KEEP_SECONDS = 7 * 24 * 3600


def marker_name(text: str) -> str:
    """The attachment's name, if this reply came with one."""
    m = MARKER_RE.search(text or "")
    return m.group(1) if m else ""


def _unique(name: str) -> Path:
    """A path that does not collide, keeping the sender's name readable."""
    safe = Path(name).name.replace("/", "_").strip() or "attachment"
    stem, dot, ext = safe.rpartition(".")
    stem = stem or safe
    ext = f".{ext}" if dot else ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(100):
        suffix = "" if n == 0 else f"-{n}"
        path = FILES_DIR / f"{stamp}-{stem}{suffix}{ext}"
        if not path.exists():
            return path
    return FILES_DIR / f"{stamp}-{int(time.time() * 1000)}{ext}"


def _download(url: str, dest: Path) -> int:
    """Fetch to disk, refusing anything implausibly large.

    Streamed rather than read whole: the size is checked as it arrives, so a
    wrong or hostile Content-Length cannot be taken on trust.
    """
    req = urllib.request.Request(url, method="GET")
    written = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, tmp.open("wb") as out:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_BYTES:
                tmp.unlink(missing_ok=True)
                raise RelayError(f"file is larger than {MAX_BYTES} bytes")
            out.write(chunk)
    # Renamed only once it is whole, so a half-written file is never handed to
    # a session as though it were an image.
    tmp.replace(dest)
    return written


def prune(now: float | None = None) -> int:
    """Drop downloads old enough that nothing is still talking about them."""
    now = time.time() if now is None else now
    gone = 0
    try:
        for path in FILES_DIR.iterdir():
            try:
                if path.is_file() and now - path.stat().st_mtime > KEEP_SECONDS:
                    path.unlink()
                    gone += 1
            except OSError:
                pass
    except FileNotFoundError:
        pass
    return gone


def fetch_into(rows: list[dict], relay: Relay) -> None:
    """Replace each row's marker with the path of the downloaded file.

    Mutates `rows` in place, before they are spooled, so the durable record
    already holds the path and no second pass is needed. Every failure is
    contained: the reply text survives in every branch.
    """
    for row in rows:
        text = row.get("text") or ""
        name = marker_name(text)
        if not name:
            continue
        message_id = str(row.get("id") or "")
        body = MARKER_RE.sub("", text).rstrip()
        try:
            FILES_DIR.mkdir(parents=True, exist_ok=True)
            url = relay.file_url(message_id)
            if not url:
                raise RelayError("the relay had no file for that message")
            dest = _unique(name)
            size = _download(url, dest)
            log(f"attach: saved {dest} ({size} bytes)")
            row["text"] = (f"{body}\n[image attached: {dest}]" if body
                           else f"[image attached: {dest}]")
            try:
                relay.file_done(message_id)
            except (RelayError, urllib.error.URLError) as exc:
                # The object lingers; the picture is already safely on disk.
                log(f"attach: could not release {message_id}: {exc}")
            prune()
        except (RelayError, urllib.error.URLError, OSError, ValueError) as exc:
            log(f"attach: {name} failed: {exc}")
            row["text"] = (f"{body}\n[they attached {name}, but it could not be "
                           f"downloaded: {exc}]" if body
                           else f"[they attached {name}, but it could not be "
                                f"downloaded: {exc}]")
