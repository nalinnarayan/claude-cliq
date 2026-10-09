"""Shared plumbing for the Claude Code <-> Zoho Cliq bridge.

Stdlib only, on purpose: the plugin must install for a teammate with no
pip step. Everything here is used by the hooks, the daemon and the CLI.
"""

from __future__ import annotations

import json
import os
import random
import secrets
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

STATE_DIR = Path(os.environ.get("CLAUDE_CLIQ_HOME", Path.home() / ".claude" / "cliq"))
CONFIG_PATH = STATE_DIR / "config.json"
AWAY_PATH = STATE_DIR / "away"
SESSIONS_DIR = STATE_DIR / "sessions"
WAITERS_DIR = STATE_DIR / "waiters"
INBOX_PATH = STATE_DIR / "inbox.jsonl"
APPROVALS_DIR = STATE_DIR / "approvals"
DAEMON_LOCK = STATE_DIR / "cliqd.lock"
LOG_PATH = STATE_DIR / "cliq.log"

# Claude Code's own convention for human-typeable ids: five lowercase
# letters with 'l' left out so it can't be confused with '1'.
_ID_ALPHABET = "".join(c for c in string.ascii_lowercase if c != "l")


def new_request_id() -> str:
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(5))


def log(msg: str) -> None:
    """Best-effort local log. Never raises -- callers are hooks."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n"
        with LOG_PATH.open("a") as fh:
            fh.write(line)
    except Exception:
        pass


@dataclass
class Config:
    relay_url: str = ""
    device_token: str = ""
    # How long a hook will hold the session waiting for a reply. Past this
    # the hook releases and the late-reply path takes over.
    wait_seconds: int = 300
    # How long a "yes to everything" reply lasts, in seconds.
    blanket_approval_seconds: int = 1800
    # One long-poll leg. Catalyst caps a function at 30s, so stay under it.
    poll_seconds: int = 25
    enabled: bool = True

    @property
    def paired(self) -> bool:
        return bool(self.relay_url and self.device_token)


def load_config() -> Config:
    data = {}
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"config unreadable: {exc}")
    cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    # Environment wins, so a teammate can point at a dev relay without
    # editing the file.
    cfg.relay_url = os.environ.get("CLAUDE_CLIQ_RELAY_URL", cfg.relay_url).rstrip("/")
    cfg.device_token = os.environ.get("CLAUDE_CLIQ_TOKEN", cfg.device_token)
    if os.environ.get("CLAUDE_CLIQ_DISABLE"):
        cfg.enabled = False
    return cfg


def save_config(cfg: Config) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def grant_blanket_approval(session_id: str, seconds: int) -> float:
    """Approve everything in one session for a bounded window.

    Answering six permission prompts from a phone is miserable, so a reply of
    "all" trades a little safety for a lot of usability. Deliberately
    bounded: one session, a time limit, and cleared the moment away mode
    goes off.
    """
    APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    until = time.time() + seconds
    (APPROVALS_DIR / f"{session_id}.json").write_text(
        json.dumps({"until": until, "granted": time.time()}))
    return until


def blanket_approval_until(session_id: str) -> float | None:
    """Remaining blanket approval for this session, if any."""
    p = APPROVALS_DIR / f"{session_id}.json"
    try:
        until = float(json.loads(p.read_text())["until"])
    except Exception:
        return None
    if until < time.time():
        p.unlink(missing_ok=True)
        return None
    return until


def clear_blanket_approvals() -> int:
    n = 0
    if APPROVALS_DIR.exists():
        for f in APPROVALS_DIR.glob("*.json"):
            f.unlink(missing_ok=True)
            n += 1
    return n


def is_away() -> bool:
    """Away mode is opt-in blocking. Disarmed, nothing ever holds a session."""
    return AWAY_PATH.exists()


def set_away(on: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if on:
        AWAY_PATH.write_text(str(int(time.time())))
    else:
        AWAY_PATH.unlink(missing_ok=True)
        # Coming back to the keyboard revokes any blanket approval: it was
        # a concession to being away, not a standing permission.
        clear_blanket_approvals()


class RelayError(Exception):
    pass


class RateLimited(RelayError):
    pass


class Relay:
    """Thin client for the Catalyst relay.

    The laptop holds no Cliq credential -- only this device token. The relay
    resolves which Cliq user owns the token server-side, which is why no
    method here ever names a recipient.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _request(self, route: str, payload: dict | None, timeout: int,
                 method: str = "POST") -> dict:
        url = f"{self.cfg.relay_url}{route}"
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        if self.cfg.device_token:
            # Not Authorization: Catalyst's gateway claims that header and
            # rejects anything that is not one of its own OAuth tokens.
            req.add_header("X-Device-Token", self.cfg.device_token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode() or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:400]
            except Exception:
                pass
            if exc.code == 429:
                raise RateLimited(detail or "rate limited") from exc
            raise RelayError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RelayError(f"unreachable: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise RelayError(f"bad JSON from relay: {exc}") from exc

    # -- pairing ---------------------------------------------------------
    def pair(self, code: str, device_name: str) -> dict:
        return self._request("/pair", {"code": code, "device_name": device_name},
                             timeout=20)

    def unpair(self) -> dict:
        return self._request("/unpair", {}, timeout=20)

    # -- outbound --------------------------------------------------------
    def notify(self, *, kind: str, text: str, request_id: str,
               session_id: str = "", cwd: str = "", expects_reply: bool = False,
               hint: str = "") -> dict:
        """Send a message to the paired user's Cliq DM.

        Note there is no recipient argument: the relay derives it from the
        device token, so a laptop cannot address someone else's DM.
        """
        return self._request("/notify", {
            "kind": kind,
            "text": text,
            "request_id": request_id,
            "session_id": session_id,
            "cwd": cwd,
            "expects_reply": expects_reply,
            "hint": hint,
        }, timeout=20)

    # -- inbound ---------------------------------------------------------
    def poll(self, since: float, wait: int | None = None) -> dict:
        """One long-poll leg. Returns {'messages': [...], 'now': <epoch>}."""
        wait = self.cfg.poll_seconds if wait is None else wait
        # Give the socket a little more than the server's own hold time so a
        # clean empty response beats a client-side abort.
        return self._request("/await", {"since": since, "wait": wait},
                             timeout=wait + 10)


def backoff_sleep(attempt: int, base: float = 1.0, cap: float = 30.0) -> None:
    """Jittered exponential backoff. A 429 from Cliq costs a 10-minute
    lockout, so retries must never be tight."""
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay * (0.5 + random.random() / 2))
