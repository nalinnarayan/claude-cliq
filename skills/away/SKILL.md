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

**Off** (the default): every trigger still DMs Cliq, fire-and-forget. Nothing
ever blocks and the terminal behaves exactly as it would without this plugin.

**On**: when Claude finishes a turn or hits a permission request, it posts to
Cliq and then waits for a reply before continuing. Permission requests that go
unanswered fall back to the normal terminal prompt rather than being denied.

If the user replies after the wait window closes, the reply is not lost — the
daemon asks in Cliq whether to resume that session in the background.

Mention that away mode stays on until they turn it off.
