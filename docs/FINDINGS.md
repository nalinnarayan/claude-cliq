# Measured behaviour

Everything here was tested on this machine against Claude Code 2.1.292, not
read from docs. The probe harness is in `probe/`.

## Hook timeouts are enforced, and are not capped at 600s

| timeout | sleep | outcome |
| --- | --- | --- |
| 20s | 30s | killed at 20s — the `end` marker never wrote |
| 700s | 640s | ran the full 640s and completed |
| 3600s | 950s | ran the full 950s and completed |

No hidden ceiling: the configured value is simply honoured. Hooks are
registered at 2100s, giving the 30-minute wait window five minutes of
headroom.

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

## A permission answered at the terminal strands its Cliq waiter

Two premises turned out to be wrong, and together they made replies stop
landing.

**"One waiter per session."** False. Claude Code issues tool calls in
parallel, and each one needing permission runs its own `PermissionRequest`
hook concurrently. Two waiters armed from a single session were observed
directly, both with live processes.

**"The hook decides the permission."** Also false. While the hook waits on
Cliq, the terminal prompt is shown too, and they race. Answering in the
terminal lets the tool run — but nothing tells the waiting hook, so it holds
for its entire window. Two were found stranded at 12 and 10 minutes.

While *any* waiter is armed, a bare reply is ambiguous and gets bounced with
"Which one?", so one stranded permission silently breaks every later reply.
Raising the wait window to 30 minutes made this six times worse.

Fixed by letting the tool call cancel its own waiter:

- `arm()` records `tool_use_id`
- `on_settled.py` runs on `PostToolUse` and `PostToolUseFailure` and calls
  `disarm_for_tool()` — either outcome means the question was settled
- `wait()` treats a vanished waiter file as cancellation and returns at once
  instead of waiting out the window
- permissions get their own shorter window (`permission_wait_seconds`, 300s),
  since they are answered in seconds when answered at all

## v3 renders Markdown; v2 does not

Not a setting — a difference between endpoints, found by probing the live API.
The published spec documents `styles: {markdown: true}`, but the live v3 bot
endpoint **rejects `styles` outright** as an unknown key. It renders bold,
italic, code, fences, lists, headings and links in `text` anyway; v2 shows the
same text raw, which is why every message looked like stray asterisks.

Other v3 differences, each learned from a 400 rather than the docs:

| | v2 | v3 |
| --- | --- | --- |
| `user_ids` | body field (`userids`) | **query parameter** |
| `sync_message` | body field, returns `message_id` | **not accepted** |
| `styles` | — | **rejected**, despite the spec |
| `slides` / `buttons` / `card` | — | accepted |
| Markdown in `text` | raw | rendered |

Unknown body keys are a hard 400 on v3, so the client sends only an allow-list.
Losing `sync_message` costs the sent `message_id`; the buttons design does not
need it, since binding comes from `arguments.req`.

**Tables are the exception.** v3 renders every other Markdown construct in
`text`, but a Markdown table still arrives as raw pipes. They have to be lifted
out and sent as native table slides, which is what `format.js` does.

## A reply sent while Claude is working had nowhere to go

Waiters are armed only while a hook is blocking — that is, while Claude is
*waiting*. Reply while it is mid-task and nothing is armed, so the daemon fell
through to the late-reply path, found the session alive, and told the user to
use the terminal. The reply was dropped.

That is the wrong reading of "alive but nothing armed". It does not mean the
session has gone idle; it usually means Claude is busy and simply has not
paused yet.

The daemon now **holds** such a reply (10 minutes) and delivers it to the first
waiter that arms, which is exactly when Claude next pauses. The user is told
"Claude's still working — I'll pass that on the moment it pauses" rather than
being sent away, and gets a confirmation when it lands.

The hold is deliberately short: long enough to cover a few tool calls, short
enough that an old reply cannot resurface against some unrelated later
question.

## `message_details` carries everything the bare variables do not

The handler attribute I never tested, and it overturns two earlier
conclusions. `message`, `chat`, `mentions` and `attachments` are the obvious
variables; `message_details` is where the useful structure lives.

```
message_details.message.text              typed text
message_details.message.comment           caption, when a file is attached
message_details.message.replied_message   the message a swipe-reply answers
message_details.message.file              {name, id, type, url}
```

**Swipe-reply does bind.** Earlier this was tested by dumping `chat`,
`mentions` and `attachments` and concluding the reply reference did not exist.
It does — `replied_message` carries the answered message's `id` *and its full
text*. Since outbound messages already end with "(id xxxxx)", the id comes back
with the reply and nothing has to be typed or stored.

**An attachment puts its caption in `comment`, not `message`.** The bare
`message` variable is empty for an attachment, which is why an image with a
caption silently failed: the relay rejected it for missing text.

**Attachment URLs expire.** `file.url` carries `?type=transient` and returns
`attachment_access_time_expired` within minutes. `attachments` also holds the
Deluge *file object*, which `invokeurl` can upload directly with `files:` —
the durable route if images are ever forwarded on.
