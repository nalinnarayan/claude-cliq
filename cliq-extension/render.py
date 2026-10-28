#!/usr/bin/env python3
"""Fill the Deluge handler templates with real values, ready to paste.

Output goes to cliq-extension/generated/, which is untracked -- the handlers
carry RELAY_INBOUND_SECRET and must never be committed.

    python3 render.py [--repo <marketplace-url>]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ENVFILE = None            # set from --env; there is deliberately no default
RCFILE = None             # set from --env; see the note in relay_base()
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
    """The relay URL for the environment being rendered.

    From `.catalystrc.<env>`, NOT the bare `.catalystrc`. The bare one is
    whichever project the CLI is currently bound to, which moves whenever
    anyone deploys -- so rendering with --env test while bound to production
    produced the PRODUCTION url carrying the TEST secret. Every handler in that
    render would have pointed the test bot at production and been refused with
    "bad secret", for no visible reason.

    The secret already followed --env. The url has to follow the same flag or
    the two describe different environments.
    """
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


def detect_repo() -> tuple[str, str]:
    """The marketplace a NEW user would add, and a warning if it looks wrong.

    Taken from the git remote, not from the local marketplace registration.
    Those differ on purpose: a maintainer registers the working tree as a
    `directory` source, so edits apply without publishing -- but a teammate
    cannot add a path on someone else's laptop. The remote is what they need.

    Returns (repo, warning). `repo` falls back to the old placeholder when
    nothing can be detected, so rendering never silently invents a source.
    """
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"],
                             cwd=HERE.parent, capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except Exception:
        url = ""
    if not url:
        return "<your-marketplace-repo>", "no git remote found -- pass --repo"

    # github.com/owner/repo(.git) or git@github.com:owner/repo(.git)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    repo = m.group(1) if m else url

    # The live blocker, worth saying at render time rather than after a
    # colleague has already subscribed and received a code.
    try:
        out = subprocess.run(["gh", "repo", "view", "--json", "isPrivate"],
                             cwd=HERE.parent, capture_output=True, text=True,
                             timeout=10).stdout
        if json.loads(out or "{}").get("isPrivate"):
            return repo, (f"{repo} is PRIVATE -- anyone without access will get a "
                          "pairing code and then fail at `marketplace add`")
    except Exception:
        pass
    return repo, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None,
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
    global ENVFILE, OUT, RCFILE
    ENVFILE = HERE.parent / "relay" / f".env.deploy.{args.env}"
    # Per environment, falling back to the active binding only when no
    # per-environment copy exists.
    RCFILE = HERE.parent / "relay" / f".catalystrc.{args.env}"
    if not RCFILE.exists():
        RCFILE = HERE.parent / "relay" / ".catalystrc"
        print(f"warning    : no .catalystrc.{args.env}; using the CLI's current "
              f"binding, which may be another environment", file=sys.stderr)
    OUT = HERE / "generated" / args.env
    if not ENVFILE.exists():
        print(f"no such env file: {ENVFILE}", file=sys.stderr)
        return 1

    detected, warning = detect_repo()
    repo = args.repo or detected
    if warning and not args.repo:
        print(f"warning    : {warning}", file=sys.stderr)

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
                    .replace("REPLACE_WITH_REPO", repo))
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
