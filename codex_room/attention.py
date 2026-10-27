"""Durable, per-member unread results; acknowledgements name an exact completion."""
from datetime import datetime


class UnreadResults:
    def __init__(self, entries, members, saved=None):
        self.seen = {entry.id for entry in entries}
        self.since = int(datetime.now().timestamp())
        valid = {entry.id: entry for entry in entries if self.eligible(entry)}
        saved = saved if isinstance(saved, dict) else {}
        self.pending = {name: entry_id for name, entry_id in saved.items()
                        if name in members and entry_id in valid and valid[entry_id].sender == name}

    @staticmethod
    def eligible(entry):
        return (entry.sender not in {"you", "system"} and not entry.hidden and entry.communication is None
                and entry.state in {"done", "failed"} and entry.finished_at is not None)

    def observe(self, entry):
        if entry.id in self.seen or not self.eligible(entry):
            return
        self.seen.add(entry.id)
        # Resuming/importing old history must not manufacture new notifications.
        if datetime.fromisoformat(entry.finished_at).timestamp() < self.since:
            return
        self.pending[entry.sender] = entry.id

    def acknowledge(self, member, entry_id):
        # A late receipt for an earlier result cannot erase a newer completion.
        if self.pending.get(member) != entry_id or not entry_id:
            return False
        del self.pending[member]
        return True
