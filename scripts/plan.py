"""Approving a plan from Cliq, with the plan in front of you.

ExitPlanMode used to arrive here as an ordinary permission: three lines saying
"Approve the plan and start work", with Allow / Deny buttons. The plan itself
never left the laptop, so from a phone you were asked to approve something you
could not read, using choices that did not match the terminal's.

Now the plan is posted as a FILE -- the one thing a Cliq bot can send that is
meant to be opened rather than scrolled past -- and the three choices the
terminal offers follow as a form.

Two measured constraints shape this:

  * The v2 file endpoint refuses `buttons`, though it accepts `comments`
    (which must be a JSON array). So the document and the choices are two
    separate messages, document first.
  * `updatedInput` REPLACES the whole tool input, so the original has to be
    merged over rather than rebuilt -- the same rule as ask.decision().
"""

from __future__ import annotations

import hookutil as H
from cliqlib import (Config, Relay, is_away, log, new_request_id,
                     notify_only_hint)

# What the terminal offers, in the same order, rendered in the grammar
# ask.render() produces so the existing form machinery parses it unchanged.
# No new Deluge function, no new relay route for the form.
# Only the two that approve. "Keep planning" is NOT listed, deliberately:
# buildForm appends its own "Something else — type it below" option with a box
# attached, and that box IS the keep-planning path, exactly as the terminal's
# third choice is the one you type into. Listing both gave four options where
# Claude offers three, and made the typed one look like an afterthought
# rather than the way to send feedback.
CHOICES = [
    ("auto", "Start work, auto-accept edits"),
    ("review", "Start work, I'll review each edit"),
]

# The permission mode each approval switches the session to, mirroring what
# the terminal's own options describe.
MODES = {"auto": "acceptEdits", "review": "default"}

MAX_CAPTION = 280


def plan_of(payload: dict) -> str:
    """The plan markdown out of the tool input.

    ExitPlanMode's input schema is not documented anywhere. `plan` is the
    field it has been observed to use, but rather than depend on that alone,
    the longest string value is taken as the fallback -- a plan is by far the
    largest thing such an input could carry. The keys are logged once so the
    real shape is recorded the first time this runs rather than guessed at
    again later.
    """
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    log(f"plan: tool_input keys = {sorted(tool_input)}")

    text = tool_input.get("plan")
    if isinstance(text, str) and text.strip():
        return text
    longest = ""
    for value in tool_input.values():
        if isinstance(value, str) and len(value) > len(longest):
            longest = value
    return longest


def render_choices() -> str:
    """The question block, in ask.render()'s grammar.

    That grammar is the spec the relay parses back (see askform.js), so this
    has to match it exactly: a bold line for the question, `n. label` for each
    option. The form adds its own "Something else" box, which is what carries
    the feedback for "keep planning".
    """
    # The question says what typing does, since the appended option is
    # generically worded and shared with AskUserQuestion. Over 50 characters,
    # so splitLabel puts the tail in the hint beside it.
    lines = ["*What should I do with this plan? Type below to keep planning.*"]
    for i, (_, label) in enumerate(CHOICES, start=1):
        lines.append(f"{i}. {label}")
    return "\n".join(lines)


def caption_for(plan: str) -> str:
    """A line or two of the plan, so the chat says what the document is.

    The file is the thing to read; this exists so the message is not a bare
    attachment with no clue what it concerns.
    """
    for raw in (plan or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line[:MAX_CAPTION]
    return "A plan to look over."


def decision(payload: dict, choice: str, feedback: str) -> dict:
    """The hook output for a chosen option.

    Approving also sets the session's permission mode, which is what makes
    "auto-accept edits" mean anything. `setMode` is sent explicitly for the
    review case too: the session is still in plan mode when this runs, so
    omitting it would leave the mode unchanged rather than merely default.
    """
    if choice == "keep":
        return {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            # `message` is the field on this branch -- `reason` is dropped
            # silently, which is why denials used to reach Claude unexplained.
            # No `interrupt`: the point is to keep planning, not to stop.
            "decision": {"behavior": "deny",
                         "message": feedback or "Keep planning — no reason given."}}}

    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {
            "behavior": "allow",
            # Merged over the original: updatedInput replaces the whole object.
            "updatedInput": dict(payload.get("tool_input") or {}),
            "updatedPermissions": [
                {"type": "setMode",
                 "mode": MODES.get(choice, "default"),
                 "destination": "session"},
            ]}}}


