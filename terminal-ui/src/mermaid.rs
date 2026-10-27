//! Render complete Mermaid fences without changing stored Markdown or its containers.
use super::{ACCENT, FG, MUTED};
use pulldown_cmark::{CodeBlockKind, Event, Parser, Tag, TagEnd};
use ratatui::text::{Line, Span};
use std::{borrow::Cow, ops::Range};
use unicode_width::UnicodeWidthStr;

pub(super) struct Prepared<'a> {
    pub markdown: Cow<'a, str>,
    blocks: Vec<Diagram>,
}
struct Diagram {
    marker: String,
    prefix: String,
    lines: Vec<Line<'static>>,
}
impl Prepared<'_> {
    pub fn expand(&self, line: Line<'static>) -> Vec<Line<'static>> {
        if self.blocks.is_empty() {
            return vec![line];
        }
        let text = line.to_string();
        for block in &self.blocks {
            if let Some(at) = text.find(&block.marker) {
                let prefix = if at == 0 { &block.prefix } else { &text[..at] };
                return block
                    .lines
                    .iter()
                    .map(|content| {
                        let mut spans = vec![Span::styled(prefix.to_owned(), MUTED)];
                        spans.extend(content.spans.clone());
                        Line::from(spans)
                    })
                    .collect();
            }
        }
        vec![line]
    }
}

fn closed(source: &str, range: &Range<usize>, content_end: usize) -> bool {
    let block = &source[range.clone()];
    let Some(marker @ (b'`' | b'~')) = block.as_bytes().first().copied() else {
        return false;
    };
    let count = block.bytes().take_while(|c| *c == marker).count();
    source[content_end..range.end]
        .trim_end_matches([' ', '\t', '\r', '\n'])
        .bytes()
        .rev()
        .take_while(|c| *c == marker)
        .count()
        >= count
}

fn render_diagram(
    content: &str,
    width: usize,
) -> Result<Vec<Line<'static>>, codex_mermaid::RenderError> {
    let (diagram, reflowed) = match codex_mermaid::render_spans(content, width) {
        Err(codex_mermaid::RenderError::TooWide) => {
            match codex_mermaid::render_vertical_spans(content, width) {
                Ok(diagram) => (diagram, true),
                Err(_) => return Err(codex_mermaid::RenderError::TooWide),
            }
        }
        Ok(diagram) => (diagram, false),
        Err(error) => return Err(error),
    };
    let mut lines = Vec::new();
    if reflowed {
        lines.push(Line::styled(
            "Mermaid：横向布局超出宽度，已改为纵向显示",
            MUTED,
        ));
    }
    lines.extend(diagram.into_iter().map(|line| {
        Line::from(
            line.into_iter()
                .map(|span| {
                    let color = match span.role {
                        codex_mermaid::Role::Node => ACCENT,
                        codex_mermaid::Role::Edge => MUTED,
                        codex_mermaid::Role::Text => FG,
                    };
                    Span::styled(span.text, color)
                })
                .collect::<Vec<_>>(),
        )
    }));
    Ok(lines)
}

