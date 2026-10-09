---
name: pair
description: Pair this machine with your Zoho Cliq account using the code the Claude Code bot DM'd you. Use when the user runs /claude-cliq:pair, says they want to connect Cliq, or pastes a pairing code.
---

# Pair with Zoho Cliq

The user should already have subscribed to the **Claude Code** bot in Zoho Cliq
and received a pairing code in that DM.

Run, substituting the code they gave you:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" pair <code>
```

If they did not supply a code, ask for it — do not invent one. If they don't
have one, tell them to subscribe to the bot at
`https://cliq.zoho.com/bots/<bot_unique_name>` and the code arrives by DM.

After pairing succeeds, tell them two things in one line each:

- `/claude-cliq:away on` when they step away — Claude then waits for Cliq replies.
- Replies work as plain text for questions, and `yes <id>` / `no <id>` for
  permission requests.

To check state or disconnect:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" status
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" test
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" unpair
```
