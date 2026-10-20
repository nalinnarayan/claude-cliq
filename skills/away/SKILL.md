---
name: away
description: Turn away mode on or off. Away mode makes Claude wait for a Zoho Cliq reply before finishing a turn or acting on a permission request. Use when the user runs /claude-cliq:away or says they're stepping away from the keyboard.
---

# Away mode

Away mode is the switch between "notify me" and "wait for me".

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away on
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away notify
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away off
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cliqctl.py" away
```

Three states, because "tell me what happened" and "hold the session until I
answer" are different wants.

**Off** (the default): nothing at all. No Cliq messages, no waiting, no relay
calls. The hooks still fire -- a plugin's hooks cannot be unregistered at
runtime -- but they return immediately, so the terminal behaves exactly as it
would without this plugin installed.

**Notify**: Cliq hears about it, the terminal never waits. Good for a long
build you want to know the end of while staying at your desk. Replying in Cliq
will NOT reach the session -- say so if they try, and point them at `on`.

**On**: Claude posts to Cliq and then waits for a reply before continuing, for
a finished turn and for a permission request alike. Permissions that go
unanswered fall back to the normal terminal prompt rather than being denied.

Only `on` ever holds a session. Only `on` accepts replies.

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