pub(super) fn prepare(source: &str, width: usize) -> Prepared<'_> {
    let mut prepared = Prepared {
        markdown: Cow::Borrowed(source),
        blocks: Vec::new(),
    };
    if !source.contains("mermaid") {
        return prepared;
    }
    let mut events = Parser::new_ext(source, super::parser_options()).into_offset_iter();
    let mut edits = Vec::new();
    while let Some((event, range)) = events.next() {
        let Event::Start(Tag::CodeBlock(CodeBlockKind::Fenced(info))) = event else {
            continue;
        };
        if info.split([',', ' ', '\t']).next() != Some("mermaid") {
            continue;
        }
        let mut content = String::new();
        let mut content_end = range.start;
        for (event, offset) in events.by_ref() {
            match event {
                Event::Text(text) => {
                    content.push_str(&text);
                    content_end = offset.end;
                }
                Event::End(TagEnd::CodeBlock) => break,
                _ => {}
            }
        }
        // CommonMark synthesizes End at EOF, including during streaming.
        if !closed(source, &range, content_end) {
            continue;
        }
        let line_start = source[..range.start].rfind('\n').map_or(0, |n| n + 1);
        let prefix = &source[line_start..range.start];
        // A list marker occurs only on the opening line. Preserve quote marks
        // and turn list markers into continuation indentation on later lines.
        let continuation = prefix
            .chars()
            .map(|c| if c == '>' { '>' } else { ' ' })
            .collect::<String>();
        let inset = if prefix.is_empty() {
            0
        } else {
            prefix.width() + 4
        };
        let lines = match render_diagram(&content, width.saturating_sub(inset)) {
            Ok(lines) => lines,
            Err(error) => {
                let reason = match error {
                    codex_mermaid::RenderError::TooWide => {
                        "当前宽度不足，已显示源码；可加宽窗口或关闭 /adhd"
                    }
                    codex_mermaid::RenderError::Unsupported => "暂不支持这段语法，已显示源码",
                    codex_mermaid::RenderError::Limit => "超出终端图形的大小限制，已显示源码",
                };
                let mut lines = vec![Line::styled(format!("Mermaid：{reason}"), MUTED)];
                lines.extend(content.lines().map(|s| Line::styled(s.to_owned(), FG)));
                lines
            }
        };
        // A single placeholder lets the Markdown renderer retain list/quote
        // context. Expand it afterwards: its unknown-language code path merges
        // separate Text events in nested fences and would flatten a diagram.
        let mut marker = format!("ROOM_MERMAID_{}", range.start);
        while source.contains(&marker) {
            marker.push('_');
        }
        let mut replacement = format!("~~~~text\n{continuation}{marker}\n{continuation}~~~~");
        if source[range.clone()].ends_with('\n') {
            replacement.push('\n');
        }
        edits.push((range, replacement));
        prepared.blocks.push(Diagram {
            marker,
            prefix: continuation,
            lines,
        });
    }
    if edits.is_empty() {
        return prepared;
    }
    let mut result = String::new();
    let mut at = 0;
    for (range, replacement) in edits {
        result.push_str(&source[at..range.start]);
        result.push_str(&replacement);
        at = range.end;
    }
    result.push_str(&source[at..]);
    prepared.markdown = Cow::Owned(result);
    prepared
}

#[cfg(test)]
mod tests {
    use super::*;
    fn display(source: &str, width: usize) -> String {
        super::super::parse(source, width)
            .iter()
            .map(|l| l.to_string())
            .collect::<Vec<_>>()
            .join("\n")
    }

    #[test]
    fn native_diagram_families_render_and_keep_surrounding_markdown() {
        for source in [
            "flowchart LR\nA[请求] --> B[回复]",
            "sequenceDiagram\nA->>B: request\nB-->>A: response",
            "stateDiagram-v2\n[*] --> Active\nActive --> [*]",
            "classDiagram\nOrder --> Item",
            "erDiagram\nCUSTOMER ||--o{ ORDER : places",
        ] {
            let markdown = format!("前文。\n\n```mermaid\n{source}\n```\n\n**后文。**");
            let output = display(&markdown, 160);
            assert!(
                output.contains("前文。") && output.contains("后文。"),
                "{output}"
            );
            assert!(
                !output.contains("Mermaid：") && !output.contains("```"),
                "{output}"
            );
            assert!(output.contains('─'), "{output}");
        }
    }

    #[test]
    fn streaming_and_literal_mermaid_stay_source_until_a_real_closing_fence() {
        let source = "````mermaid\nflowchart LR\nA[请求] --> B[回复]\n````\n";
        let closing = source.rfind("````").unwrap() + 4;
        for end in (0..closing).filter(|n| source.is_char_boundary(*n)) {
            assert!(
                matches!(prepare(&source[..end], 100).markdown, Cow::Borrowed(_)),
                "prefix {end}"
            );
        }
        assert!(display(source, 100).contains('┌'));
        for literal in [
            "````text\n```mermaid\nflowchart LR\nA --> B\n```\n````",
            "    ```mermaid\n    flowchart LR\n    A --> B\n    ```",
            "`mermaid` is a language name.",
        ] {
            assert!(matches!(prepare(literal, 100).markdown, Cow::Borrowed(_)));
        }
    }

