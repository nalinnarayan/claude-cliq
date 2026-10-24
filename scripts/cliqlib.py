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
WAITERS_DIR = STATE_DIR / "waiters"
INBOX_PATH = STATE_DIR / "inbox.jsonl"
APPROVALS_DIR = STATE_DIR / "approvals"
STREAKS_DIR = STATE_DIR / "streaks"
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
    # How long a hook holds the session waiting for a reply.
    #
    # Thirty minutes: long enough to cover a meeting, which is the realistic
    # away case. Past this the session is released, and an interactive
    # terminal sitting at the prompt cannot be fed from outside at all -- so
    # a later reply is answered with "pick it up at the terminal". The clock
    # restarts on every reply, so this bounds a single gap, not the whole
    # conversation. Hook is registered at 2100s.
    wait_seconds: int = 1800
    # Permissions get a shorter hold than questions. They are answered in
    # seconds when answered at all, and a permission waiter that outlives its
    # tool call makes every later bare reply ambiguous.
    permission_wait_seconds: int = 300
    # How long a "yes to everything" reply lasts, in seconds.
    blanket_approval_seconds: int = 1800
    # Release after this many consecutive no-tool-call blocks.
    #
    # A hundred exchanges, and the user is told when they reach it. Claude
    # Code stops honouring blocks at CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (8 by
    # default) -- a guard against hooks that block unconditionally, which
    # this one cannot do since every block waits on a human. That cap is
    # raised above this number in settings.json so the limit the user hears
    # about is the one they actually hit.
    max_block_streak: int = 100
    # Waiting for the rest of a multi-attachment send.
    #
    # Pictures sent together do NOT arrive together: a phone uploads them one
    # at a time. Two sent in one go were measured 23 seconds apart, and the
    # third of three arrived 18 seconds after the first two.
    #
    # So the wait is a QUIET PERIOD after the most recent attachment, not a
    # window from the first. A fixed window released as soon as one sibling
    # turned up, which split three pictures into two and then one; measuring
    # from the last arrival instead keeps extending while uploads continue and
    # delivers once they stop. It also makes a LONE picture quicker than the
    # fixed window did.
    #
    # Costs nothing when a reply has no attachment, and no extra reads when it
    # does: the wait is spent listening on the doorbell the next picture rings.
    attachment_quiet_seconds: float = 12.0
    # A hard ceiling from the first attachment, so a slow trickle still goes
    # out rather than holding the session indefinitely.
    attachment_grace_seconds: float = 120.0
    # One long-poll leg. Catalyst caps a function at 30s, so stay under it.
    poll_seconds: int = 25
    # How a laptop learns a reply has arrived.
    #
    #   stream -- subscribe to the Data Streams doorbell and fetch when it
    #             rings. One read per reply.
    #   poll   -- ask the relay repeatedly. The same collector doing the same
    #             routing; only the trigger differs. Kept as an escape hatch
    #             against a surprise bill or a proxy that blocks wss://, and
    #             overridable by CLAUDE_CLIQ_TRANSPORT so flipping it does not
    #             itself require a working stream.
    transport: str = "stream"
    # Poll mode is a DEGRADED mode and is priced like one. A flat 20s interval
    # over a 30-minute window is 90 fetches -- 180 SELECTs -- which blows the
    # whole 10,000/month allowance at five questions a day. That is the same
    # shape as the outage this design was built to fix, so the fallback backs
    # off hard and is capped outright.
    #
    # 10s, 20s, 40s ... up to 300s, stopping after poll_fallback_budget
    # fetches: about 20 SELECTs for a full window instead of 180. The cost is
    # latency, which is the right thing to trade when the doorbell is broken.
    poll_fallback_seconds: float = 10.0
    poll_fallback_max_seconds: float = 300.0
    poll_fallback_budget: int = 20
    # Most messages this laptop will send in one minute. Cliq's real ceiling
    # is 50/min shared across the WHOLE org, which no single machine can see;
    # this only bounds a runaway local loop, which is the failure that has
    # actually happened. Enforced here because the relay-side counter cost two
    # Data Store writes per message and there is nowhere cheap to put it.
    local_sends_per_min: int = 20
    # After the stream fails, don't try it again for this long. A proxy that
    # blocks wss:// fails the same way every time, and paying a connect
    # timeout on every blocked hook is a slow, confusing way to discover it.
    stream_retry_after_seconds: int = 600
    # A reply older than this is refused rather than delivered. Without the
    # daemon a row can sit in the relay for its whole 24h TTL, and route()
    # with one armed waiter would hand a reply typed hours ago to a brand-new
    # question. Observed on the first end-to-end run.
    stale_after_seconds: int = 1800
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
    cfg.transport = os.environ.get("CLAUDE_CLIQ_TRANSPORT", cfg.transport).strip().lower()
    if cfg.transport not in ("stream", "poll"):
        log(f"unknown transport {cfg.transport!r}; falling back to poll")
        cfg.transport = "poll"
    return cfg


