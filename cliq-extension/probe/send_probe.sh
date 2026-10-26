#!/usr/bin/env bash
# PROBE -- throwaway. Posts the gate-test message straight to Cliq v3,
# bypassing the relay so no deploy is needed and core.js stays untouched.
#
#   bash cliq-extension/probe/send_probe.sh form    # the form button + 3KB argument
#   bash cliq-extension/probe/send_probe.sh buttons # six buttons, to probe the cap
set -euo pipefail
cd "$(dirname "$0")/../.."

set -a; . "relay/.env.deploy.${CLIQ_ENV:-test}"; set +a   # secrets stay in the environment
BASE="${CLIQ_BASE_URL:-https://cliq.zoho.com}"
WHO="${PROBE_USER:-nalin.narayan@zohocorp.com}"
URL="$BASE/api/v3/bots/$CLIQ_BOT_NAME/messages?zapikey=$CLIQ_ZAPIKEY&user_ids=$WHO"

case "${1:-form}" in
form)
  # 280 characters: just under the measured cap. `arguments` is limited to
  # 300 in total, which a four-question form spec (~1.2KB) cannot fit -- so
  # the spec cannot ride in the button and something must serve it at tap
  # time. Measured by sending 3000 and getting input_maxlength_reached.
  FILLER=$(python3 -c "print('x' * 240)")
  BODY=$(FILLER="$FILLER" OWNER="${CLIQ_FUNCTION_OWNER:-}" python3 - <<'PY'
import json, os
btn = {"label": "Answer",
       "action": {"type": "invoke.function",
                  "data": {"name": "claudeCliqProbeForm",
                           **({"owner": os.environ["OWNER"]} if os.environ.get("OWNER") else {})}},
       "arguments": {"spec": os.environ["FILLER"]}}
print(json.dumps({"text": "*Gate test* — tap Answer. A form should open.\n\n"
                          "The radio's hint line reports how much of a 3000-character "
                          "argument survived; anything less than 3000 means the real "
                          "feature needs server-side storage after all.",
                  "buttons": [btn]}))
PY
)
  ;;
buttons)
  # Our code asserts a five-button maximum in three places and has never
  # probed it. Six buttons, no form, nothing else to confound the result.
  BODY=$(OWNER="${CLIQ_FUNCTION_OWNER:-}" python3 - <<'PY'
import json, os
owner = {"owner": os.environ["OWNER"]} if os.environ.get("OWNER") else {}
btns = [{"label": f"Button {i}",
         "action": {"type": "invoke.function",
                    "data": {"name": "claudeCliqProbeForm", **owner}},
         "arguments": {"spec": str(i)}} for i in range(1, 7)]
print(json.dumps({"text": "*Button cap probe* — six buttons. If this message "
                          "arrives at all, five was not the ceiling. Do not tap.",
                  "buttons": btns}))
PY
)
  ;;
*) echo "usage: send_probe.sh [form|buttons]" >&2; exit 2 ;;
esac

printf 'posting %s probe to %s as bot %s\n' "${1:-form}" "$WHO" "$CLIQ_BOT_NAME"
curl -sS -X POST "$URL" -H 'Content-Type: application/json' -d "$BODY" \
  -w '\nHTTP %{http_code}\n'