def _send_document(relay: Relay, plan: str, cwd: str) -> bool:
    """Post the plan as a file. False if it could not be sent."""
    name = "plan.md"
    try:
        relay.notify_file(name=name, data=plan.encode("utf-8"),
                          caption=caption_for(plan), content_type="text/markdown")
        return True
    except Exception as exc:                      # relay down, Cliq 429, …
        log(f"plan: could not send the document: {exc}")
        return False


def handle(cfg: Config, relay: Relay, payload: dict) -> int:
    """Send the plan, offer the choices, and answer.

    Emitting nothing falls back to the terminal dialog, which is the right
    outcome for every failure here.
    """
    plan = plan_of(payload)
    if not plan.strip():
        return 0                          # nothing to show; let the terminal ask

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    rid = new_request_id()
    ctx = H.context_line(cwd)
    head = "📋 A plan is ready" + (f" — {ctx}" if ctx else "")

    if not is_away():
        # notify-only: say the plan exists and get out of the way. No
        # document either -- the terminal is already showing it.
        H.send(relay, kind="ask", text=f"{head}\n\n{caption_for(plan)}",
               request_id=rid, session_id=session_id, cwd=cwd,
               expects_reply=False,
               hint=notify_only_hint(waiting_at_terminal=True))
        return 0

    # The document first: the choices are what gets acted on, so they should
    # be the last thing in the chat.
    sent = _send_document(relay, plan, cwd)
    if not sent:
        head += "\n\n_(the document itself couldn't be sent — it's at the terminal)_"

    text = f"{head}\n\n{render_choices()}"
    if not H.send(relay, kind="ask", text=text, request_id=rid,
                  session_id=session_id, cwd=cwd, expects_reply=True,
                  hint="tap Answer to choose",
                  answers=[{"label": "Answer", "value": "form", "req": rid}]):
        return 0                          # relay down: the terminal has it

    # The question window, not the 300s permission one: a plan is read and
    # considered. `kind="ask"` also unlocks the routing that lets a one-word
    # reply be an answer rather than a verdict.
    log(f"plan: sent {rid} ({len(plan)} chars), waiting")
    # tool_use_id so on_settled can DISARM this waiter when the question is
    # answered at the terminal instead. Without it the waiter sat armed for
    # its full window, and every abandoned one showed up in the chooser as
    # another "session" to pick from.
    reply = H.block_for_reply(cfg, rid, session_id=session_id, kind="ask",
                              tool_use_id=payload.get("tool_use_id", ""),
                              cwd=cwd, topic=caption_for(plan)[:160])
    log(f"plan: {rid} -> {(reply or {}).get('type', 'nothing')}")
    if not reply or reply.get("type") == "cancelled":
        return 0                          # lapsed or dismissed; terminal takes over

    picked = reply.get("picked") or {}
    chosen = picked.get("0") or picked.get(0) or []
    if isinstance(chosen, str):
        chosen = [chosen]
    if not chosen:
        return 0

    # The first entry is the option index; anything that is not an index is
    # what they typed instead, which is feedback either way.
    index, feedback = None, ""
    for entry in chosen:
        value = str(entry).strip()
        if value.isdigit() and int(value) < len(CHOICES):
            index = int(value)
        elif value:
            feedback = value

    if index is None:
        # Typed rather than picked, which is the keep-planning path: the text
        # becomes the feedback Claude plans against. This is now the ONLY way
        # to keep planning, which is why "Keep planning" is not a listed
        # choice -- picking the typed option without typing is refused by the
        # form itself (missingOther), so feedback cannot be empty here.
        return _emit(payload, "keep", feedback)
    return _emit(payload, CHOICES[index][0], feedback)


def _emit(payload: dict, choice: str, feedback: str) -> int:
    log(f"plan: {choice}" + (" with feedback" if feedback else ""))
    H.emit(decision(payload, choice, feedback))
    return 0
