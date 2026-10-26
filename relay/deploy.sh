#!/usr/bin/env bash
# Deploy the relay, injecting secrets from an untracked env file.
#
#   ./deploy.sh                  -> .env.deploy
#   ./deploy.sh <suffix>         -> .env.deploy.<suffix>
#
# There is only one environment now: the project that started as the test
# account became the live one. `.env.deploy.test` survived that change holding
# the retired bot's name and a DIFFERENT inbound secret, and deploying it
# overwrote the live function's config -- same project, older secrets, so the
# doorbell and the ask forms stopped working and nothing said why. Hence the
# completeness check below.
#
# Catalyst reads env vars out of catalyst-config.json, which is committed, so
# secrets are merged in just before the deploy and stripped straight after.
# The trap means an interrupted deploy still leaves the file clean.
#
# Which PROJECT this lands in comes from `catalyst project:use`, not from here
# -- so switch the project first, or the test secrets go to production. The
# banner below prints the active project for exactly that reason.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG=functions/relay/catalyst-config.json
SUFFIX="${1:-}"
if [ -z "$SUFFIX" ]; then
  # No default, deliberately. There used to be one -- the unsuffixed
  # .env.deploy was whichever environment happened to be current -- and that
  # ambiguity is how a stale .env.deploy.test was deployed over production,
  # same project, older secrets, with nothing to say why the doorbell stopped.
  # Naming the environment costs one word and removes the whole class.
  echo "usage: ./deploy.sh <env>    e.g. prod | test" >&2
  echo "environments available here:" >&2
  ls -1 .env.deploy.* 2>/dev/null | sed 's/^\.env\.deploy\./  /' \
    | grep -v '^  example$' >&2
  exit 1
fi
ENVFILE=".env.deploy.$SUFFIX"

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE (see .env.deploy.example)"; exit 1; }

# Catalyst REPLACES env_variables wholesale, so a file missing a key silently
# unsets it in the deployed function. Every variable the code reads is listed
# here; anything absent aborts the deploy rather than quietly breaking a
# feature that only shows up as "I replied and nothing happened".
REQUIRED="CLIQ_BASE_URL CLIQ_BOT_NAME CLIQ_ZAPIKEY RELAY_INBOUND_SECRET \
RELAY_STREAM_SECRET DS_CHANNEL_ID RELAY_TLD CLIQ_FUNCTION_NAME \
CLIQ_ASK_FUNCTION_NAME CLIQ_ASK_SUBMIT_FUNCTION_NAME"
MISSING=""
for key in $REQUIRED; do
  grep -qE "^${key}=" "$ENVFILE" || MISSING="$MISSING $key"
done
if [ -n "$MISSING" ]; then
  echo "refusing to deploy: $ENVFILE is missing$MISSING" >&2
  echo "Catalyst replaces the whole env, so these would be UNSET in the" >&2
  echo "deployed function. Add them, or deploy a complete env file." >&2
  exit 1
fi

# Deploying test secrets into the production project is the one mistake here
# that is both easy and expensive, so say where this is going.
ACTIVE=$(python3 - <<'RC'
import json, pathlib
try:
    rc = json.loads(pathlib.Path(".catalystrc").read_text())
    pr = rc["projects"][0]
    print(f'{pr["name"]} ({pr["id"]})')
except Exception:
    print("unknown — run `catalyst project:use`")
RC
)
echo "env file : $ENVFILE"
echo "project  : $ACTIVE"

cp "$CONFIG" "$CONFIG.bak"
restore() { mv "$CONFIG.bak" "$CONFIG"; }
trap restore EXIT

python3 - "$CONFIG" "$ENVFILE" <<'PY'
import json, sys
cfg_path, env_path = sys.argv[1], sys.argv[2]
cfg = json.load(open(cfg_path))
env = {}
for line in open(env_path):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
cfg["deployment"]["env_variables"] = env
json.dump(cfg, open(cfg_path, "w"), indent=2)
print(f"injected {len(env)} env vars: {', '.join(sorted(env))}")
PY

# A token, when one is saved for this environment, so deploying does not
# depend on which account the CLI happens to be logged into. The CLI holds one
# session at a time and the two environments live in different accounts, so
# without this every switch is a browser login -- and a deploy aimed at the
# wrong account is the expensive mistake here.
TOKEN_FILE=".catalyst-token${SUFFIX:+.$SUFFIX}"
if [ -f "$TOKEN_FILE" ]; then
  echo "auth     : token from $TOKEN_FILE"
  catalyst deploy --only functions --token "$(cat "$TOKEN_FILE")"
else
  echo "auth     : the CLI's current login ($(catalyst whoami 2>/dev/null | tail -1 | sed 's/.*: //'))"
  catalyst deploy --only functions
fi
