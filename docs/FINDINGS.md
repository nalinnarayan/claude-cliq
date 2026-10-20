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

### The inbound idempotency key is never sent

`core.inbound` reads

```js
// Cliq does not redeliver, but a handler may fire twice on a retry of
// its own; keying on the Cliq message id makes this idempotent.
const id = String(body.message_id || crypto.randomUUID());
```

but `message_handler.dg` only ever sends `cliq_user`, `text` and
`replied_text`. So the fallback fires every time, `msg_id` is a fresh UUID on
every delivery, and **the documented idempotency does not exist** — a handler
firing twice inserts two rows and the reply is delivered twice.

Caught by noticing a queued row's id was a UUID rather than a Cliq id:

```
queued: 1
  id=55ef2165-1196-4750-8964-675d0a32d031  request_id=(none)  text='Hi'
```

Unfixed. The handler would need to pass the incoming message's own id, and
nothing in this repo records whether `message_details` exposes one — only that
`message_details.message.replied_message` carries the id of the message being
*replied to*. Worth probing with a debug handler before claiming it can be
done.

### Two writes per notification had already blown the write tier

Separate from polling, and found while reviewing rather than from a failure.
Catalyst's free tiers are not uniform:

| | free per month |
| --- | --- |
| SELECT | 10,000 |
| INSERT | 5,000 |
| **UPDATE** | **1,000** |
| DELETE | 1,000 |

Every outbound notification performed **two** UPDATEs, each preceded by its
own SELECT:

```js
putSession:  SELECT ROWID FROM Sessions  ... → updateRow
bumpCounter: SELECT ROWID, n FROM Counters ... → updateRow
```

At twenty notifications a day that is ~1,200 UPDATEs a month against an
allowance of 1,000 — breached on its own, before a single poll. Fixing only
the polling would have hit this a few weeks later.

Both are now gone, for different reasons.

**`putSession` had no reader.** Its only consumer was the daemon's
`handle_unmatched`, deciding whether a reply with nothing waiting belonged to
a live session. A blocked hook knows that about itself, so the table went with
the daemon.

**The send counter had nowhere cheap to go.** It guarded Cliq's shared 50/min,
which is worth guarding — breaching it locks out the whole org for ten
minutes. But Catalyst offers no cheap counter: **Cache has no atomic
increment** (only get/put/update/delete), and a get+put there lands on a
**1,000/month GET tier**, worse than the Data Store. So it is removed rather
than approximated, and the gap is stated instead:

- a breach now surfaces as Cliq's own 429, which `cliq.dm` already detects;
- pacing is enforced **per laptop** (`local_sends_per_min`, a small local
  file, free), which bounds one runaway machine;
- it does **not** bound fifty people active at once. That is a capacity
  problem needing per-user pacing or a queue, and pretending a miscounted
  40/min budget covered it was worse than admitting it does not.

Tables went from five to three: `Devices`, `PairCodes`, `Pending`. `Sessions`
and `Counters` can be dropped from the console at leisure; nothing reads or
writes them.

### Cliq button labels cap at 20 characters, and a rejected send looked asked

The chooser's first real run produced nothing, and the log said why:

```
notify failed: HTTP 400: {"code":"input_maxlength_reached",
  "message":"The size of the key 'label' has exceeded the character limit of 20."}
collect: asked which of ['yaqbj', 'nmvxp'] a plain reply belongs to
```

`"claude-cliq (question)"` is 22 characters. Cliq rejects the **whole
message** — not the label, not the button, the message — so no chooser
appeared at all. `buildButtons` had been truncating to 100, which was our own
guess with nothing behind it.

The label now carries the folder alone, capped at 20, with what each session
wants moved into the message text where there is room. Sessions sharing a
folder name get a distinguishing suffix, since two identical buttons are worse
than none.

**The second bug is the instructive one.** Those two log lines are in the wrong
order: the send failed, and the code then recorded that it had asked.
`_notify` swallowed the `RelayError` and returned `None`, so `_ask_which`
marked `asked=True` regardless — and `needs_pick` meant the reply could only
ever be resolved by a tap that would now never be offered. One reply was
permanently, silently stuck.

`_notify` returns whether it actually sent, and nothing records "I asked"
unless the question went out. Worth generalising: **a helper that swallows its
errors and returns nothing teaches every caller to assume success.** The same
helper shape had already hidden a failed ticket renewal earlier in this
project.

### The chooser was deleted, and a commit message said otherwise

Reported: *"When I swipe reply it correctly lands in that session. When I just
reply into the text box, both sessions ignore the reply."*

Both halves were working as designed and the design had a hole. `route()`
returns `(None, None)` for a plain reply with two waiters — correct, that is
the never-guess rule. `ask_which`, the thing that resolved the ambiguity, was
deleted with the daemon, and the step-3 commit claimed it *"moves rather than
dies"*. It did not move. A claim in a commit message is not a change.

So a plain reply was fetched, found unroutable, and left in the spool in
silence. Worse, it stayed **eligible**: the moment one session finished and a
single waiter remained, that text would answer whatever was asked next.

Rebuilt on the hook side, with two differences from the daemon version:

