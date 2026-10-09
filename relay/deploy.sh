#!/usr/bin/env bash
# Deploy the relay, injecting secrets from an untracked .env.deploy.
#
# Catalyst reads env vars out of catalyst-config.json, which is committed, so
# secrets are merged in just before the deploy and stripped straight after.
# The trap means an interrupted deploy still leaves the file clean.
set -euo pipefail
cd "$(dirname "$0")"

CONFIG=functions/relay/catalyst-config.json
ENVFILE=.env.deploy

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE (see .env.deploy.example)"; exit 1; }

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
