# Measured behaviour

Everything here was tested on this machine against Claude Code 2.1.292, not
read from docs. The probe harness is in `probe/`.

## Hook timeouts are enforced, and are not capped at 600s

| timeout | sleep | outcome |
| --- | --- | --- |
| 20s | 30s | killed at 20s — the `end` marker never wrote |
| 700s | 640s | ran the full 640s and completed |

So a blocking hook can hold a session well past the 600s default. The plugin
registers `Stop` and `PermissionRequest` at 900s, with the actual wait window
set by `wait_seconds` in config (default 300s).

## Hooks fire inside `--bg` sessions

A session resumed with `claude --resume <id> --bg "<prompt>"` runs its `Stop`
hook normally. This is what closes the loop: a late reply resumes the session
in the background, and that session pings Cliq again when it finishes.

## `--resume --bg` continues the same session with full context

Seeded a session with a fact, resumed it in the background asking for the fact
back, and got the right answer — same session id, same transcript.

```
user      Remember: the project codename is BLUEJAY. Reply with just: ok
assistant ok
user      what was the codename again?
assistant BLUEJAY
```

## `--bg` requires a trusted workspace

Two refusals worth knowing:

```
Workspace not trusted. Run `claude` in <dir> once and accept the trust prompt, then retry.
Workspace not trusted. The home directory is trusted one session at a time —
start this from an interactive terminal there, or from a project directory.
```

The home directory never qualifies, no matter what. The daemon detects this and
says what fixes it rather than reporting a raw error.

## Stop hook payload

`last_assistant_message` arrives directly in stdin — no transcript parsing
needed. `session_id` is the same id `--resume` takes.

## Still unverified

Needs the real bot to answer: **can the bot DM a user who has not subscribed?**
Cliq's v2 docs describe `userids` as targeting *subscribers*, and v3 defines
`bot_unauthorized_message_access`. Users subscribe as part of onboarding, so
this is probably moot — but pairing would break if a code were ever sent to a
non-subscriber.

## `stop_hook_active` must not suppress the notification

It goes true on the Stop immediately after the hook returned
`{"decision":"block"}`, when Claude answered with no tool call in between.
It exists to prevent an infinite block loop.

Guarding the whole hook with it is wrong, and the bug is quiet: the first
answer reaches Cliq, and then every answer to a follow-up asked *from* Cliq
is swallowed. The session looks like it stopped responding.

The two concerns are separate:

| `stop_hook_active` | away | notifies | blocks |
| --- | --- | --- | --- |
| false | off | yes | no |
| false | on | yes | **yes** |
| true | off | yes | no |
| true | on | yes | no |

Notify always; block only when away mode is on *and* we did not just block.
A reply to the released message still works — it takes the late-reply path
and resumes the session in the background.