- **`needs_pick`.** An ambiguous reply is marked unroutable, not merely
  unrouted. It can only ever be resolved by naming a session. Previously
  "nothing matched" and "must not be auto-matched" were the same state, which
  is what made the drift possible.
- **FIFO, not last-in.** The daemon popped the most recently held item by
  position. The chooser now asks about one reply at a time and a tap applies
  to the oldest outstanding one, because a leader changes hands and position
  is not identity.

What survived the deletion and needed no rebuilding: buttons, `/cliq-action`,
and the per-button request-id override. The spool also already did the
"hold the text" job `hold_reply` used to, so this was wiring rather than
design.

The leader that asks is often gone before the tap arrives. That turns out not
to matter: a tap is just another queued reply and it names its target, so
whichever hook collects it next can apply it.

### A second concurrent session gave up after ten seconds

Reported, not found by testing: *"I turned away mode on from another session.
It said it was already on and started waiting. But suddenly stopped waiting and
showed done."*

`waiter.wait()` ended with `disarm()` on timeout. That was right when its
`deadline` **was** the question's window. It stopped being right the moment
`collect.wait_for()` began calling it in ten-second slices so a follower could
re-check whether leadership had fallen vacant:

```
follower: wait(rid, now + 10)   -> times out, DISARMS its own waiter
          armed(rid)            -> False
          "answered at the terminal, or expired" -> returns None
hook:     emits nothing -> the turn finishes -> "done"
```

So the leader kept working and every *other* session stopped waiting after ten
seconds. An ownership mistake, not a logic one: a function given a slice of a
window must not clean up as though it owned the whole thing.

`wait()` is now purely "watch until this moment" — it still disarms on
**resolve**, because the payload has been taken, but never on timeout.
`block_for_reply()` disarms when the window is genuinely over, since it is the
only caller that knows that.

Proven with a held lock and a real follower:

```
slice 1 after  10.0s -> got=None still armed=True
slice 2 after  20.0s -> got=None still armed=True
slice 3 after  25.1s -> got=None still armed=True
```

Worth noting how this was missed. "Two concurrent sessions" had been on the
untested list for days and was named as the most likely regression *in the
same message that shipped it* — the single-collector daemon existed to prevent
exactly this race, and replacing it with a lock and a spool was never
exercised with two real sessions. Naming a risk is not testing it.

### A connection name is a lease, and a per-device one degrades silently

The sharpest bug of the lot, because nothing failed. The relay derived
`connection_name` from the device's token hash, which seemed obviously right:
stable, opaque, one per machine.

It meant the **second** blocked hook could not get a ticket at all.

```
collect: no stream ticket (HTTP 500:
  {"error":"Duplicate value for connection_name. Please give a different value"})
  — polling instead
collect: fetched 1 → routed to mnhgu: answer
```

Minting for a name whose subscriber session still exists is refused, and
`close()` **preserves** that session by design. So the first hook took the
stream, and every hook after it fell back to polling — and because the
fallback works, the reply still arrived and nothing looked wrong. It was found
only by reading the log of a cycle that had already succeeded.

Two changes:

- **The name is random per ticket**, `crypto.randomBytes(10)`, not derived
  from anything. Nothing needs it stable — it names a connection, and the
  ticket is what authorises. It is also better for privacy: a stable name
  handed to access control in the clear would have re-linked devices whatever
  the wake label did.
- **`close()` now unsubscribes first**, deleting the session rather than
  parking it. Parking exists so a later `-2` can resume, which this bridge
  never does — it fetches once on arming instead — so tidying up is free.

Verified: two consecutive tickets, two different names, both subscribed, same
`wake_id` (the bell is per account, the lease is per connection).

The trade accepted: with a fresh name each time, *every* connect is a first
connect, so the transient `SESSION_EXPIRED` can appear on any cycle rather
than only the first ever. One extra round trip, absorbed by the retry — much
better than every second hook quietly costing a hundred times the reads.

**The lesson generalises.** A fallback that works is a fallback that hides
things. The poll path did exactly what it was built for and that is precisely
why the stream being broken went unnoticed; `cliqctl status` should report
which transport actually served the last reply, not merely which is
configured.

### A ticket cannot be renewed while its session is alive

```
wake: session not ready yet — reconnecting (1/3)
wake: could not renew the ticket: HTTP 500:
      {"error":"Duplicate value for connection_name. Please give a different value"}
wake: subscribed
```

Re-minting a ticket for a `connection_name` whose subscriber session still
exists is refused. And `close()` **preserves** that session on purpose — that
is exactly what makes `-2` resume possible — so after a disconnect the name is
still taken and renewal can only fail.

Retrying with the **same** ticket is what works, which is what the transient
`SESSION_EXPIRED` wanted anyway. The renewal path is removed rather than left
in place to fail quietly; it survived the first live run only because a failed
renew was non-fatal.

Consequence for anything built later: a per-device `connection_name` is a
*lease*, not a label. Two live connections cannot share one, so a second
concurrent subscriber on the same device would need a different name — and the
name is capped at 20 characters.

### The doorbell works, and two more limits are undocumented

First full cycle, on the test environment:

