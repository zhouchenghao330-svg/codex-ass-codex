//! Only acknowledge a completed result after its expanded end is actually visible.
use super::*;

impl App {
    pub(super) fn unread_count(&self) -> usize {
        self.members
            .iter()
            .filter(|m| !string(m, "unread_entry_id").is_empty())
            .count()
    }

    pub(super) fn mark_visible_results(&mut self) {
        if !self.focused
            || self.modal.is_some()
            || self.settings.is_some()
            || !self.candidates.is_empty()
            || self.selection.is_some()
            || self.press.is_some()
        {
            return;
        }
        let end = self.scroll.saturating_add(self.body.height as usize);
        for member in &mut self.members {
            let id = string(member, "unread_entry_id");
            if id.is_empty() {
                continue;
            }
            let Some(&message) = self.index.get(id) else {
                continue;
            };
            let entry = &self.entries[message];
            if entry["collapsed"] == true || !matches!(string(entry, "state"), "done" | "failed") {
                continue;
            }
            let last = self
                .timeline
                .rows
                .iter()
                .rposition(|row| row.message == message && !row.text.trim().is_empty());
            if last.is_some_and(|last| last >= self.scroll && last < end) {
                self.pending_reads
                    .push((string(member, "name").to_owned(), id.to_owned()));
                member["unread_entry_id"] = json!("");
            }
        }
    }

    pub(super) fn send_read_receipts(&mut self, bridge: &Bridge) {
        for (member, entry_id) in self.pending_reads.drain(..) {
            bridge.send(json!({"type":"read-result", "member":member, "entry_id":entry_id}));
        }
    }

    pub(super) fn open_unread_menu(&mut self) {
        let first = self
            .members
            .iter()
            .find(|m| !string(m, "unread_entry_id").is_empty())
            .map(|m| string(m, "name").to_owned());
        self.show_unread_member(first);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    #[test]
    fn results_stay_unread_in_other_views_popovers_collapsed_history_and_unfocused_terminal() {
        let mut app = App::new(100);
        app.members = vec![
            json!({"name":"A","label":"A"}),
            json!({"name":"B","label":"B","unread_entry_id":"result"}),
        ];
        app.selected = "A".into();
        app.show_all = false;
        app.on_service(json!({"type":"init","entries":[{"id":"result","sender":"B","body":"line\n\n".repeat(60),"state":"done"}]}));
        let mut terminal = Terminal::new(TestBackend::new(100, 24)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.unread_count(), 1);
        app.toggle_member_menu();
        app.selected = "B".into();
        app.timeline.invalidate();
        app.rebuild = true;
        terminal.draw(|f| app.draw(f)).unwrap();
        assert!(app.pending_reads.is_empty());
        app.toggle_member_menu();
        app.follow = false;
        app.pending_bottom = false;
        app.scroll = 0;
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.unread_count(), 1);
        app.entries[0]["collapsed"] = json!(true);
        app.timeline.invalidate();
        app.rebuild = true;
        app.follow = true;
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.unread_count(), 1);
        app.entries[0]["collapsed"] = json!(false);
        app.timeline.invalidate();
        app.rebuild = true;
        app.focused = false;
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.unread_count(), 1);
        app.focused = true;
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.unread_count(), 0);
        assert_eq!(app.pending_reads, vec![("B".into(), "result".into())]);
    }
}
