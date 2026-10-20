---
name: away
description: Turn away mode on or off. Away mode makes Claude wait for a Zoho Cliq reply before finishing a turn or acting on a permission request. Use when the user runs /claude-cliq:away or says they're stepping away from the keyboard.
---

# Away mode

Away mode is the switch between "notify me" and "wait for me".

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away on
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away off
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away
```

There are two states and no third.

**Off** (the default): nothing happens at all. No Cliq messages, no waiting,
no relay calls. The hooks still fire -- a plugin's hooks cannot be
unregistered at runtime -- but they return immediately, so the terminal
behaves exactly as it would without this plugin installed.

**On**: when Claude finishes a turn or hits a permission request, it posts to
Cliq and then waits for a reply before continuing. Permission requests that go
unanswered fall back to the normal terminal prompt rather than being denied.

So "off" means silent, not "notify without waiting". Someone who turns it off
has said they do not want to be interrupted, and a ping is an interruption.

It waits **30 minutes at a time**, and that clock restarts on every reply, so
a long back-and-forth never runs out of window — only a long silence does.
After **100 replies in a row with no tool call between them**, the bot says so
and stops waiting.

Past either limit the session is released. A released interactive session
cannot be fed from outside, so a later reply gets told where the work is and
the user picks it up at the terminal. Say that plainly if they ask; there is no
resume-from-Cliq path.

Turning away mode **off** also revokes any blanket `all` approvals the user
granted from Cliq — those were a concession to being away, not standing
permission. Mention that if they ask about revoking one.

Mention that away mode stays on until they turn it off.
