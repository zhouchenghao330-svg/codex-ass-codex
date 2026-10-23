"""The single public tool shared by dynamic tools and the old-thread MCP adapter."""
from __future__ import annotations

DESCRIPTION = """Send a PUBLIC message to another independent Codex Room member.
Use only when the user requests cooperation, or to answer an incoming Room request.
All text is visible to the user. This returns a delivery receipt immediately, NOT
the recipient's answer. Never wait/poll for it; their reply arrives as a new input.
Use reply_to=the incoming message_id when replying. A reply defaults to
expect_reply=false (no answer requested), but this is a hint, NOT a closed
conversation. Continue substantive work required by the user's task even if an
incoming message says false. Set expect_reply=true when the other member should
continue, including intermediate turns of a multi-step discussion or counting
task; set false when your contribution needs no answer. Never acknowledge just
to acknowledge.
Do not send 'received', 'thanks', or repeat a message already accepted. Each chain
has a fixed message budget and can be stopped by the user. An invalid recipient
returns the current member list. Native child agents keep their native tools.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient's @name or display name from the Room roster."},
        "message": {"type": "string", "description": "Complete public message, including context needed by the recipient."},
        "reply_to": {"type": ["string", "null"], "description": "Incoming message_id when replying; otherwise omit."},
        "expect_reply": {"type": "boolean", "description": "Request a substantive response (true for intermediate turns, false when none is needed). A hint, not a conversation lock. New requests default true; replies default false."},
    },
    "required": ["to", "message"],
    "additionalProperties": False,
}


def dynamic_tools():
    return [{"type": "namespace", "name": "room", "description": "Public Codex Room communication.",
             "tools": [{"type": "function", "name": "send_message", "description": DESCRIPTION, "inputSchema": SCHEMA}]}]


def mcp_tool():
    return {"name": "send_message", "description": DESCRIPTION, "inputSchema": SCHEMA,
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}}
