"""The single public tool shared by dynamic tools and the old-thread MCP adapter."""
from __future__ import annotations

DESCRIPTION = """Send a PUBLIC message to another independent Codex Room member.
Use only when the user requests cooperation, or to answer an incoming Room request.
All text is visible to the user. This returns a delivery receipt immediately, NOT
the recipient's answer. Never wait/poll for it; their reply arrives as a new input.
Use reply_to=the incoming message_id when replying. A reply defaults to
expect_reply=false: finish your contribution without triggering an acknowledgement
loop. Set expect_reply=true only if a further substantive answer is needed.
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
        "expect_reply": {"type": "boolean", "description": "Need a substantive response? New requests default true; replies default false."},
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
