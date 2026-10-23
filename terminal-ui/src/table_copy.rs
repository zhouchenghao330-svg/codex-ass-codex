//! Clipboard coordinates for Markdown tables. The screen is a grid, but the
//! clipboard contains logical rows (tabs between cells), not drawing glyphs.
//! Mappings are built once with the layout and shared by its physical rows.
use crate::history::{self, Row, Selection};
use pulldown_cmark::{Alignment, Event, Options, Parser, Tag, TagEnd};
use std::{collections::BTreeMap, ops::Range, sync::Arc};
use tui_markdown::StyleSheet;
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

#[derive(Clone)]
pub struct TableLine {
    table: Arc<Table>,
    fragments: Vec<Fragment>,
}

impl TableLine {
    pub fn slice(&self, display: Range<usize>) -> Self {
        let fragments = self
            .fragments
            .iter()
            .filter_map(|fragment| {
                let start = display.start.max(fragment.display.start);
                let end = display.end.min(fragment.display.end);
                if start < end
                    || (fragment.source.is_empty() && display.contains(&start) && start == end)
                {
                    Some(Fragment {
                        cell: fragment.cell,
                        display: start - display.start..end - display.start,
                        source: fragment.source.start + start - fragment.display.start
                            ..fragment.source.start + end - fragment.display.start,
                    })
                } else {
                    None
                }
            })
            .collect();
        Self {
            table: self.table.clone(),
            fragments,
        }
    }
}

struct Table {
    cells: Vec<Vec<String>>,
}

#[derive(Clone)]
struct Fragment {
    cell: (usize, usize),
    display: Range<usize>,
    source: Range<usize>,
}

struct ParsedTable {
    alignments: Vec<Alignment>,
    cells: Vec<Vec<String>>,
}

fn tables(source: &str) -> Vec<ParsedTable> {
    // Match tui-markdown's parser options, including references and GFM links.
    let options = Options::ENABLE_STRIKETHROUGH
        | Options::ENABLE_TASKLISTS
        | Options::ENABLE_HEADING_ATTRIBUTES
        | Options::ENABLE_YAML_STYLE_METADATA_BLOCKS
        | Options::ENABLE_SUPERSCRIPT
        | Options::ENABLE_SUBSCRIPT
        | Options::ENABLE_MATH
        | Options::ENABLE_FOOTNOTES
        | Options::ENABLE_DEFINITION_LIST
        | Options::ENABLE_GFM
        | Options::ENABLE_TABLES;
    let parser = Parser::new_ext(source, options);
    let mut definitions = parser
        .reference_definitions()
        .iter()
        .filter_map(|(_, definition)| source.get(definition.span.clone()))
        .collect::<Vec<_>>()
        .join("\n");
    let events = parser.into_offset_iter().collect::<Vec<_>>();
    for (event, range) in &events {
        if matches!(event, Event::Start(Tag::FootnoteDefinition(_))) {
            definitions.push('\n');
            definitions.push_str(&source[range.clone()]);
        }
    }
    let mut result = Vec::new();
    let mut table: Option<ParsedTable> = None;
    for (event, range) in events {
        match event {
            Event::Start(Tag::Table(alignments)) => {
                table = Some(ParsedTable {
                    alignments,
                    cells: Vec::new(),
                });
            }
            Event::Start(Tag::TableHead | Tag::TableRow) => {
                if let Some(table) = table.as_mut() {
                    table.cells.push(Vec::new());
                }
            }
            Event::Start(Tag::TableCell) => {
                if let Some(row) = table.as_mut().and_then(|t| t.cells.last_mut()) {
                    // Render a single unwrapped cell through the SAME renderer.
                    // This retains entities, escaped pipes, code, image fallbacks
                    // and reference-link destinations without reimplementing them.
                    let markdown = format!("| {} |\n| --- |\n\n{}", &source[range], definitions);
                    let rendered = tui_markdown::from_str(&markdown);
                    let cell = rendered
                        .lines
                        .iter()
                        .find(|line| line.to_string().starts_with('│'));
                    let text = cell
                        .map(|line| {
                            line.spans[2..line.spans.len() - 2]
                                .iter()
                                .map(|span| span.content.as_ref())
                                .collect::<String>()
                        })
                        .unwrap_or_default();
                    row.push(text);
                }
            }
            Event::End(TagEnd::Table) => {
                if let Some(table) = table.take() {
                    result.push(table);
                }
            }
            _ => {}
        }
    }
    result
}

