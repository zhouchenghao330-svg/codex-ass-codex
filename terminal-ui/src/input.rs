use serde_json::{Value, json};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Attachment {
    pub start: usize,
    pub end: usize,
    pub kind: String,
    pub content: String,
    pub label: String,
}
#[derive(Clone, Default)]
pub struct Snapshot {
    pub text: String,
    pub cursor: usize,
    pub attachments: Vec<Attachment>,
}
#[derive(Clone, Default)]
pub struct Input {
    pub state: Snapshot,
    undo: Vec<Snapshot>,
    redo: Vec<Snapshot>,
}
impl std::ops::Deref for Input {
    type Target = Snapshot;
    fn deref(&self) -> &Snapshot {
        &self.state
    }
}
impl std::ops::DerefMut for Input {
    fn deref_mut(&mut self) -> &mut Snapshot {
        &mut self.state
    }
}
pub fn byte_at_char(text: &str, index: usize) -> usize {
    text.char_indices()
        .nth(index)
        .map(|(i, _)| i)
        .unwrap_or(text.len())
}
impl Input {
    pub fn from_prompt(value: &Value) -> Self {
        let text = value
            .as_str()
            .or_else(|| value["text"].as_str())
            .unwrap_or("")
            .to_owned();
        let attachments = value["attachments"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|a| {
                let start = byte_at_char(&text, a["start"].as_u64()? as usize);
                let end = byte_at_char(&text, a["end"].as_u64()? as usize);
                let label = a["label"].as_str()?.to_owned();
                if start >= end || text.get(start..end)? != label {
                    return None;
                }
                Some(Attachment {
                    start,
                    end,
                    kind: a["kind"].as_str()?.into(),
                    content: a["content"].as_str()?.into(),
                    label,
                })
            })
            .collect();
        Self {
            state: Snapshot {
                cursor: text.len(),
                text,
                attachments,
            },
            ..Self::default()
        }
    }
    pub fn prompt(&self) -> Value {
        if self.attachments.is_empty() {
            return json!(self.text);
        }
        json!({"text": self.text, "attachments":self.attachments.iter().map(|a|json!({
            "start": self.text[..a.start].chars().count(), "end":self.text[..a.end].chars().count(),
            "kind":a.kind,"content":a.content,"label":a.label
        })).collect::<Vec<_>>()})
    }
    fn checkpoint(&mut self) {
        self.undo.push(self.state.clone());
        if self.undo.len() > 100 {
            self.undo.remove(0);
        }
        self.redo.clear();
    }
    fn replace_range(&mut self, mut start: usize, mut end: usize, text: &str) {
        for a in &self.attachments {
            if (start < a.end && end > a.start)
                || (a.start < start && start == end && start < a.end)
            {
                start = start.min(a.start);
                end = end.max(a.end);
            }
        }
        let delta = text.len() as isize - (end - start) as isize;
        self.state
            .attachments
            .retain(|a| a.end <= start || a.start >= end);
        for a in &mut self.state.attachments {
            if a.start >= end {
                a.start = a.start.saturating_add_signed(delta);
                a.end = a.end.saturating_add_signed(delta);
            }
        }
        self.state.text.replace_range(start..end, text);
        self.state.cursor = start + text.len();
    }
    pub fn insert(&mut self, text: &str) {
        self.checkpoint();
        let c = self.cursor;
        self.replace_range(c, c, text);
    }
    pub fn completion(
        &mut self,
        start: usize,
        end: usize,
        replacement: &str,
        cursor: usize,
    ) -> bool {
        if !self.text.is_char_boundary(start)
            || !self.text.is_char_boundary(end)
            || end < start
            || self
                .attachments
                .iter()
                .any(|a| start < a.end && end > a.start)
        {
            return false;
        }
        self.checkpoint();
        self.replace_range(start, end, replacement);
        self.set_cursor(cursor);
        true
    }
    pub fn paste(&mut self, text: &str, images: &[String]) {
        self.checkpoint();
        let c = self.cursor;
        if !images.is_empty() {
            let mut next = self
                .attachments
                .iter()
                .filter(|a| a.kind == "image")
                .count()
                + 1;
            let mut display = String::new();
            let mut attachments = Vec::new();
            for path in images {
                if !display.is_empty() {
                    display.push(' ');
                }
                let label = format!("[Image #{next}]");
                next += 1;
                let start = c + display.len();
                display.push_str(&label);
                attachments.push(Attachment {
                    start,
                    end: c + display.len(),
                    kind: "image".into(),
                    content: path.clone(),
                    label,
                });
            }
            self.replace_range(c, c, &display);
            self.state.attachments.extend(attachments);
        } else if text.chars().count() >= 1000 || text.lines().count() >= 8 {
            let label = format!("[Pasted Content {} chars]", text.chars().count());
            self.replace_range(c, c, &label);
            self.state.attachments.push(Attachment {
                start: c,
                end: c + label.len(),
                kind: "paste".into(),
                content: text.into(),
                label,
            });
        } else {
            self.replace_range(c, c, text);
        }
        self.state.attachments.sort_by_key(|a| a.start);
    }
    pub fn left(&mut self) {
        if let Some(a) = self
            .attachments
            .iter()
            .find(|a| a.start < self.cursor && self.cursor <= a.end)
        {
            self.state.cursor = a.start;
            return;
        }
        if let Some((index, _)) = self.text[..self.cursor].grapheme_indices(true).next_back() {
            self.state.cursor = index;
        }
    }
    pub fn right(&mut self) {
        if let Some(a) = self
            .attachments
            .iter()
            .find(|a| a.start <= self.cursor && self.cursor < a.end)
        {
            self.state.cursor = a.end;
            return;
        }
        if let Some(g) = self.text[self.cursor..].graphemes(true).next() {
            self.state.cursor += g.len();
        }
    }
    pub fn backspace(&mut self) {
        self.checkpoint();
        let end = self.cursor;
        self.left();
        let start = self.cursor;
        self.replace_range(start, end, "");
    }
    pub fn delete(&mut self) {
        self.checkpoint();
        let start = self.cursor;
        self.right();
        let end = self.cursor;
        self.replace_range(start, end, "");
    }
    pub fn clear(&mut self) {
        self.checkpoint();
        self.state = Snapshot::default();
    }
    pub fn undo(&mut self) {
        if let Some(s) = self.undo.pop() {
            let old = std::mem::replace(&mut self.state, s);
            self.redo.push(old);
        }
    }
    pub fn redo(&mut self) {
        if let Some(s) = self.redo.pop() {
            let old = std::mem::replace(&mut self.state, s);
            self.undo.push(old);
        }
    }
    pub fn expand(&mut self) {
        if let Some(a) = self
            .attachments
            .iter()
            .find(|a| a.kind == "paste" && a.start <= self.cursor && self.cursor <= a.end)
            .cloned()
        {
            self.checkpoint();
            self.replace_range(a.start, a.end, &a.content);
        }
    }
    pub fn set_cursor(&mut self, mut cursor: usize) {
        cursor = cursor.min(self.text.len());
        while !self.text.is_char_boundary(cursor) {
            cursor -= 1;
        }
        if let Some(a) = self
            .attachments
            .iter()
            .find(|a| a.start < cursor && cursor < a.end)
        {
            cursor = a.end;
        }
        self.state.cursor = cursor;
    }
    pub fn location(&self, width: usize) -> (usize, usize) {
        location(&self.text[..self.cursor], width)
    }
    pub fn vertical(&mut self, width: usize, delta: isize) {
        let (row, column) = self.location(width);
        let target = row.saturating_add_signed(delta);
        let mut best = self.cursor;
        let mut distance = usize::MAX;
        for (byte, r, c) in positions(&self.text, width) {
            if r == target && c.abs_diff(column) < distance {
                best = byte;
                distance = c.abs_diff(column);
            }
            if r > target {
                break;
            }
        }
        self.set_cursor(best);
    }
    pub fn cursor_at(&mut self, width: usize, row: usize, column: usize) {
        let best = positions(&self.text, width)
            .into_iter()
            .min_by_key(|(_, r, c)| (r.abs_diff(row), c.abs_diff(column)))
            .map(|p| p.0)
            .unwrap_or(0);
        self.set_cursor(best);
    }
    pub fn slice(&self, mut start: usize, mut end: usize) -> Self {
        for a in &self.attachments {
            if start < a.end && end > a.start {
                start = start.min(a.start);
                end = end.max(a.end);
            }
        }
        let mut result = Self::default();
        if let Some(text) = self.text.get(start..end) {
            result.state.text = text.into();
            result.state.cursor = text.len();
            result.state.attachments = self
                .attachments
                .iter()
                .filter(|a| start <= a.start && a.end <= end)
                .cloned()
                .map(|mut a| {
                    a.start -= start;
                    a.end -= start;
                    a
                })
                .collect();
        }
        result
    }
    pub fn expanded_text(&self) -> String {
        let mut text = self.text.clone();
        for a in self.attachments.iter().rev().filter(|a| a.kind == "paste") {
            text.replace_range(a.start..a.end, &a.content);
        }
        text
    }
    pub fn full_text(&self) -> String {
        let mut text = self.text.clone();
        for a in self.attachments.iter().rev() {
            text.replace_range(a.start..a.end, &a.content);
        }
        text
    }
}
pub fn location(text: &str, width: usize) -> (usize, usize) {
    let width = width.max(1);
    let (mut row, mut column) = (0, 0);
    for g in text.graphemes(true) {
        if g == "\n" {
            row += 1;
            column = 0;
            continue;
        }
        if column > 0 && column + (if g == "\t" { 1 } else { g.width() }) > width {
            row += 1;
            column = 0;
        }
        column += if g == "\t" { 1 } else { g.width() };
    }
    if column >= width {
        row += 1;
        column = 0;
    }
    (row, column)
}
fn positions(text: &str, width: usize) -> Vec<(usize, usize, usize)> {
    let width = width.max(1);
    let (mut row, mut column) = (0, 0);
    let mut result = Vec::new();
    for (byte, g) in text.grapheme_indices(true) {
        if g != "\n" && column > 0 && column + (if g == "\t" { 1 } else { g.width() }) > width {
            row += 1;
            column = 0;
        }
        result.push((byte, row, column));
        if g == "\n" {
            row += 1;
            column = 0;
        } else {
            column += if g == "\t" { 1 } else { g.width() };
        }
    }
    if column >= width {
        row += 1;
        column = 0;
    }
    result.push((text.len(), row, column));
    result
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn completing_recipient_before_an_attachment_preserves_payload_and_undo() {
        let mut input = Input::default();
        input.insert("@a ");
        input.paste(&"原文".repeat(3000), &[]);
        input.set_cursor(2);
        assert!(input.completion(0, 2, "@开发", "@开发 ".len()));
        assert_eq!(input.prompt()["attachments"][0]["start"], 4);
        assert!(input.expanded_text().ends_with(&"原文".repeat(3000)));
        input.undo();
        assert!(input.text.starts_with("@a "));
        assert_eq!(input.attachments.len(), 1);
    }
    #[test]
    fn click_and_vertical_motion_respect_wrapped_wide_input() {
        let mut input = Input::default();
        input.insert("甲乙ab\n丙丁cd");
        input.state.cursor = 0;
        input.vertical(6, 1);
        assert_eq!(input.cursor, "甲乙ab\n".len());
        input.cursor_at(6, 1, 4);
        assert_eq!(input.cursor, "甲乙ab\n丙丁".len());
        input.insert("\t");
        assert_eq!(input.location(6), (1, 5));
    }
    #[test]
    fn unicode_and_atomic_pastes_roundtrip() {
        let mut input = Input::default();
        input.insert("中文👨‍👩‍👧‍👦");
        input.backspace();
        let long = "甲".repeat(6000);
        input.paste(&long, &[]);
        let saved = input.prompt();
        let restored = Input::from_prompt(&saved);
        assert_eq!(restored.full_text(), format!("中文{long}"));
        input.backspace();
        assert_eq!(input.text, "中文");
        input.undo();
        assert_eq!(input.prompt(), saved);
        input.expand();
        assert_eq!(input.text.chars().count(), 6002);
        assert!(input.attachments.is_empty());
    }
    #[test]
    fn inserts_and_deletes_shift_unicode_attachment_offsets() {
        let mut input = Input::default();
        input.insert("中文");
        input.paste("", &["/tmp/image.png".into()]);
        input.state.cursor = 0;
        input.insert("前");
        assert_eq!(input.prompt()["attachments"][0]["start"], 3);
        input.state.cursor = 0;
        input.delete();
        assert_eq!(input.prompt()["attachments"][0]["start"], 2);
        input.state.cursor = input.text.len();
        input.left();
        input.delete();
        assert_eq!(input.text, "中文");
    }
}