```
15:58:50 wake: subscribed
16:00:17 MY WAKE -> fetching
16:00:21 COLLECTED: 'Hi'    (request_id=none)
16:00:21 COLLECTED: 'Hello' (request_id=none)
```

A typed reply reached `/cliq-inbound`, which filed it and rang the bell; the
subscriber recognised its own label, fetched **once**, and drained the
account's queue. **Two SELECTs for the whole cycle**, against 5,760 a day for
the polling it replaces. The queue read 0 afterwards, so delete-on-delivery
works.

Note what came back: the earlier `'Hi'`, stranded from a previous round with
nothing listening, was collected alongside the new reply. That is the
"replied while nothing was listening" case solving itself for free — and it is
also the **stale-reply hazard** in action. A reply from twenty minutes earlier
was handed back as current. With one armed waiter, `waiter.route()` would have
delivered it as the answer to a brand-new question. The age guard is not
optional.

**`connection_name` is capped at 20 characters.** Measured: 21, 22, 23, 24 and
32 are all refused with

```
Please check if the length of the input is in the correct range
```

which names neither the field nor the limit. 20 passes. The SDK's own
`isValidInputString` only checks the charset `[a-zA-Z0-9-_]+`, so this is
server-side and nothing warns you.

**The token-pair response does not include `channel_id`**, though the docs
list it among the fields. Only `url`, `key` and `wss_id` come back. The relay
fills it from its own config rather than passing `undefined` down to the
laptop.

**`SESSION_EXPIRED` on a first connect is confirmed transient.** It fired again
on the very first use of the derived connection name, with a ticket seconds
old, and connected first try on the next attempt. `wake.listen()` now treats
`SESSION_EXPIRED`, `SERVER_DOWN` and `SWITCH_URL` as *reconnect*, with backoff
and optional ticket renewal, while `AUTH_FAILED`, `MISSING_KEY` and `BLOCKED`
stay fatal. Without that, every laptop's first connection would fail with a
message pointing at entirely the wrong problem.

### A stdlib Python subscriber works; two details are not in any doc

`scripts/wake.py` is ~260 lines of RFC 6455 against the protocol recovered from
the built SDK. Measured against the live channel:

```
connecting…
wake: subscribed            <- 101 + Sec-WebSocket-Accept verified, then mtype=0
WAKE #1: {'w': 'probe'}
WAKE #2: {'w': 'probe'}
listen() returned 'done' after 2 wake(s)
```

The **second** wake is the real proof: delivery is sequential per subscriber,
so it only arrives if the ack for the first was accepted.

**Every control payload but the ack carries `type: "con"`.**

```js
subscribePayload   = { type: 'con', value: 'subscribe', streamingId: '' }
serverPingPayload  = { type: 'con', value: 'ping' }
unsubscribePayload = { type: 'con', value: 'unsubscribe' }
ackPayload         = { type: 'ack', streamingId: '' }   // no 'con'
```

Easy to miss by grepping for `value:` and assuming the rest of the struct.

**`SESSION_EXPIRED` (-11) can be transient on a brand-new connection name, and
it is a liar.** Two first-connect attempts were refused with -11, both with
freshly minted tickets — once before the `type: "con"` fix and once after. The
*same* ticket then connected fine seconds later, both from the JS SDK and from
this client. So the fix is right by reference to the SDK but was **not** shown
to be the cure.

The consequence for the product: -11 must be **retried once with a fresh
ticket** rather than failing the wait. Its wording sends you hunting for a
stale credential, which is the wrong place entirely — the handshake, the
Accept header and the subscribe payload were all correct at the time.

A useful shape for diagnosing this: connect **without** subscribing first. The
server answers immediately with a session frame (`uid`, `rsid`, `sid`, `xa`,
`zuid`), which separates "the connection was refused" from "the subscribe was
refused" — two failures that look identical from a client that does both at
once.

### The ticket is the security boundary; channel access control is not

Measured, not assumed. `/ds-access` was made to refuse any `connectionName`
beginning with `deny-`, a ticket was minted for exactly such a name, and the
connection was **accepted anyway**:

```
POST /            {"connectionName":"deny-gate4"}  ->  403 {"approved":false}
connect with that ticket                           ->  OPEN, accepted
```

Mounted at `/ds-access` and again at `POST /`, since Catalyst invokes the
*function* for access control and has no way to know about a sub-path. Both
refused; the connection still opened. So the channel's access control is not
calling this relay.

The channel reports `control_type: 1` with `control_ids: 64689000000056913` —
one numeric id, which is a **User Roles** configuration (the dialog's default
radio), not Custom Logic. Worth noting what this does *not* prove: whether a
Custom Logic channel would fail open or closed is still unknown, because this
channel never invoked anything.

What it does prove, and it is the thing that matters:

**A connection cannot be made without a token pair, and token pairs are minted
only by the relay with admin credentials after a valid device token is
presented.** Access control adds nothing observable on top. So `/ds-access`
must not be described, or relied on, as a gate — it is a shape check at a path
nothing currently calls. Note also that minting is *not* gated by access
control: a ticket was issued for `deny-gate4` without complaint.

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