def save_config(cfg: Config) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def block_streak(session_id: str, *, bump: bool) -> int:
    """Consecutive Stop-blocks with no tool call in between.

    Claude Code stops honouring blocks after 8 of these. We count our own so
    we can release the session one short of that, deliberately and with an
    explanation, rather than having a block silently ignored.
    """
    STREAKS_DIR.mkdir(parents=True, exist_ok=True)
    f = STREAKS_DIR / f"{session_id}.txt"
    if not bump:
        f.unlink(missing_ok=True)
        return 0
    try:
        n = int(f.read_text().strip()) + 1
    except Exception:
        n = 1
    f.write_text(str(n))
    return n


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


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
# Claude Code stops honouring Stop-hook blocks after this many consecutive
# ones with no tool call between. The guard is aimed at hooks that block
# unconditionally; this one cannot spin, because every block waits on a human.
# Set above Config.max_block_streak so our own announced limit lands first --
# reaching Claude Code's cap instead ends the exchange with no explanation.
BLOCK_CAP = "120"


def ensure_block_cap() -> str | None:
    """Raise the Stop-hook block cap in the user's settings.

    A plugin cannot contribute env -- plugin.json carries metadata only -- so
    without this each person silently keeps the default of 8 and their
    conversations get cut off after a handful of exchanges.

    Returns a line describing what changed, or None if nothing needed doing.
    """
    key = "CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"
    try:
        data = json.loads(SETTINGS_PATH.read_text()) if SETTINGS_PATH.exists() else {}
    except Exception as exc:
        return f"couldn't read {SETTINGS_PATH}: {exc}"
    env = data.get("env") or {}
    current = str(env.get(key, "")).strip()
    try:
        if current and int(current) >= int(BLOCK_CAP):
            return None                     # already as high or higher
    except ValueError:
        pass                                # unparseable: replace it

    env[key] = BLOCK_CAP
    data["env"] = env
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(SETTINGS_PATH)
    except Exception as exc:
        return f"couldn't write {SETTINGS_PATH}: {exc}"
    was = f" (was {current})" if current else ""
    return f"Set {key}={BLOCK_CAP} in {SETTINGS_PATH}{was} — restart Claude Code for it to apply."


PACE_PATH = STATE_DIR / "pace.json"


def send_allowed(limit_per_min: int) -> bool:
    """Local send pacing. Free: one small file, no network, no Data Store.

    Deliberately NOT protection for Cliq's shared 50/min -- a laptop cannot
    see what fifty colleagues are doing. It stops one machine from emptying
    the allowance by itself, and nothing more. Fails OPEN: a pacing file that
    cannot be read must never be the reason a notification is lost.
    """
    minute = int(time.time() // 60)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(PACE_PATH.read_text())
        except Exception:
            data = {}
        used = int(data.get("n", 0)) if data.get("minute") == minute else 0
        if used >= limit_per_min:
            return False
        tmp = PACE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"minute": minute, "n": used + 1}))
        tmp.replace(PACE_PATH)
        return True
    except Exception as exc:
        log(f"pacing check failed, allowing the send: {exc}")
        return True


# Three states, because "tell me what happened" and "hold the session until I
# answer" are different wants and conflating them loses one of them.
#
#   off     nothing at all -- no messages, no waiting, no relay calls
#   notify  messages only, fire-and-forget; the terminal never waits
#   on      messages, and the session waits for your reply
MODES = ("off", "notify", "on")


def notify_only_hint(waiting_at_terminal: bool = False) -> str:
    """What to tell someone whose reply cannot reach the session.

    In notify-only mode a reply from Cliq reaches nothing: no hook is
    listening, so it sits in the relay until it expires and the person hears
    nothing back. A message they could answer and a message they cannot look
    identical, so every notify-only message has to say which it is -- and,
    more usefully, how to change it.
    """
    if waiting_at_terminal:
        return ("notify-only — it's waiting for you at the terminal, and "
                "replying here won't reach it. Run `/claude-cliq:away on` "
                "to change that.")
    return ("notify-only — replying here won't reach this session. "
            "Run `/claude-cliq:away on` at the terminal to change that.")