/// Byte ranges of visible fragments. Whitespace discarded at a word wrap stays
/// in the logical cell, so copying across that wrap restores the original space.
fn wrap_cell(text: &str, width: usize) -> Vec<Range<usize>> {
    if text.width() <= width {
        return vec![0..text.len()];
    }
    let graphemes = text.grapheme_indices(true).collect::<Vec<_>>();
    let mut result = Vec::new();
    let mut at = 0;
    while at < graphemes.len() {
        let mut used = 0;
        let mut boundary = None;
        let mut end = graphemes.len();
        for (i, (_, grapheme)) in graphemes.iter().enumerate().skip(at) {
            if i > at && grapheme.chars().all(char::is_whitespace) {
                boundary = Some(i);
            }
            used += grapheme.width();
            if used > width && i > at {
                end = boundary.unwrap_or(i);
                break;
            }
        }
        let byte_end = graphemes.get(end).map(|g| g.0).unwrap_or(text.len());
        result.push(graphemes[at].0..byte_end);
        at = end;
        while at < graphemes.len() && graphemes[at].1.chars().all(char::is_whitespace) {
            at += 1;
        }
    }
    result
}

/// Validate every cell against the actual layout before installing a map. A
/// literal box drawn in prose/code must never acquire Markdown-table semantics.
fn map_table(table: &ParsedTable, rows: &[&mut Row], start: usize) -> Option<Vec<TableLine>> {
    let top = &rows[start];
    let border = top.line.spans.iter().find(|span| {
        span.content.starts_with('┌')
            && span.content.ends_with('┐')
            && span.style == tui_markdown::DefaultStyleSheet.table_border()
    })?;
    let widths = border
        .content
        .strip_prefix('┌')?
        .strip_suffix('┐')?
        .split('┬')
        .map(|part| {
            if !part.chars().all(|c| c == '─') {
                return None;
            }
            part.chars().count().checked_sub(2)
        })
        .collect::<Option<Vec<_>>>()?;
    if widths.len() != table.alignments.len() {
        return None;
    }
    let prefix = top.text[..top.text.find('┌')?].width();
    let data = Arc::new(Table {
        cells: table.cells.clone(),
    });
    let border_line = || TableLine {
        table: data.clone(),
        fragments: Vec::new(),
    };
    let mut mapped = vec![border_line()];
    let mut at = start + 1;
    for (logical_row, cells) in table.cells.iter().enumerate() {
        let wrapped = cells
            .iter()
            .zip(&widths)
            .map(|(text, &width)| wrap_cell(text, width))
            .collect::<Vec<_>>();
        let height = wrapped.iter().map(Vec::len).max().unwrap_or(1);
        for physical_row in 0..height {
            let row = rows.get(at)?;
            let mut fragments = Vec::new();
            let mut column = prefix;
            for (cell, &width) in widths.iter().enumerate() {
                let left = history::byte_at_cell(&row.text, column);
                let right = history::byte_at_cell(&row.text, column + width + 3);
                let segment = row.text.get(left..right)?;
                let range = wrapped
                    .get(cell)
                    .and_then(|ranges| ranges.get(physical_row))
                    .cloned();
                let text = range
                    .as_ref()
                    .map(|r| &cells[cell][r.clone()])
                    .unwrap_or("");
                let padding = width.saturating_sub(text.width());
                let pad_left = match table.alignments[cell] {
                    Alignment::Right => padding,
                    Alignment::Center => padding / 2,
                    _ => 0,
                };
                let expected = format!(
                    "│ {}{}{} ",
                    " ".repeat(pad_left),
                    text,
                    " ".repeat(padding - pad_left)
                );
                if segment != expected {
                    return None;
                }
                if let Some(source) = range {
                    let content = left + '│'.len_utf8() + 1 + pad_left;
                    fragments.push(Fragment {
                        cell: (logical_row, cell),
                        display: content..content + text.len(),
                        source,
                    });
                }
                column += width + 3;
            }
            if row.text.get(history::byte_at_cell(&row.text, column)..)? != "│" {
                return None;
            }
            mapped.push(TableLine {
                table: data.clone(),
                fragments,
            });
            at += 1;
        }
        if logical_row == 0 {
            let separator = &rows.get(at)?.text;
            if !separator.contains('├') || !separator.ends_with('┤') {
                return None;
            }
            mapped.push(border_line());
            at += 1;
        }
    }
    let bottom = &rows.get(at)?.text;
    if !bottom.contains('└') || !bottom.ends_with('┘') {
        return None;
    }
    mapped.push(border_line());
    Some(mapped)
}

