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
ENVFILE = HERE.parent / "relay" / ".env.deploy"
RCFILE = HERE.parent / "relay" / ".catalystrc"
OUT = HERE / "generated"


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
    # The CLI records e.g. <project>-<org-id>.development; the
    # data-centre suffix follows from where the project lives.
    tld = "in" if domain.endswith(".development") and "zoho" not in domain else "in"
    return f"https://{domain}.catalystserverless.{tld}/server/relay"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="<your-marketplace-repo>",
                    help="what teammates pass to `claude plugin marketplace add`")
    ap.add_argument("--relay", default=None, help="override the relay base URL")
    args = ap.parse_args()

    env = read_env()
    secret = env.get("RELAY_INBOUND_SECRET", "")
    if not secret:
        print("RELAY_INBOUND_SECRET is empty in relay/.env.deploy", file=sys.stderr)
        return 1
    base = args.relay or relay_base()

    OUT.mkdir(exist_ok=True)
    (OUT / ".gitignore").write_text("*\n")

    for name in ("welcome_handler.dg", "message_handler.dg"):
        text = (HERE / name).read_text()
        text = (text.replace("REPLACE_WITH_RELAY_INBOUND_SECRET", secret)
                    .replace("REPLACE_WITH_RELAY_BASE", base)
                    .replace("REPLACE_WITH_REPO", args.repo))
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
