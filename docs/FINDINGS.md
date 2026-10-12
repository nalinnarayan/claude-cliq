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

### …but it must not release the session either

The first fix over-corrected: never blocking twice in a row meant that after
answering a follow-up, the session was *released*. It then sat live and idle
at the terminal prompt, and the next Cliq reply had nowhere to go —

- no hook was waiting, so nothing could consume it;
- `--bg --resume` against a live session forks a copy rather than continuing it;
- an idle interactive session cannot be fed from outside at all.

The daemon correctly reported "that session is still open in your terminal",
but the reply was dropped and the conversation was stuck.

Blocking repeatedly is in fact safe **in this design**, because every block
requires a human reply — it cannot spin on its own. The only real ceiling is
Claude Code refusing after 8 consecutive blocks with no tool call in between.
So the hook now counts its own streak and releases at 6, deliberately and with
an explanation, instead of having a block silently ignored at 8. Any tool call
resets the counter, so a session doing real work holds indefinitely.

## Decision: replies land inside the window, or you go to the terminal

Investigated at length and deliberately **not** built. A live interactive
session cannot be fed from outside:

- `--resume --bg` forks a copy while the target is running
- stopping it would kill work in progress
- Remote Control is disabled by org policy here
- `--messaging-socket-path` is a real cross-session bus, but no CLI exposes it
- `--input-format stream-json` is real, but needs `--print`

Background sessions *could* close the gap — they can be stopped and then
resumed with no flags, and `claude attach` / Ctrl+Z move between terminal and
background. But that means starting away-work differently from ordinary work,
plus session housekeeping, to serve a minority case.

So: the wait window (10 minutes, `wait_seconds`) is the contract. Past it the
session is released and the reply goes to the terminal. The late-reply resume
still fires for sessions that genuinely ended — a closed terminal or a
finished headless run — which costs nothing extra since it is already built.