pub fn attach<'a>(source: &str, rows: impl Iterator<Item = &'a mut Row>) {
    if !source.contains('|') {
        return;
    }
    let source = history::clean(source);
    let tables = tables(&source);
    if tables.is_empty() {
        return;
    }
    let mut rows = rows.collect::<Vec<_>>();
    let mut next = 0;
    for table in tables {
        for start in next..rows.len() {
            if let Some(mapped) = map_table(&table, &rows, start) {
                next = start + mapped.len();
                for (row, mapping) in rows[start..next].iter_mut().zip(mapped) {
                    row.table = Some(mapping);
                }
                break;
            }
        }
    }
}

/// Used by clipboard and partial forwarding alike. Never includes unselected
/// cells/characters; multiple screen fragments of one cell become one range.
pub fn selected_text<'a>(
    selection: Selection,
    rows: impl Iterator<Item = (usize, &'a Row)>,
) -> String {
    let mut parts = Vec::new();
    let mut current: Option<Arc<Table>> = None;
    let mut ranges: BTreeMap<(usize, usize), Range<usize>> = BTreeMap::new();
    let flush = |parts: &mut Vec<String>,
                 table: &Option<Arc<Table>>,
                 ranges: &mut BTreeMap<(usize, usize), Range<usize>>| {
        if let Some(table) = table {
            let mut lines: Vec<String> = Vec::new();
            let mut previous = None;
            for ((row, cell), range) in std::mem::take(ranges) {
                if previous == Some(row) {
                    lines.last_mut().unwrap().push('\t');
                } else {
                    lines.push(String::new());
                    previous = Some(row);
                }
                lines
                    .last_mut()
                    .unwrap()
                    .push_str(&table.cells[row][cell][range]);
            }
            if !lines.is_empty() {
                parts.push(lines.join("\n"));
            }
        }
    };
    for (index, row) in rows {
        match &row.table {
            Some(line) => {
                if current
                    .as_ref()
                    .is_none_or(|t| !Arc::ptr_eq(t, &line.table))
                {
                    flush(&mut parts, &current, &mut ranges);
                    current = Some(line.table.clone());
                }
                if let Some((a, b)) = selection.span(index, row.text.len()) {
                    for fragment in &line.fragments {
                        let start = a.max(fragment.display.start);
                        let end = b.min(fragment.display.end);
                        if start < end || (fragment.source.is_empty() && a < start && b > end) {
                            let source = (fragment.source.start + start - fragment.display.start)
                                ..(fragment.source.start + end - fragment.display.start);
                            ranges
                                .entry(fragment.cell)
                                .and_modify(|r| {
                                    r.start = r.start.min(source.start);
                                    r.end = r.end.max(source.end);
                                })
                                .or_insert(source);
                        }
                    }
                }
            }
            None => {
                flush(&mut parts, &current, &mut ranges);
                current = None;
                if let Some((a, b)) = selection.span(index, row.text.len()) {
                    parts.push(row.text[a..b].to_owned());
                } else if row.text.is_empty() {
                    parts.push(String::new());
                }
            }
        }
    }
    flush(&mut parts, &current, &mut ranges);
    parts.join("\n")
}
