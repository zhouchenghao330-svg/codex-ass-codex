use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(Default)]
pub struct Input {
    pub text: String,
    pub cursor: usize,
}

impl Input {
    pub fn insert(&mut self, text: &str) {
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
    }
    pub fn left(&mut self) {
        if let Some((index, _)) = self.text[..self.cursor].grapheme_indices(true).next_back() {
            self.cursor = index;
        }
    }
    pub fn right(&mut self) {
        if let Some(grapheme) = self.text[self.cursor..].graphemes(true).next() {
            self.cursor += grapheme.len();
        }
    }
    pub fn backspace(&mut self) {
        let end = self.cursor;
        self.left();
        self.text.replace_range(self.cursor..end, "");
    }
    pub fn delete(&mut self) {
        let start = self.cursor;
        self.right();
        self.text.replace_range(start..self.cursor, "");
        self.cursor = start;
    }
    pub fn take(&mut self) -> String {
        self.cursor = 0;
        std::mem::take(&mut self.text)
    }
    pub fn location(&self, width: usize) -> (usize, usize) {
        let width = width.max(1);
        let (mut row, mut column) = (0, 0);
        for grapheme in self.text[..self.cursor].graphemes(true) {
            if grapheme == "\n" {
                row += 1;
                column = 0;
                continue;
            }
            if column > 0 && column + grapheme.width() > width {
                row += 1;
                column = 0;
            }
            column += grapheme.width();
        }
        if column >= width {
            row += 1;
            column = 0;
        }
        (row, column)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn edits_and_cursor_respect_wide_graphemes() {
        let mut input = Input::default();
        input.insert("甲👨‍👩‍👧‍👦e\u{301}");
        input.backspace();
        assert_eq!(input.text, "甲👨‍👩‍👧‍👦");
        input.left();
        input.delete();
        assert_eq!(input.text, "甲");
        input.insert("乙a");
        assert_eq!(input.location(4), (1, 1));
    }
}
