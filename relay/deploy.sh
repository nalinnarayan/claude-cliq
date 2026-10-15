#!/usr/bin/env bash
# Deploy the relay, injecting secrets from an untracked env file.
#
#   ./deploy.sh                  -> .env.deploy        (production)
#   ./deploy.sh test             -> .env.deploy.test   (test environment)
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
ENVFILE=".env.deploy${SUFFIX:+.$SUFFIX}"

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE (see .env.deploy.example)"; exit 1; }

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

catalyst deploy --only functions
