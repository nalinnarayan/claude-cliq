#!/usr/bin/env python3
"""Fill the Deluge handler templates with real values, ready to paste.

Output goes to cliq-extension/generated/, which is untracked -- the handlers
carry RELAY_INBOUND_SECRET and must never be committed.

    python3 render.py [--repo <marketplace-url>]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ENVFILE = None            # set from --env; there is deliberately no default
RCFILE = HERE.parent / "relay" / ".catalystrc"
OUT = None                # HERE/"generated"/<env>; set from --env

# Per environment, because two of the five files have the SAME name in both
# (message_handler, welcome_handler) -- so a single directory means the last
# render silently overwrites the other environment's, and the directory ends
# up holding two environments' secrets with nothing to say which is which.
# Pasting the wrong one points a bot at the wrong account.


def read_env() -> dict:
    env = {}
    for line in ENVFILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def relay_base() -> str:
    rc = json.loads(RCFILE.read_text())
    domain = rc["projects"][0]["domain"]["name"]
    # The CLI records e.g. <project>-<org-id>.development. The
    # data-centre suffix does NOT follow from the domain -- both branches of
    # the ternary that used to live here returned "in", so a project outside
    # the India DC rendered a wrong URL silently and the handlers failed with
    # no clue why. There is nothing in .catalystrc that names the DC, so it
    # comes from the env file, defaulting to the DC this project has used
    # since the start.
    tld = read_env().get("RELAY_TLD", "in")
    return f"https://{domain}.catalystserverless.{tld}/server/relay"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="<your-marketplace-repo>",
                    help="what teammates pass to `claude plugin marketplace add`")
    ap.add_argument("--relay", default=None, help="override the relay base URL")
    ap.add_argument("--env", required=True,
                    help="which environment: prod | test "
                         "(reads relay/.env.deploy.<env>)")
    args = ap.parse_args()

    # Bound before anything reads it: relay_base() also pulls RELAY_TLD out of
    # the same file, so both must come from the same environment. Required
    # rather than defaulted, because rendering with the wrong one bakes
    # another account's relay URL and secret into a bot's handlers -- the bot
    # would then talk to the wrong environment and look merely broken.
    global ENVFILE, OUT
    ENVFILE = HERE.parent / "relay" / f".env.deploy.{args.env}"
    OUT = HERE / "generated" / args.env
    if not ENVFILE.exists():
        print(f"no such env file: {ENVFILE}", file=sys.stderr)
        return 1

    env = read_env()
    secret = env.get("RELAY_INBOUND_SECRET", "")
    if not secret:
        print(f"RELAY_INBOUND_SECRET is empty in {ENVFILE}", file=sys.stderr)
        return 1
    base = args.relay or relay_base()

    OUT.mkdir(parents=True, exist_ok=True)   # generated/ itself may not exist
    (OUT / ".gitignore").write_text("*\n")

    fn_name = env.get("CLIQ_FUNCTION_NAME", "claudeCliqAction")
    ask_fn = env.get("CLIQ_ASK_FUNCTION_NAME", "claudeCliqAskForm")
    ask_submit_fn = env.get("CLIQ_ASK_SUBMIT_FUNCTION_NAME", "claudeCliqAskSubmit")
    sources = sorted(f.name for f in HERE.glob("*.dg") if "debug" not in f.name)
    for name in sources:
        text = (HERE / name).read_text()
        text = (text.replace("REPLACE_WITH_RELAY_INBOUND_SECRET", secret)
                    .replace("REPLACE_WITH_RELAY_BASE", base)
                    .replace("REPLACE_WITH_FUNCTION_NAME", fn_name)
                    .replace("REPLACE_WITH_ASK_FUNCTION_NAME", ask_fn)
                    .replace("REPLACE_WITH_ASK_SUBMIT_FUNCTION_NAME", ask_submit_fn)
                    .replace("REPLACE_WITH_REPO", args.repo))
        # The bot function is written out under the name it must be CREATED
        # with, so there is no way to paste it into the wrong one.
        if name == "claudeCliqAction.dg":
            name = f"{fn_name}.dg"
        elif name == "claudeCliqAskForm.dg":
            name = f"{ask_fn}.dg"
        elif name == "claudeCliqAskSubmit.dg":
            name = f"{ask_submit_fn}.dg"
        if "REPLACE_WITH_" in text:
            leftover = [l.strip() for l in text.splitlines() if "REPLACE_WITH_" in l]
            print(f"! {name} still has placeholders: {leftover}", file=sys.stderr)
        (OUT / name).write_text(text)
        print(f"wrote generated/{name}")

    print(f"\nrelay base : {base}")
    print(f"secret     : {secret[:8]}… ({len(secret)} chars)")
    print("\nPaste each file into its handler in the Cliq bot builder.")
    print("generated/ is untracked -- these files carry the secret.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
