---
name: pair
description: Pair this machine with your Zoho Cliq account using the code the Claude Code bot DM'd you. Use when the user runs /claude-cliq:pair, says they want to connect Cliq, or pastes a pairing code.
---

# Pair with Zoho Cliq

The user should already have subscribed to the **Claude Code** bot in Zoho Cliq
and received a pairing code in that DM.

The bot's message gives both a code and a relay URL. Run, substituting both:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" pair <code> --relay <relay-url>
```

The URL is only needed the first time; later pairings on the same machine
reuse the stored one, so `pair <code>` alone is enough if they have paired
before.

If they did not supply a code, ask for it — do not invent one. If they don't
have one, tell them to subscribe to the bot at
`https://cliq.zoho.com/bots/<bot_unique_name>` and the code arrives by DM.
If they have a code but no URL, ask them to paste the bot's whole message:
there is no default, and pairing cannot be guessed at.

After pairing succeeds, tell them two things in one line each:

- `/claude-cliq:away on` when they step away — Claude then waits for Cliq replies.
- Questions are answered by typing a plain reply. Permission requests are
  answered by tapping the buttons, or by typing `yes <id>` / `no <id>` with
  the five-letter id from the message — the id is what says which request is
  meant, which is why a bare "yes" is not enough when more than one is open.
  `all` approves everything in that session for a while. To answer a specific
  older message, swipe-reply to it.

To check state or disconnect:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" status
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" test
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" unpair
```
