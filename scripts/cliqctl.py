#!/usr/bin/env python3
"""User-facing control: pair, unpair, away, status, test.

    cliqctl.py pair <code> [--relay URL]
    cliqctl.py away on|off
    cliqctl.py status
    cliqctl.py test
"""

from __future__ import annotations

import argparse
import os
import time
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import waiter
from cliqlib import (CONFIG_PATH, MODES, Relay, RelayError, ensure_block_cap,
                     load_config, mode, new_request_id, save_config, set_mode)

# Where a teammate pairs if they pass no --relay. This is the live relay, not
# a placeholder: getting it wrong means pairing succeeds against nothing.
DEFAULT_RELAY = ("https://<project-test>-<org-id-test>.development"
                 ".catalystserverless.in/server/relay")


def cmd_pair(args) -> int:
    cfg = load_config()
    cfg.relay_url = (args.relay or cfg.relay_url or DEFAULT_RELAY).rstrip("/")
    cfg.device_token = ""                       # a fresh pairing replaces any old one
    relay = Relay(cfg)
    try:
        res = relay.pair(args.code.strip(), socket.gethostname())
    except RelayError as exc:
        print(f"Pairing failed: {exc}")
        print("Check the code hasn't expired — ask the bot for a new one.")
        return 1
    token = res.get("device_token")
    if not token:
        print(f"Relay did not return a device token: {res}")
        return 1
    cfg.device_token = token
    cfg.enabled = True
    save_config(cfg)
    print(f"Paired with {res.get('cliq_user', 'your Cliq account')}.")
    print(f"Token stored in {CONFIG_PATH} (0600).")
    note = ensure_block_cap()
    if note:
        print(note)
    print("Run `/claude-cliq:away on` when you step away and Claude will wait for "
          "your replies in Cliq.")
    return 0


def cmd_unpair(args) -> int:
    cfg = load_config()
    if cfg.paired:
        try:
            Relay(cfg).unpair()
        except RelayError as exc:
            print(f"(relay said: {exc})")
    cfg.device_token = ""
    save_config(cfg)
    print("Unpaired. This machine can no longer send or receive Cliq messages.")
    return 0


AWAY_HELP = {
    "off": "nothing at all — no Cliq messages, no waiting",
    "notify": "messages only — Cliq hears about it, the terminal never waits",
    "on": "messages, and the session waits for your reply",
}


def cmd_away(args) -> int:
    if args.state is None:
        current = mode()
        print(f"away: {current}  ({AWAY_HELP[current]})")
        return 0
    wanted = args.state.lower()
    aliases = {"yes": "on", "true": "on", "1": "on",
               "no": "off", "false": "off", "0": "off",
               "notify-only": "notify", "quiet": "notify"}
    wanted = aliases.get(wanted, wanted)
    if wanted not in MODES:
        print(f"Unknown mode {args.state!r}. Pick one of: "
              + ", ".join(f"{m} ({AWAY_HELP[m]})" for m in MODES))
        return 2
    set_mode(wanted)
    on = wanted == "on"
    if wanted == "notify":
        print("Notify-only — Claude will tell you what happened in Cliq and "
              "never hold the terminal.")
        print("\nReplying there won't reach the session. Use `away on` for that.")
    elif on:
        print("Away mode on — Claude will ping you in Cliq and wait for a "
              "reply before finishing.")
        # Said up front, because the alternative is learning it from a reply
        # that goes nowhere.
        print(f"\nIt waits {load_config().wait_seconds // 60} minutes at a "
              f"time, restarting that clock on every reply. Past it the "
              f"session is released and you carry on at the terminal.")
    else:
        print("Away mode off — no Cliq messages and no waiting until you turn "
              "it back on.")
    return 0


def cfg_state_dir() -> str:
    from cliqlib import STATE_DIR
    return str(STATE_DIR)


def cmd_status(args) -> int:
    cfg = load_config()
    print(f"relay      : {cfg.relay_url or '(unset)'}")
    print(f"paired     : {'yes' if cfg.paired else 'no'}")
    print(f"enabled    : {cfg.enabled}")
    print(f"away mode  : {mode()}  ({AWAY_HELP[mode()]})")
    print(f"transport  : {cfg.transport}" +
          ("  (doorbell; ~1 read per reply)" if cfg.transport == "stream"
           else f"  (polling every {cfg.poll_fallback_seconds:.0f}s)"))
    print(f"wait window: {cfg.wait_seconds}s")
    # What actually served the last reply, which is the question worth asking:
    # the poll fallback works, so a broken stream reads as a healthy one.
    try:
        import json as _json
        from cliqlib import STATE_DIR as _SD
        d = _json.loads((_SD / "served.json").read_text())
        mins = int((time.time() - float(d["ts"])) // 60)
        print(f"last served: {d['mode']} ({d.get('count', '?')} message(s), {mins}m ago)")
    except Exception:
        print("last served: nothing collected yet")
    if (__import__("pathlib").Path(cfg_state_dir()) / "stream-down").exists():
        print("stream     : in cooldown after a failure — replies are polling")
    print(f"max replies: {cfg.max_block_streak} in a row without a tool call")
    cap = os.environ.get("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP")
    print(f"block cap  : {cap or 'default (8) — run pair again, then restart'}")
    armed = waiter.active()
    print(f"waiting now: {len(armed)}" + (
        " — " + ", ".join(w["request_id"] for w in armed) if armed else ""))
    return 0


def cmd_test(args) -> int:
    cfg = load_config()
    if not cfg.paired:
        print("Not paired yet. Run `/claude-cliq:pair <code>` first.")
        return 1
    rid = new_request_id()
    try:
        Relay(cfg).notify(kind="test", text="🔔 Test ping from Claude Code.",
                          request_id=rid, hint="if you can read this, the "
                                               "bridge works")
    except RelayError as exc:
        print(f"Failed: {exc}")
        return 1
    print(f"Sent (id {rid}). Check your Cliq DM from the Claude Code bot.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="cliqctl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair"); p.add_argument("code"); p.add_argument("--relay")
    p.set_defaults(fn=cmd_pair)
    sub.add_parser("unpair").set_defaults(fn=cmd_unpair)
    p = sub.add_parser("away"); p.add_argument("state", nargs="?")
    p.set_defaults(fn=cmd_away)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("test").set_defaults(fn=cmd_test)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
