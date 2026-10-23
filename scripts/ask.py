#!/usr/bin/env python3
"""Answering Claude's own multiple-choice questions from Cliq.

`AskUserQuestion` arrives at the PermissionRequest hook like everything else,
but it is not a permission: Allow and Deny are not answers to "which approach?".
Claude Code lets a hook answer it directly -- that and `ExitPlanMode` are the
only two tools where an `allow` carrying `updatedInput` replaces the terminal
dialog instead of being discarded -- and `AskUserQuestion.call()` reads
`{answers, annotations}` straight out of its own input.

So the job is to collect real answers and hand them back in the shape the
terminal dialog would have produced:

    {"answers": {"<exact question text>": "<option label>"}}

multiSelect is one comma-joined string. A custom answer is plain free text in
the same slot, so "Other" needs no special field. All measured, see
docs/FINDINGS.md.
"""

from __future__ import annotations

import hookutil as H
import waiter
from cliqlib import Config, Relay, is_away, new_request_id


def questions_of(payload: dict) -> list[dict]:
    qs = (payload.get("tool_input") or {}).get("questions")
    return qs if isinstance(qs, list) else []


def answerable(questions: list[dict]) -> bool:
    """Can every question be put as a choice?

    An extended AskUserQuestion schema allows `kind` of text or number, which
    may or may not be enabled in a given build. Anything that is not a list of
    options belongs at the terminal, where it can be typed.
    """
    if not questions:
        return False
    for q in questions:
        if not isinstance(q, dict):
            return False
        if q.get("kind") not in (None, "", "choice"):
            return False
        if not isinstance(q.get("options"), list) or not q["options"]:
            return False
    return True


def render(questions: list[dict]) -> str:
    """The message a person reads, and the relay parses back.

    Deliberately one format for both. The relay rebuilds the form from this
    text when the button is tapped, because a button's `arguments` cap at 300
    characters and a four-question spec will not fit -- so nothing is stored
    and the message IS the spec.

    Strict enough to parse, plain enough to read:

        *The question?*
        1. First option
        2. Second option
    """
    out = []
    for q in questions:
        # The marker is how the relay knows to build a checkbox rather than a
        # radio. It has to live in the text, because the text is the only
        # thing that comes back.
        marker = " (choose any)" if q.get("multiSelect") else ""
        out.append(f"*{' '.join(str(q.get('question', '')).split())}{marker}*")
        for i, opt in enumerate(q.get("options") or [], start=1):
            label = " ".join(str(opt.get("label", "")).split())
            out.append(f"{i}. {label}")
            desc = " ".join(str(opt.get("description", "")).split())
            if desc:
                # Indented with em spaces: Cliq collapses ordinary leading
                # whitespace in a continuation line.
                out.append(f"  _{desc[:160]}_")
        out.append("")
    return "\n".join(out).rstrip()


def to_answers(questions: list[dict], picked: dict) -> dict:
    """Turn {question index: [option indexes or free text]} into what Claude wants.

    Keyed by the EXACT question text from the original input, never by anything
    that travelled through Cliq, and valued by the exact original label -- the
    label Cliq echoes back may have been truncated at its 100-character cap.
    """
    answers: dict[str, str] = {}
    for i, q in enumerate(questions):
        chosen = picked.get(str(i)) or picked.get(i)
        if not chosen:
            continue                      # unanswered questions are omitted
        labels = []
        for item in chosen if isinstance(chosen, list) else [chosen]:
            if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
                opts = q.get("options") or []
                n = int(item)
                if 0 <= n < len(opts):
                    labels.append(str(opts[n].get("label", "")))
                continue
            text = str(item).strip()       # free text: an "Other" answer
            if text:
                labels.append(text)
        if not labels:
            continue
        key = str(q.get("question", ""))
        if key in answers:
            # Two questions with identical text collapse to one key, so the
            # selections join rather than one silently winning.
            answers[key] = f"{answers[key]}, {', '.join(labels)}"
        else:
            answers[key] = ", ".join(labels)
    return answers


def decision(payload: dict, answers: dict) -> dict:
    """The hook output that answers the question.

    `updatedInput` must carry the original input too: Claude Code re-checks it
    and `AskUserQuestion.call()` reads `questions` from the same object.
    """
    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest",
        "decision": {"behavior": "allow",
                     "updatedInput": {**(payload.get("tool_input") or {}),
                                      "answers": answers}}}}


def handle(cfg: Config, relay: Relay, payload: dict) -> int:
    """Ask in Cliq, wait, and answer. Emitting nothing falls back to the
    terminal dialog, which is the right outcome for every failure here."""
    questions = questions_of(payload)
    if not answerable(questions):
        return 0

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    rid = new_request_id()
    ctx = H.context_line(cwd)
    head = "❓ Claude is asking" + (f" — {ctx}" if ctx else "")
    text = f"{head}\n\n{render(questions)}"

    if not is_away():
        # notify-only: say what was asked, then get out of the way. The
        # terminal is already showing the picker.
        H.send(relay, kind="ask", text=text, request_id=rid,
               session_id=session_id, cwd=cwd, expects_reply=False,
               hint="waiting for you at the terminal")
        return 0

    if not H.send(relay, kind="ask", text=text, request_id=rid,
                  session_id=session_id, cwd=cwd, expects_reply=True,
                  hint="tap Answer to pick",
                  answers=[{"label": "Answer", "value": "form", "req": rid}]):
        return 0                          # relay down: the terminal has it

    reply = H.block_for_reply(cfg, rid, session_id=session_id, kind="ask",
                              cwd=cwd, topic=render(questions)[:160])
    if not reply:
        return 0                          # window lapsed; terminal takes over

    if reply.get("type") == "cancelled":
        # The form was dismissed. Hand the session back at once rather than
        # holding it for the rest of the window.
        return 0

    picked = reply.get("picked")
    if reply.get("type") == "answer" and not picked and len(questions) == 1:
        # A plain typed reply answers a single question verbatim, which is
        # also how someone says something none of the options cover.
        picked = {"0": (reply.get("text") or "").strip()}
    if not picked:
        return 0

    answers = to_answers(questions, picked)
    if not answers:
        return 0
    H.emit(decision(payload, answers))
    return 0
