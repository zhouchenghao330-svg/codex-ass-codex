use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use serde::Deserialize;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

pub const FG: Color = Color::Rgb(218, 216, 206);
pub const MUTED: Color = Color::Rgb(147, 152, 142);
pub const ACCENT: Color = Color::Rgb(169, 182, 255);

#[derive(Deserialize)]
pub struct Fixture {
    pub history: Vec<String>,
    pub stream_blocks: Vec<String>,
    pub source_lines: usize,
}

pub struct Message {
    pub source: String,
    pub collapsed: bool,
    pub label: String,
    parsed: Vec<Line<'static>>,
}

#[derive(Clone)]
pub struct Row {
    pub line: Line<'static>,
    pub text: String,
    pub message: usize,
    pub header: bool,
}

#[derive(Default)]
pub struct History {
    pub messages: Vec<Message>,
    pub rows: Vec<Row>,
    starts: Vec<usize>,
    pub width: usize,
}

/// Cache all stable history. A stream update renders only its last message;
/// mouse movements never parse Markdown or walk the full history.
impl History {
    pub fn message_rows(&self, index: usize) -> &[Row] {
        let start = self.starts[index];
        let end = self
            .starts
            .get(index + 1)
            .copied()
            .unwrap_or(self.rows.len());
        &self.rows[start..end]
    }
    pub fn new(width: usize) -> Self {
        Self {
            width: width.max(1),
            ..Self::default()
        }
    }

    pub fn push(&mut self, source: String, label: &str) -> usize {
        let index = self.messages.len();
        self.starts.push(self.rows.len());
        let parsed = parse(&source);
        self.messages.push(Message {
            source,
            label: label.into(),
            collapsed: false,
            parsed,
        });
        self.rows.extend(self.render(index));
        index
    }

    pub fn update_tail(&mut self, source: String) {
        let Some(index) = self.messages.len().checked_sub(1) else {
            return;
        };
        self.messages[index].parsed = parse(&source);
        self.messages[index].source = source;
        self.rows.truncate(self.starts[index]);
        self.rows.extend(self.render(index));
    }

    pub fn reflow(&mut self, width: usize) {
        self.width = width.max(1);
        self.rows.clear();
        self.starts.clear();
        for index in 0..self.messages.len() {
            self.starts.push(self.rows.len());
            self.rows.extend(self.render(index));
        }
    }

    pub fn toggle(&mut self, index: usize) {
        self.messages[index].collapsed = !self.messages[index].collapsed;
        self.reflow(self.width);
    }

    fn render(&self, index: usize) -> Vec<Row> {
        let message = &self.messages[index];
        let marker = if message.collapsed { "▸" } else { "▾" };
        let header = Line::styled(
            format!("{marker} {} · m{:04}", message.label, index + 1),
            ACCENT,
        );
        let mut result = vec![row(header, index, true)];
        if message.collapsed {
            let preview = message
                .source
                .lines()
                .find(|s| !s.trim().is_empty())
                .unwrap_or("");
            result.extend(
                wrap(Line::styled(preview.to_owned(), MUTED), self.width)
                    .into_iter()
                    .take(1)
                    .map(|line| row(line, index, false)),
            );
        } else {
            for line in &message.parsed {
                result.extend(
                    wrap(line.clone(), self.width)
                        .into_iter()
                        .map(|line| row(line, index, false)),
                );
            }
        }
        result.push(row(Line::raw(""), index, false));
        result
    }
}

fn parse(source: &str) -> Vec<Line<'static>> {
    tui_markdown::from_str(source)
        .lines
        .into_iter()
        .map(|line| {
            Line::from(
                line.spans
                    .into_iter()
                    .map(|s| Span::styled(s.content.into_owned(), s.style))
                    .collect::<Vec<_>>(),
            )
            .style(line.style)
        })
        .collect()
}

fn row(line: Line<'static>, message: usize, header: bool) -> Row {
    Row {
        text: line.to_string(),
        line,
        message,
        header,
    }
}

/// Wrap at grapheme/cell boundaries, preserving styles and literal code spaces.
pub fn wrap(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    let width = width.max(1);
    let mut result = Vec::new();
    let mut spans = Vec::new();
    let mut cells = 0;
    for span in line.spans {
        let mut chunk = String::new();
        for grapheme in span.content.graphemes(true) {
            let size = grapheme.width();
            if cells > 0 && cells + size > width {
                if !chunk.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut chunk), span.style));
                }
                result.push(Line::from(std::mem::take(&mut spans)).style(line.style));
                cells = 0;
            }
            chunk.push_str(grapheme);
            cells += size;
        }
        if !chunk.is_empty() {
            spans.push(Span::styled(chunk, span.style));
        }
    }
    if !spans.is_empty() || result.is_empty() {
        result.push(Line::from(spans).style(line.style));
    }
    result
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub struct Point {
    pub row: usize,
    pub byte: usize,
}

#[derive(Clone, Copy, Debug)]
pub struct Selection {
    pub anchor: Point,
    pub head: Point,
}

impl Selection {
    pub fn ordered(&self) -> (Point, Point) {
        if self.anchor <= self.head {
            (self.anchor, self.head)
        } else {
            (self.head, self.anchor)
        }
    }

