use ratatui::{
    style::{Color, Style},
    text::{Line, Span},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
pub const FG: Color = Color::Rgb(218, 216, 206);
pub const MUTED: Color = Color::Rgb(147, 152, 142);
pub const ACCENT: Color = Color::Rgb(169, 182, 255);
pub const BG: Color = Color::Rgb(30, 33, 31);
#[derive(Clone)]
pub struct Row {
    pub line: Line<'static>,
    pub text: String,
    pub message: usize,
    pub action: String,
    pub blocks: Vec<String>,
    pub full: bool,
    pub working: bool,
    pub source_start: Option<usize>,
    pub table: Option<crate::table_copy::TableLine>,
}
pub fn parse(source: &str, width: usize) -> Vec<Line<'static>> {
    // Tables must wrap their cells before the outer line wrapper runs; wrapping
    // a natural-width table as plain text splits its borders and columns apart.
    let options =
        tui_markdown::Options::default().table_width(width.clamp(1, u16::MAX as usize) as u16);
    tui_markdown::from_str_with_options(&clean(source), &options)
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

/// Wrap at grapheme/cell boundaries, preserving styles and literal code spaces.
pub fn wrap(line: Line<'static>, width: usize) -> Vec<Line<'static>> {
    let width = width.max(1);
    let mut result = Vec::new();
    let mut spans = Vec::new();
    let mut cells = 0;
    for span in line.spans {
        let mut chunk = String::new();
        let safe = clean(&span.content);
        for grapheme in safe.graphemes(true) {
            if grapheme == "\n" {
                if !chunk.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut chunk), span.style));
                }
                result.push(Line::from(std::mem::take(&mut spans)).style(line.style));
                cells = 0;
                continue;
            }
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
/// Treat tool output as text, never as terminal commands (OSC 52, cursor moves,
/// screen clears). Preserve ordinary Unicode, newlines and readable tab spacing.
pub(crate) fn clean(text: &str) -> String {
    let mut out = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            match chars.next() {
                Some('[') => {
                    for next in chars.by_ref() {
                        if ('@'..='~').contains(&next) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    while let Some(next) = chars.next() {
                        if next == '\x07' {
                            break;
                        }
                        if next == '\x1b' && chars.peek() == Some(&'\\') {
                            chars.next();
                            break;
                        }
                    }
                }
                _ => {}
            }
        } else if c == '\t' {
            out.push(' ');
        } else if c == '\n' || !c.is_control() {
            out.push(c);
        }
    }
    out
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
    fn terminal_controls_cannot_escape_tool_output() {
        let rows = wrap(
            Line::from(Span::raw(
                "before\x1b[2J\x1b]52;c;secret\x07after\nnext".to_owned(),
            )),
            80,
        );
        assert_eq!(
            rows.iter().map(ToString::to_string).collect::<Vec<_>>(),
            vec!["beforeafter", "next"]
        );
        assert!(!rows.iter().any(|r| r.to_string().contains('\x1b')));
    }
}
