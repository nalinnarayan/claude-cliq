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
hook normally — so a background session pings Cliq exactly like a terminal
one. Measured, but no longer used: see *Resume removed* below.

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

The home directory never qualifies, no matter what. Between this, the live-vs-
idle distinction and the trust prompt, the setup surface was larger than the
feature it bought — which is why resume was dropped.

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
Nothing consumes a reply to a released message, so the bot says where the work
is instead.

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
So the hook counts its own streak and releases at `max_block_streak` (100),
deliberately and with an explanation, instead of having a block silently
ignored. `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` is set higher still (120) so the
limit the user is told about is the one they actually reach. Any tool call
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

So: the wait window (30 minutes, `wait_seconds`, restarting on every reply) is
the contract. Past it the session is released and the work is picked up at the
terminal.

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

## The idle notification is noise in away mode

`Notification` with `idle_prompt` fires whenever the session sits at the
prompt. In away mode that is true in exactly the two states the bridge
already reports — holding for a reply, or released at the Q&A ceiling — so it
only ever duplicated a message the user had just had, and its wording ("may be
waiting on you") implied an action that did not exist.

Removed. Errors still arrive via `PostToolUseFailure`.

The same episode exposed a wording bug worth keeping in mind: the held-reply
path told the user "Claude's still working" whenever the session was alive
with nothing armed. Usually true, but not when the session has been released
at the ceiling — and claiming to know a state we cannot observe reads as the
bridge being confused. It now says only what it does: the reply is kept and
will be passed on at the next pause.

## A plugin cannot set env for its users

`plugin.json` carries metadata only — name, description, version, author and
such. There is no way for a plugin to contribute an environment variable or a
settings entry, confirmed against every manifest in the official marketplace.

That matters for `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`. Claude Code stops
honouring Stop-hook blocks after 8 consecutive ones with no tool call between;
raising it is what keeps a long Cliq conversation alive. Setting it once in
one person's `~/.claude/settings.json` does nothing for anyone else, so
pairing writes it into each user's own settings and says so. It needs a
restart to take effect.

## Data Streams works, with three traps on the way in

Measured against the live project, not read from docs. One `POST /ds-probe`
call, four steps, all green:

```
ok  load:      vendored package resolved
ok  channels:  1 channel(s)
ok  tokenPair: url=in2-dms.zoho.in  wss_id=…  key_present=true
ok  publish:   true
```

So the design holds: **an Advanced I/O function with no end-user context can
mint a subscriber ticket.** That was the single question that could have killed
it, because the SDK asks for the ticket with `CREDENTIAL_USER.user` while every
other method uses `.admin`, and the docs say token generation needs admin auth.
In practice the server-side call is accepted.

Also settled: the datacenter resolves itself. The ticket comes back with
`url: in2-dms.zoho.in`, so nothing has to know we are on `.in`.

### Ack-gating is per-subscriber, so one stalled laptop stalls only itself

The question that would have sunk a single shared channel. The docs say only
*"until the acknowledgement is sent, the next message in the stream will not be
delivered"* and never say whether that is scoped per subscriber or per channel.
Per channel, one laptop with a half-open socket would wedge the doorbell for
the whole org.

Measured with two live subscribers on `claude-cliq-wake` — A never acks, B acks
each, then three wakes published:

```
A: MESSAGE #1 streamingId=898147301518741393   A: deliberately NOT acking
B: MESSAGE #1 streamingId=898147301518741393   B: acked
B: MESSAGE #2 streamingId=898147320103701531   B: acked
B: MESSAGE #3 streamingId=898147338780937338   B: acked

A (never acks) received 1
B (acks each)  received 3
```

**Per-subscriber.** A stopped dead at one message; B was unaffected. Three
further facts from the same run:

- Both subscribers received the **same `streamingId`** for the first wake, so
  it is a true broadcast with independent per-subscriber cursors. Reading does
  not consume a message for anyone else.
- `subscribe('0')` live mode works, and delivery was effectively instant.
- **Ack on receipt, never after processing.** A's stall is exactly what
  happens if an ack is delayed — every later message is withheld. A wake is
  content-free, so losing one to a crash is recoverable from `Pending`; a
  stalled cursor is not.

Worth knowing for the gate harness: the vendored JS SDK makes a perfectly good
throwaway subscriber, so this was answered without writing a line of the
hand-rolled Python client. Sequencing note — the design-killer should be tested
before the client that is merely ours to write.

### Trap 1: the package is real but unpublished

`npm install @zcatalyst/datastreams` returns a 404 from the registry — verified
with the actual install command, not just `npm view`. `zcatalyst-sdk-node@3.4.0`
has no datastreams module, PyPI `zcatalyst-sdk@1.4.0` has none, and Maven's
`com.zoho.catalyst` group has none either. The source is on GitHub and already
versioned `1.0.0`.

Rebuild:

```
npm install github:catalystbyzoho/zcatalyst-sdk-js
cd node_modules/zcatalyst-sdk-js/packages/datastreams
# swap "workspace:*" for the PUBLISHED siblings, which do exist on npm
#   @zcatalyst/utils@^1.0.0  @zcatalyst/transport@^1.0.0  ws@^8.18.0
npm install typescript@5.4.5 @types/node@20 @types/ws
tsc -p tsconfig.cjs.json      # needs the monorepo tsconfig*.json one level up
```

Target is ES2020, so node20 runs it. Only `datastreams` needs vendoring —
`utils`, `transport`, `auth-admin` and `auth-client` are all published at 1.0.0.

### Trap 2: `new DataStreams()` cannot work, whatever the docs show

The documented usage is `new DataStreams()` with no argument, relying on the
SDK to "resolve project credentials from the execution environment". It always
throws `AUTH_ERROR`. `@zcatalyst/auth-admin@1.0.0` guards its env loader with

```js
if (!isNonEmptyString(!projectId)) return {};
```

and `!projectId` is a **boolean**, never a string, so `isNonEmptyString` is
always false, the guard always fires, and `getDefaultCredentials()` always
finds `{}`. Verified by calling their own validator.

Beyond the inverted condition it is the wrong idea for Advanced I/O anyway:
`x-zc-projectid` and its siblings arrive as request **headers**, not as process
env. So the app is initialised per request — `new ZCAuth().init(req)` — exactly
as the old SDK's `catalyst.initialize(req)` does it.

### Trap 3: a `file:` dependency deploys as a broken symlink

npm symlinks `file:` dependencies into `node_modules` by default, and
Catalyst's deploy zips the directory without following symlinks. The function
would have failed with `MODULE_NOT_FOUND` in production while working perfectly
on the laptop. `install-links=true` in `relay/functions/relay/.npmrc` copies
instead; the comment there says why, because a future `npm install` would
otherwise silently undo it.

## A hook can answer AskUserQuestion itself — but only two tools can

`PermissionRequest`'s decision union is exactly:

```js
decision: Ue([
  u({behavior:C("allow"), updatedInput:ue(o(),oe()).optional(),
     updatedPermissions:A(Lt()).optional()}),
  u({behavior:C("deny"), message:o().optional(), interrupt:H().optional()})
])
```

There is **no `"ask"`**, and no `reason` on either branch. Claude Code says so
itself on a validation failure: `PermissionRequest decision must be
{"behavior": "allow"} or {"behavior": "deny", "message": "..."}`.

For a tool that would otherwise show a dialog, an `allow` carrying no
`updatedInput` is **discarded** — `if(!B.updatedInput &&
e.requiresUserInteraction?.()) return null` — and the terminal prompt opens
anyway. The exempt set is `kxr = new Set([mu, Qo])` with `mu="ExitPlanMode"`
and `Qo="AskUserQuestion"`. Those two, and nothing else, can be answered by a
hook. `AskUserQuestion.call()` then reads `{answers, annotations}` straight out
of its own input, which is also exactly what the real dialog returns.

**Measured, not inferred.** A throwaway hook emitting

```json
{"behavior": "allow",
 "updatedInput": {"questions": [...], "answers": {"<question text>": "Second option"}}}
```

produced, with no dialog shown at all:

```
The user answered: "Mechanism probe 1 of 2 …"="Second option",
                   "Mechanism probe 2 of 2 …"="A typed answer that is not any listed option"
```

Three things that settles:

- `answers` is keyed by the **exact question text**, and the value is the
  option **label**. Confirmed against answered calls in `~/.claude/projects`:
  multiSelect is one comma-joined string, `"A, B, C"`.
- A custom answer is just free text in that same slot — it matches no listed
  label and is accepted regardless. So "Other" needs no special field.
- Several questions in one call are answered together, in one decision.

Note the framing: Claude Code renders a hook-supplied answer as *"The user
answered…"*. It is not the user. Anything reading that output has no way to
tell the difference, which is a good reason never to canned-answer outside a
probe.

## Cliq's markdown is not CommonMark

Probed the live bot with one message carrying ten labelled variants.

| sent | rendered |
| --- | --- |
| `**bold**` | bold, with the inner asterisks still visible — **broken** |
| `*bold*` | bold |
| `_italic_` | italics |
| `__italic__` | underline, not bold |
| `~~strike~~` | strikethrough, with the tildes still visible — **broken** |
| `~strike~` | strikethrough |
| `` `code` `` | code |
| `## Heading two` | a real heading |
| `- bullet item` | a literal hyphen, which reads fine anyway |
| `[the repo](url)` | a link |

Every doubled marker is one too many, and Claude writes doubled markers by
default — so `**bold**` had been reaching Cliq verbatim for the whole project.
`format.js` had always written its *own* labels with single asterisks, so the
renderer knew the dialect; nothing converted the prose passing through it.

`toCliqMarkdown()` now halves the doubled runs, skipping fences and code spans
so `**kwargs` in a snippet survives, and keeping CommonMark's rule that a
delimiter run must hug its content — without it, `2 ** 8` turns the rest of
the message bold.

Two notes for anyone extending this:

- `defuse_cliq_markup` on the laptop still replaces every `~` with `∼`, so
  strikethrough never reaches Cliq from a hook at all. That is deliberate:
  Claude writes `~/path` far more often than it means to strike text out.
- The probe itself exposed a real hazard. It was sent with the raw relay call
  rather than through a hook, so it armed no waiter — the bot asked a question
  and had nowhere to put the answer. The first reply bound correctly to the
  right id and was dropped anyway. **Anything the bot asks outside a hook must
  arm a waiter first.**

## Resume removed

Resuming an idle session from Cliq was built and then taken out. The mechanism
itself works — `--resume --bg` continues the same session with full context,
and its hooks fire. What sank it was everything around it:

- a session in `$HOME` can never be resumed in the background, and `$HOME` is
  where sessions get started;
- any other folder needs its trust prompt accepted first;
- a *live* idle session cannot be resumed at all, only forked, so the daemon
  had to tell live from dead before deciding what to offer;
- and the collector has to outlive Claude itself to receive the late reply,
  which hooks cannot arrange — Claude Code reaps processes they spawn. That
  needs a LaunchAgent, i.e. a second always-on component.

Four conditions, a confirm-then-resume round trip in Cliq, and a background
daemon, to serve the case where someone replies after half an hour of silence.
The contract is now one sentence: **a reply reaches a session whose hook is
still waiting, and nothing else.** Past the window the bot names the folder and
says to carry on there.

## Arming a waiter once is not enough

A hook arms its waiter, starts the daemon, then blocks for up to half an hour.
If the daemon dies in that window — killed, or standing down after a code
change — nothing notices, and the reply reaches Cliq and simply never arrives.
That is indistinguishable from the bridge being broken, and it happened
repeatedly during development because editing the daemon's own source makes it
exit on purpose.

`wait()` now checks every ten seconds and restarts the daemon if it has gone.

Related: diagnosing with `/await` **destroys the evidence**, because
collecting a message deletes it. Two of the replies investigated today were
consumed by the act of looking at them. `/peek` returns the queue without
consuming it.

## Away mode has to keep the collector alive

Every "I replied and nothing happened" tonight had one cause: nothing was
running to collect the reply. The reply reached the relay and bound correctly
every time.

The daemon exited after thirty minutes idle **even with away mode on** — which
is exactly backwards. Away mode is the user saying they have stepped out and
replies are expected; shutting down the one thing that collects them
guarantees the failure away mode exists to prevent. It now stays alive while
away mode is on, and `away on` starts it rather than waiting for a hook.

Two smaller gaps in the same path: a reply arriving while a *live* session was
mid-task was discarded with a "nothing is waiting" notice, and is now held for
half an hour until its next `Stop`; and `wait()` only revived the daemon while
a hook was actively waiting, which does nothing between turns. A reply for a
session that is gone is not held — there is nothing left to hand it to.

## Cliq reads ~text~ as strikethrough

Messages print paths like `~/claude-cliq`, so any two tildes in one message
struck out everything between them — and Claude's prose has tildes in paths
and approximations ("~5 minutes"). Paths now display with an ellipsis, and
stray tildes in message bodies are replaced with U+223C, which looks the same
and carries no markup meaning.