    pub fn span(&self, row: usize, len: usize) -> Option<(usize, usize)> {
        let (first, last) = self.ordered();
        if row < first.row || row > last.row {
            return None;
        }
        let start = if row == first.row {
            first.byte.min(len)
        } else {
            0
        };
        let end = if row == last.row {
            last.byte.min(len)
        } else {
            len
        };
        (start < end).then_some((start, end))
    }

    pub fn text(&self, rows: &[Row]) -> String {
        let (first, last) = self.ordered();
        if first == last || first.row >= rows.len() {
            return String::new();
        }
        let end_row = last.row.min(rows.len().saturating_sub(1));
        (first.row..=end_row)
            .map(|i| {
                let text = &rows[i].text;
                let start = if i == first.row {
                    first.byte.min(text.len())
                } else {
                    0
                };
                let end = if i == last.row {
                    last.byte.min(text.len())
                } else {
                    text.len()
                };
                text.get(start..end).unwrap_or("").to_owned()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }

    pub fn highlight(&self, index: usize, row: &Row) -> Line<'static> {
        let Some((left, right)) = self.span(index, row.text.len()) else {
            return row.line.clone();
        };
        let mut position = 0;
        let mut spans = Vec::new();
        for span in &row.line.spans {
            let text = span.content.as_ref();
            let mut cuts = vec![
                0,
                text.len(),
                left.saturating_sub(position).min(text.len()),
                right.saturating_sub(position).min(text.len()),
            ];
            cuts.sort_unstable();
            cuts.dedup();
            for pair in cuts.windows(2) {
                if let Some(part) = text.get(pair[0]..pair[1]) {
                    let style = if left <= position + pair[0] && position + pair[0] < right {
                        span.style.patch(
                            Style::default()
                                .fg(Color::White)
                                .bg(Color::Rgb(65, 90, 116)),
                        )
                    } else {
                        span.style
                    };
                    spans.push(Span::styled(part.to_owned(), style));
                }
            }
            position += text.len();
        }
        Line::from(spans).style(row.line.style)
    }
}

pub fn byte_at_cell(text: &str, column: usize) -> usize {
    let mut cells = 0;
    for (byte, grapheme) in text.grapheme_indices(true) {
        if cells + grapheme.width() > column {
            return byte;
        }
        cells += grapheme.width();
    }
    text.len()
}

pub fn word_span(text: &str, byte: usize) -> (usize, usize) {
    if byte >= text.len() {
        return (text.len(), text.len());
    }
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    if chars.is_empty() {
        return (0, 0);
    }
    let index = chars
        .iter()
        .rposition(|(offset, _)| *offset <= byte)
        .unwrap_or(0);
    let separators = " ()[]{}',\"`";
    let space = chars[index].1 == ' ';
    let mut first = index;
    let mut last = index + 1;
    while first > 0
        && if space {
            chars[first - 1].1 == ' '
        } else {
            !separators.contains(chars[first - 1].1)
        }
    {
        first -= 1;
    }
    while last < chars.len()
        && if space {
            chars[last].1 == ' '
        } else {
            !separators.contains(chars[last].1)
        }
    {
        last += 1;
    }
    (
        chars[first].0,
        chars.get(last).map(|x| x.0).unwrap_or(text.len()),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wide_cells_and_reverse_selection_are_exact() {
        assert_eq!(byte_at_cell("甲乙abc", 0), 0);
        assert_eq!(byte_at_cell("甲乙abc", 1), 0);
        assert_eq!(byte_at_cell("甲乙abc", 2), 3);
        assert_eq!(byte_at_cell("甲乙abc", 3), 3);
        let rows = vec![
            row(Line::raw("甲乙abc"), 0, false),
            row(Line::raw("下一行"), 0, false),
        ];
        let selection = Selection {
            anchor: Point { row: 1, byte: 3 },
            head: Point { row: 0, byte: 3 },
        };
        assert_eq!(selection.text(&rows), "乙abc\n下");
        assert_eq!(selection.highlight(0, &rows[0]).to_string(), "甲乙abc");
    }

    #[test]
    fn wrapping_preserves_text_styles_and_graphemes() {
        let input = Line::from(vec![Span::styled("甲乙", ACCENT), Span::raw("e\u{301}abc")]);
        let rows = wrap(input, 3);
        assert!(rows.iter().all(|r| r.width() <= 3));
        assert_eq!(
            rows.iter().map(ToString::to_string).collect::<String>(),
            "甲乙e\u{301}abc"
        );
        assert_eq!(rows[0].spans[0].style.fg, Some(ACCENT));
    }

    #[test]
    fn stream_update_keeps_stable_rows_untouched() {
        let mut history = History::new(80);
        history.push("**稳定历史**".into(), "模拟Codex");
        let first = history.rows[1].text.clone();
        history.push("新".into(), "模拟Codex");
        history.update_tail("新的流式文字".into());
        assert_eq!(history.rows[1].text, first);
        history.toggle(0);
        assert!(history.messages[0].collapsed);
        assert!(history.rows[0].text.starts_with('▸'));
    }

    #[test]
    fn double_click_uses_the_existing_terminal_word_separators() {
        let text = "取消；没有选区时 Ctrl+C";
        let (start, end) = word_span(text, "取消；".len());
        assert_eq!(&text[start..end], "取消；没有选区时");
    }
}
