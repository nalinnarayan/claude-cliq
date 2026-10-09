#!/bin/bash
# Stop-hook probe: records when it starts, sleeps, records if it survived.
# If Claude Code kills the hook at a timeout ceiling, the "end" line never appears.
LOG="$1"; SLEEP="$2"
cat > /dev/null                      # drain stdin (hook payload)
echo "$(date +%s) start sleep=${SLEEP}" >> "$LOG"
sleep "$SLEEP"
echo "$(date +%s) end   sleep=${SLEEP}" >> "$LOG"
exit 0