def mode() -> str:
    """Which of the three states is active."""
    try:
        raw = AWAY_PATH.read_text().strip().lower()
    except Exception:
        return "off"
    if raw in MODES:
        return raw
    # The file used to hold a timestamp and its mere existence meant "on".
    # Anyone upgrading mid-session keeps the behaviour they had.
    return "on"


def set_mode(new_mode: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if new_mode not in MODES:
        raise ValueError(f"unknown mode {new_mode!r}; expected one of {MODES}")
    if new_mode == "off":
        AWAY_PATH.unlink(missing_ok=True)
    else:
        AWAY_PATH.write_text(new_mode)
    if new_mode != "on":
        # A blanket approval was a concession to being away from the keyboard.
        # Anything short of waiting means you are back at it.
        clear_blanket_approvals()


def is_away() -> bool:
    """True only in the state that actually holds a session."""
    return mode() == "on"


def notifies() -> bool:
    """True whenever Cliq should hear about anything at all."""
    return mode() != "off"


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
               hint: str = "", answers: list[dict] | None = None) -> dict:
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
            # [{label, value, req?}] -- the relay turns these into buttons.
            "answers": answers or [],
        }, timeout=20)

    # -- inbound ---------------------------------------------------------
    def stream_ticket(self) -> dict:
        """What this device needs to hear its own doorbell.

        Returns {url, key, wss_id, channel_id, wake_id}. The key is a
        credential to RECEIVE broadcasts and nothing else -- it cannot read
        anybody's mailbox, including this device's.
        """
        return self._request("/stream-ticket", {}, timeout=30)

    def notify_file(self, *, name: str, data: bytes, caption: str = "",
                    content_type: str = "text/markdown") -> dict:
        """Put a document in the paired user's DM.

        For a plan: too long to read as chat text, and meant to be opened
        rather than scrolled past. No recipient argument, as everywhere else
        -- the relay resolves it from the device token.

        The file endpoint refuses `buttons` (measured), so whatever the person
        is meant to DO about the document has to follow as a normal notify().
        """
        from urllib.parse import urlencode
        qs = urlencode({"name": name, "caption": caption, "type": content_type})
        url = f"{self.cfg.relay_url}/notify-file?{qs}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", content_type)
        if self.cfg.device_token:
            req.add_header("X-Device-Token", self.cfg.device_token)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode()[:400]
            except Exception:
                pass
            raise RelayError(f"HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RelayError(f"unreachable: {exc.reason}") from exc

    # -- attachments -----------------------------------------------------
    def file_url(self, message_id: str) -> str:
        """A short-lived download URL for the file that came with a reply.

        The relay derives the object's key from THIS DEVICE's account, so a
        message id is only ever able to name something in our own namespace.
        """
        return self._request("/file-url", {"message_id": message_id},
                             timeout=20).get("url", "")

    def file_done(self, message_id: str) -> None:
        """Say the download succeeded, so the relay can drop the object."""
        self._request("/file-url", {"message_id": message_id, "done": True},
                      timeout=20)

    def poll(self, since: float, wait: int | None = None,
             timeout: int | None = None) -> dict:
        """One fetch. Returns {'messages': [...], 'now': <epoch>}.

        `wait` is how long the SERVER holds the request; `timeout` is how long
        this socket will wait for it. They are separate because a wait=0 fetch
        still needs a sane socket budget -- deriving one from the other gave a
        non-blocking fetch ten seconds, which a Catalyst cold start can
        exceed, and the hook then released the session to the terminal.
        """
        wait = self.cfg.poll_seconds if wait is None else wait
        # A little more than the server's own hold time, so a clean empty
        # response beats a client-side abort.
        return self._request("/await", {"since": since, "wait": wait},
                             timeout=timeout if timeout is not None else wait + 10)


def backoff_sleep(attempt: int, base: float = 1.0, cap: float = 30.0) -> None:
    """Jittered exponential backoff. A 429 from Cliq costs a 10-minute
    lockout, so retries must never be tight."""
    delay = min(cap, base * (2 ** attempt))
    time.sleep(delay * (0.5 + random.random() / 2))
