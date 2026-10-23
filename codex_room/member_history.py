"""Bounded, source-linked user dialogue previews, without model summarization."""
from __future__ import annotations

USER_CHARS = 2_000
REPLY_CHARS = 600
REPLY_LINES = 3


def excerpt(text, limit, lines=None):
    content = "\n".join(text.splitlines()[:lines]) if lines else text
    return {"text": content[:limit], "truncated": len(content) > limit or content != text}


def recent_dialogue(entries, name, count):
    if not count:
        return []
    questions = [(index, e) for index, e in enumerate(entries)
                 if e.sender == "you" and e.target == name and not e.hidden and not e.communication]
    selected = questions[-count:]
    results = []
    for index, question in selected:
        # Several user inputs can share a Codex run after steer. Match the
        # nearest preceding user input in that run, rather than mixing in an
        # unrelated peer turn that happened between user messages.
        boundary = next((pos for pos, q in questions if pos > index and question.run_id and q.run_id == question.run_id), len(entries))
        replies = [e for e in entries[index + 1:boundary]
                   if question.run_id and e.run_id == question.run_id and e.sender == name
                   and not e.hidden and not e.communication and e.body.strip()]
        item = {"user": {"message_id": question.id, "time": question.time, "state": question.state,
                         **excerpt(question.display_body if question.display_body is not None else question.body, USER_CHARS)},
                "codex_preview": None}
        if question.quote:
            item["user"]["has_forwarded_material"] = True
        if question.attachments:
            item["user"]["attachments"] = [{"kind": a.kind, "label": a.label} for a in question.attachments]
        if replies:
            reply = replies[-1]
            item["codex_preview"] = {"message_id": reply.id, "time": reply.time, "state": reply.state,
                                     **excerpt(reply.body, REPLY_CHARS, REPLY_LINES)}
        results.append(item)
    return results