    #[test]
    fn nested_quotes_lists_tilde_fences_and_multiple_diagrams_preserve_their_context() {
        let source = "> ~~~mermaid\n> flowchart LR\n> A[请求] --> B[回复]\n> ~~~\n\n- 项目\n\n  ```mermaid\n  flowchart TD\n  C[审核] --> D[完成]\n  ```\n\n[链接][ref]\n\n[ref]: https://example.com/context\n";
        let output = display(source, 100);
        for expected in [
            "请求",
            "回复",
            "审核",
            "完成",
            "项目",
            "https://example.com/context",
        ] {
            assert!(output.contains(expected), "{expected}: {output}");
        }
        assert!(
            !output.contains("flowchart") && !output.contains("Mermaid："),
            "{output}"
        );
        assert!(output.lines().all(|l| l.width() <= 100), "{output}");
    }

    #[test]
    fn fallback_keeps_source_for_narrow_unsupported_or_oversized_diagrams() {
        let source = include_str!("../../tests/fixtures/markdown_mermaid.md");
        let narrow = display(source, 20);
        assert!(
            narrow.contains("当前宽度不足") && narrow.contains("flowchart LR"),
            "{narrow}"
        );
        let wide = display(source, 160);
        assert!(wide.contains("用户重试") && wide.contains('┌'));
        assert!(!wide.contains("flowchart LR"));
        let unsupported = display("```mermaid\npie\n\"A\": 2\n```", 100);
        assert!(unsupported.contains("暂不支持") && unsupported.contains("pie"));
        let huge = format!(
            "```mermaid\nflowchart LR\nA[{}]\n```",
            "x".repeat(17 * 1024)
        );
        assert!(display(&huge, 100).contains("大小限制"));
    }

    #[test]
    fn equipment_multiline_graph_preserves_all_nodes_labels_and_relations() {
        let source = include_str!("../../tests/fixtures/markdown_mermaid_equipment.md");
        for width in [77, 92, 117, 157] {
            let output = display(source, width);
            assert!(output.contains("已改为纵向显示"), "{output}");
            assert!(
                !output.contains("已显示源码") && !output.contains("<br"),
                "{output}"
            );
            assert_eq!(
                output.lines().filter(|line| line.starts_with('┌')).count(),
                12
            );
            for text in [
                "equipment",
                "装备",
                "remaining_life",
                "剩余寿命：NUMERIC",
                "usage_life_unit",
                "寿命单位",
                "year=年",
                "hour=小时",
                "mileage=里程数",
                "factory_name",
                "登记厂家名称",
                "factory.name",
                "关联厂家名称",
                "fault",
                "故障",
                "fault_workorder",
                "workorder",
                "工单",
                "workorder_person",
                "person",
                "人员",
                "工单状态规则",
                "0新建、1维修中、2已完成",
                "has_field",
                "requires",
                "value_domain",
                "historical_label_of",
                "source_text",
            ] {
                assert!(output.contains(text), "missing {text}: {output}");
            }
            assert_eq!(output.matches("has_field").count(), 2);
            assert!(output.lines().all(|line| line.width() <= width));
        }
        let narrow = display(source, 35);
        assert!(narrow.contains("当前宽度不足") && narrow.contains("<br/>"));
        let wide = display(source, 2000);
        assert!(!wide.contains("Mermaid：") && !wide.contains("<br/>"));
        assert_eq!(source.matches("<br/>").count(), 13);
    }

    #[test]
    fn multiline_edge_labels_fit_without_losing_their_text_or_direction() {
        let source = include_str!("../../tests/fixtures/markdown_mermaid_edge_labels.md");
        for direction in ["LR", "RL", "TD", "BT"] {
            for width in [77, 92, 157] {
                let output = display(
                    &source.replace("flowchart LR", &format!("flowchart {direction}")),
                    width,
                );
                for expected in [
                    "装备节点",
                    "工单节点",
                    "描述、字段清单、字段含义",
                    "关联边",
                    "描述、方向、路径、去重规则、映射",
                ] {
                    assert!(output.contains(expected), "{output}");
                }
                assert!(
                    !output.contains("已显示源码") && !output.contains("<br/>"),
                    "{output}"
                );
                assert!(output.lines().all(|line| line.width() <= width));
            }
        }
    }
}
