"""The single public tool shared by dynamic tools and the old-thread MCP adapter."""
from __future__ import annotations

DESCRIPTION = """Send a PUBLIC message to another independent Codex Room member.
Use only when the user requests cooperation, or to answer an incoming Room request.
All text is visible to the user. This returns a delivery receipt immediately, NOT
the recipient's answer. Never wait/poll for it; their reply arrives as a new input.
Use reply_to=the incoming message_id when replying. Every call MUST explicitly
set expect_reply; there is no default. Set true when the other member must
continue, including intermediate discussion or counting steps; set false when
no reply is needed. Receiving false still lets you read/use the information for
your user's task, but the host blocks automatic messages back to that sender.
Do not bypass this by omitting reply_to or replying to an older message. Other
tools and reporting to your user remain available. After a blocked call, stop
retrying and return to your user's work. Never acknowledge just to acknowledge.
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
        "expect_reply": {"type": "boolean", "description": "Required, no default: true when the recipient must continue (including intermediate steps), false when no reply is needed. The host blocks automatic messages back to the sender of an incoming false message."},
    },
    "required": ["to", "message", "expect_reply"],
    "additionalProperties": False,
}


def dynamic_tools():
    return [{"type": "namespace", "name": "room", "description": "Public Codex Room communication.",
             "tools": [{"type": "function", "name": "send_message", "description": DESCRIPTION, "inputSchema": SCHEMA}]}]


def mcp_tool():
    return {"name": "send_message", "description": DESCRIPTION, "inputSchema": SCHEMA,
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}}
