use crate::history::{self, ACCENT, FG, MUTED, Row};
use ratatui::{
    style::{Color, Modifier, Style},
    text::{Line, Span},
};
use serde_json::Value;
use std::collections::{HashMap, HashSet};

pub fn string<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
pub fn color(v: &str) -> Color {
    if v.len() == 7 && v.starts_with('#') {
        if let Ok(n) = u32::from_str_radix(&v[1..], 16) {
            return Color::Rgb((n >> 16) as u8, (n >> 8) as u8, n as u8);
        }
    }
    ACCENT
}
pub fn duration(seconds: f64) -> String {
    let n = seconds.max(0.) as u64;
    if n >= 3600 {
        format!("{}h {}m {}s", n / 3600, n / 60 % 60, n % 60)
    } else if n >= 60 {
        format!("{}m {}s", n / 60, n % 60)
    } else {
        format!("{n}s")
    }
}
#[derive(Default)]
pub struct Timeline {
    pub rows: Vec<Row>,
    pub width: usize,
    cache: HashMap<String, Vec<Row>>,
    order: Vec<String>,
    starts: Vec<usize>,
    pub groups: HashSet<String>,
    pub details: HashSet<String>,
    pub all_details: HashSet<String>,
}
// Reflow by text position within a message block, rather than retaining an old
// screen-row number when centered reading changes the wrap width.
pub struct ScrollAnchor {
    message: usize,
    blocks: Vec<String>,
    action: String,
    occurrence: usize,
    offset: usize,
}
impl ScrollAnchor {
    fn matches(&self, row: &Row) -> bool {
        self.message == row.message && self.blocks == row.blocks && self.action == row.action
    }
}
impl Timeline {
    pub fn new(width: usize) -> Self {
        Self {
            width: width.max(1),
            ..Self::default()
        }
    }
    pub fn invalidate(&mut self) {
        self.cache.clear();
    }
    pub fn anchor(&self, index: usize) -> Option<ScrollAnchor> {
        let row = self.rows.get(index)?;
        let mut anchor = ScrollAnchor {
            message: row.message,
            blocks: row.blocks.clone(),
            action: row.action.clone(),
            occurrence: 0,
            offset: 0,
        };
        let start = self.rows[..index]
            .iter()
            .rposition(|r| !anchor.matches(r))
            .map_or(0, |n| n + 1);
        anchor.offset = self.rows[start..index].iter().map(|r| r.text.len()).sum();
        anchor.occurrence = self.rows[..start]
            .iter()
            .enumerate()
            .filter(|(i, r)| anchor.matches(r) && (*i == 0 || !anchor.matches(&self.rows[i - 1])))
            .count();
        Some(anchor)
    }
    pub fn locate(&self, anchor: &ScrollAnchor) -> Option<usize> {
        let mut seen = 0;
        let mut index = 0;
        while index < self.rows.len() {
            if !anchor.matches(&self.rows[index]) {
                index += 1;
                continue;
            }
            let start = index;
            while index < self.rows.len() && anchor.matches(&self.rows[index]) {
                index += 1;
            }
            if seen == anchor.occurrence {
                let mut offset = 0;
                for n in start..index {
                    offset += self.rows[n].text.len();
                    if offset > anchor.offset {
                        return Some(n);
                    }
                }
                return Some(index - 1);
            }
            seen += 1;
        }
        None
    }
    pub fn rebuild(
        &mut self,
        entries: &[Value],
        members: &[Value],
        selected: &str,
        show_all: bool,
        changed: &HashSet<String>,
    ) {
        let visible = entries.iter().enumerate().filter(|(_, e)| {
            if e["hidden"].as_bool() == Some(true) {
                return false;
            }
            // An input can be steered before the first visible output. Its empty
            // shell stays in storage but should not become a blank chat card.
            if string(e, "state") == "continued"
                && string(e, "body").is_empty()
                && e["blocks"].as_array().is_none_or(|b| b.is_empty())
            {
                return false;
            }
            if show_all {
                return true;
            }
            string(e, "sender") == selected
                || string(e, "target") == selected
                || (string(e, "sender") == "system" && string(e, "target").is_empty())
        });
        let mut seen = HashSet::new();
        let mut next = Vec::new();
        let mut rebuilt = HashSet::new();
        let changed_batches = entries
            .iter()
            .filter(|e| changed.contains(string(e, "id")))
            .map(|e| string(e, "batch_id"))
            .filter(|s| !s.is_empty())
            .collect::<HashSet<_>>();
        for (i, e) in visible {
            let batch = string(e, "batch_id");
            if show_all
                && string(e, "sender") == "you"
                && !batch.is_empty()
                && !seen.insert(batch.to_owned())
            {
                continue;
            }
            let id = string(e, "id").to_owned();
            if changed.contains(&id)
                || changed_batches.contains(string(e, "batch_id"))
                || !self.cache.contains_key(&id)
            {
                let rows = self.render(e, i, members, show_all, entries);
                self.cache.insert(id.clone(), rows);
                rebuilt.insert(id.clone());
            }
            next.push(id);
        }
        let prefix = self
            .order
            .iter()
            .zip(&next)
            .take_while(|(a, b)| a == b && !rebuilt.contains(*a))
            .count();
        if prefix < self.order.len() || prefix < next.len() {
            let start = self.starts.get(prefix).copied().unwrap_or(self.rows.len());
            self.rows.truncate(start);
            self.starts.truncate(prefix);
            for id in next.iter().skip(prefix) {
                self.starts.push(self.rows.len());
                self.rows.extend(self.cache[id].iter().cloned());
            }
        }
        self.order = next;
    }
    fn render(
        &self,
        e: &Value,
        index: usize,
        members: &[Value],
        show_all: bool,
        entries: &[Value],
    ) -> Vec<Row> {
        let mut rows = Vec::new();
        let id = string(e, "id");
        let sender = string(e, "sender");
        let member_name = if sender == "you" {
            string(e, "target")
        } else {
            sender
        };
        let member = members
            .iter()
            .find(|m| string(m, "name") == member_name)
            .unwrap_or(&Value::Null);
        let label = string(member, "label");
        let accent = color(string(member, "color"));
        let mut push = |line: Line<'static>,
                        action: String,
                        blocks: Vec<String>,
                        full: bool,
                        working: bool| {
            rows.push(Row {
                text: line.to_string(),
                line,
                message: index,
                action,
                blocks,
                full,
                working,
                source_start: None,
                table: None,
            });
        };
        if sender == "system" {
            for l in string(e, "body").lines() {
                push(
                    Line::styled(format!("◇ {l}"), MUTED),
                    "meta".into(),
                    vec![],
                    false,
                    false,
                );
            }
        } else if e["communication"].is_object() {
            let info = &e["communication"];
            let target = members
                .iter()
                .find(|m| string(m, "name") == string(e, "target"));
            let target_label = target
                .map(|m| string(m, "label"))
                .unwrap_or(string(info, "target_label"));
            let collapsed = e["collapsed"].as_bool().unwrap_or(false);
            push(
                Line::from(vec![
                    Span::styled(
                        format!("{} @{label}", if collapsed { "▸" } else { "▾" }),
                        accent,
                    ),
                    Span::styled(" → ", MUTED),
                    Span::styled(
                        format!("@{target_label}"),
                        target.map(|m| color(string(m, "color"))).unwrap_or(MUTED),
                    ),
                    Span::styled(
                        match string(info, "delivery_mode") {
                            "queue" => " · Queue",
                            "steer" => " · Steer",
                            _ => "",
                        },
                        MUTED,
                    ),
                    Span::styled(
                        format!(
                            " · {} · {}{}",
                            string(info, "action"),
                            string(e, "time"),
                            if string(info, "transport") == "room" {
                                match string(info, "delivery") {
                                    "queued" => " · 待投递",
                                    "delivering" => " · 投递中",
                                    "delivered" => " · 已送达",
                                    "steered" => " · 已追加",
                                    "uncertain" => " · 投递未确认",
                                    "failed" => " · 投递失败",
                                    "stopped" => " · 已停止",
                                    _ => "",
                                }
                            } else if string(e, "state") == "failed" {
                                " · 失败"
                            } else if string(e, "state") == "working" {
                                " · 发送中"
                            } else if string(e, "state") == "stopped" {
                                " · 已停止"
                            } else {
                                ""
                            }
                        ),
                        MUTED,
                    ),
                ]),
                format!("collapse:{id}"),
                vec![],
                collapsed,
                false,
            );
            if info["content_available"].as_bool() != Some(true) {
                push(
                    Line::styled("Codex 未提供这条通信的正文", MUTED),
                    "meta".into(),
                    vec![],
                    true,
                    false,
                );
            } else if collapsed {
                push(
                    history::parse(string(e, "body").lines().next().unwrap_or(""), self.width)
                        .into_iter()
                        .next()
                        .unwrap_or_default(),
                    format!("collapse:{id}"),
                    vec!["message".into()],
                    true,
                    false,
                );
            } else {
                for line in history::parse(string(e, "body"), self.width) {
                    push(line, String::new(), vec!["message".into()], false, false);
                }
            }
            if !string(info, "delivery_error").is_empty() {
                push(
                    Line::styled(
                        format!("投递状态：{}", string(info, "delivery_error")),
                        MUTED,
                    ),
                    "meta".into(),
                    vec![],
                    true,
                    false,
                );
            }
        } else if sender == "you" {
            let labels = if show_all && !string(e, "batch_id").is_empty() {
                entries
                    .iter()
                    .filter(|x| string(x, "batch_id") == string(e, "batch_id"))
                    .map(|x| {
                        let n = string(x, "target");
                        let l = members
                            .iter()
                            .find(|m| string(m, "name") == n)
                            .map(|m| string(m, "label"))
                            .unwrap_or(n);
                        format!(
                            "@{l} {}",
                            match string(x, "state") {
                                "queued" => "待追加",
                                "steered" => "已追加",
                                "failed" => "失败",
                                "stopped" => "已停止",
                                _ => "",
                            }
                        )
                    })
                    .collect::<Vec<_>>()
                    .join(" ")
            } else {
                format!(
                    "@{} {}",
                    label,
                    match string(e, "state") {
                        "queued" => "待追加",
                        "steered" => "已追加",
                        "failed" => "失败",
                        "stopped" => "已停止",
                        _ => "",
                    }
                )
            };
            push(
                Line::from(vec![Span::styled(format!("› {labels} "), accent)]),
                "meta".into(),
                vec![],
                false,
                false,
            );
            let body = e["display_body"].as_str().unwrap_or(string(e, "body"));
            let mut offset = 0;
            for l in body.split('\n') {
                push(
                    Line::styled(l.to_owned(), FG),
                    format!("userbody:{offset}"),
                    vec![],
                    false,
                    false,
                );
                offset += l.len() + 1;
            }
            if !e["quote"].is_null() {
                push(
                    Line::styled("↪ 附有转发引用（点击查看）", MUTED),
                    format!("quote:{id}"),
                    vec![],
                    false,
                    false,
                );
            }
        } else {
            let collapsed = e["collapsed"].as_bool().unwrap_or(false);
            let cwd = string(member, "cwd");
            let parent = members
                .iter()
                .find(|m| string(m, "name") == string(member, "parent_name"));
            let heading = Line::from(vec![
                Span::styled(
                    format!("{} {label}  @{label}", if collapsed { "▸" } else { "▾" }),
                    Style::default().fg(accent).add_modifier(Modifier::BOLD),
                ),
                Span::styled(format!(" · {cwd}"), MUTED),
                Span::styled(
                    parent
                        .map(|p| format!(" · ↳ @{}", string(p, "label")))
                        .unwrap_or_default(),
                    MUTED,
                ),
            ]);
            push(heading, format!("collapse:{id}"), vec![], collapsed, false);
            if collapsed {
                let text = string(e, "body")
                    .lines()
                    .find(|l| !l.trim().is_empty())
                    .unwrap_or("…");
                push(
                    Line::styled(text.to_owned(), MUTED),
                    format!("collapse:{id}"),
                    vec![],
                    true,
                    false,
                );
            } else if let Some(blocks) = e["blocks"].as_array().filter(|b| !b.is_empty()) {
                let mut at = 0;
                while at < blocks.len() {
                    if string(&blocks[at], "kind") == "text" {
                        for line in history::parse(string(&blocks[at], "text"), self.width) {
                            push(
                                line,
                                String::new(),
                                vec![string(&blocks[at], "id").into()],
                                false,
                                false,
                            );
                        }
                        at += 1;
                    } else {
                        let first = at;
                        while at < blocks.len() && string(&blocks[at], "kind") != "text" {
                            at += 1;
                        }
                        let group = &blocks[first..at];
                        let key = format!("{id}:{first}");
                        let open = self.groups.contains(&key);
                        let ids = group
                            .iter()
                            .map(|b| string(b, "id").to_owned())
                            .collect::<Vec<_>>();
                        let commands = group
                            .iter()
                            .filter(|b| string(b, "kind") == "command")
                            .count();
                        let tools = group.iter().filter(|b| string(b, "kind") == "tool").count();
                        let files = group
                            .iter()
                            .flat_map(|b| b["files"].as_array().into_iter().flatten())
                            .collect::<Vec<_>>();
                        let added: i64 = files.iter().filter_map(|f| f["added"].as_i64()).sum();
                        let deleted: i64 = files.iter().filter_map(|f| f["deleted"].as_i64()).sum();
                        let failed = group
                            .iter()
                            .filter(|b| string(b, "status") == "failed")
                            .count();
                        let mut bits = Vec::new();
                        if commands > 0 {
                            bits.push(format!("{commands} 命令"));
                        }
                        if tools > 0 {
                            bits.push(format!("{tools} 工具"));
                        }
                        if !files.is_empty() {
                            bits.push(format!("{} 文件 +{added} -{deleted}", files.len()));
                        }
                        if failed > 0 {
                            bits.push(format!("{failed} 项失败"));
                        }
                        push(
                            Line::styled(
                                format!(
                                    "{} {}  （点击{} · Ctrl+T）",
                                    if open { "▾" } else { "▸" },
                                    bits.join(" · "),
                                    if open { "收起" } else { "展开" }
                                ),
                                if failed > 0 {
                                    Color::Rgb(232, 184, 120)
                                } else {
                                    MUTED
                                },
                            ),
                            format!("tools:{key}"),
                            ids.clone(),
                            false,
                            false,
                        );
                        if open {
                            let all = self.all_details.contains(&key);
                            push(
                                Line::styled(
                                    if all {
                                        "  仅看摘要"
                                    } else {
                                        "  全部详情"
                                    },
                                    ACCENT,
                                ),
                                format!("all:{key}"),
                                ids.clone(),
                                false,
                                false,
                            );
                            for (offset, b) in group.iter().enumerate() {
                                let itemkey = format!("{id}:{}", first + offset);
                                let expanded = all || self.details.contains(&itemkey);
                                let kind = string(b, "kind");
                                let status = match string(b, "status") {
                                    "inProgress" => "工作中",
                                    "failed" => "失败",
                                    "stopped" => "停止",
                                    _ => "完成",
                                };
                                let title = if kind == "file" {
                                    file_title(b)
                                } else {
                                    format!(
                                        "{} · {} · {status}",
                                        if kind == "command" {
                                            "命令"
                                        } else {
                                            "工具"
                                        },
                                        string(b, "title")
                                    )
                                };
                                let line = if kind == "command" {
                                    shell_line(&format!(
                                        "  {} {title}",
                                        if expanded { "▾" } else { "▸" }
                                    ))
                                } else {
                                    Line::styled(
                                        format!("  {} {title}", if expanded { "▾" } else { "▸" }),
                                        if kind == "file" {
                                            Color::Rgb(232, 184, 120)
                                        } else {
                                            ACCENT
                                        },
                                    )
                                };
                                push(
                                    line,
                                    format!("detail:{itemkey}"),
                                    vec![string(b, "id").into()],
                                    false,
                                    false,
                                );
                                if expanded {
                                    if kind == "file" {
                                        if let Some(files) = b["files"].as_array() {
                                            for f in files {
                                                push(
                                                    Line::styled(
                                                        format!(
                                                            "    {} {} +{} -{}",
                                                            string(f, "action"),
                                                            string(f, "path"),
                                                            f["added"],
                                                            f["deleted"]
                                                        ),
                                                        MUTED,
                                                    ),
                                                    String::new(),
                                                    vec![string(b, "id").into()],
                                                    false,
                                                    false,
                                                );
                                                for l in string(f, "diff").split('\n') {
                                                    push(
                                                        diff_line(l),
                                                        String::new(),
                                                        vec![string(b, "id").into()],
                                                        false,
                                                        false,
                                                    );
                                                }
                                            }
                                        }
                                    } else {
                                        for l in string(b, "text").split('\n') {
                                            push(
                                                Line::styled(l.to_owned(), MUTED),
                                                String::new(),
                                                vec![string(b, "id").into()],
                                                false,
                                                false,
                                            );
                                        }
                                    }
                                } else {
                                    let text = string(b, "text");
                                    let preview = text.lines().next().unwrap_or("");
                                    let trimmed = preview.chars().take(140).collect::<String>();
                                    let suffix = if text.lines().count() > 1 {
                                        format!(" …（{} 行）", text.lines().count())
                                    } else {
                                        String::new()
                                    };
                                    push(
                                        Line::styled(format!("    输出：{trimmed}{suffix}"), MUTED),
                                        format!("detail:{itemkey}"),
                                        vec![string(b, "id").into()],
                                        false,
                                        false,
                                    );
                                }
                            }
                        }
                    }
                }
            } else {
                for line in history::parse(string(e, "body"), self.width) {
                    push(line, String::new(), vec![], false, false);
                }
            }
            if string(e, "state") == "working" {
                push(Line::raw("Working"), String::new(), vec![], false, true);
            } else if let Some(seconds) = e["duration_seconds"].as_f64() {
                let finished = string(e, "finished_at");
                let time = finished.get(11..16).unwrap_or(string(e, "time"));
                push(
                    Line::styled(
                        format!(
                            "Worked for {} · {time}{}",
                            duration(seconds),
                            match string(e, "state") {
                                "failed" => " · 失败",
                                "stopped" => " · 已停止",
                                _ => "",
                            }
                        ),
                        MUTED,
                    ),
                    "meta".into(),
                    vec![],
                    false,
                    false,
                );
            }
        }
        push(Line::raw(""), "meta".into(), vec![], false, false);
        // Cache table-to-cell mappings with the layout, never while dragging.
        // Tool output and fenced code keep their literal clipboard contents.
        if sender != "you" && sender != "system" && e["collapsed"].as_bool() != Some(true) {
            if e["communication"].is_object() {
                crate::table_copy::attach(
                    string(e, "body"),
                    rows.iter_mut()
                        .filter(|r| r.blocks.iter().any(|id| id == "message")),
                );
            } else if let Some(blocks) = e["blocks"].as_array().filter(|b| !b.is_empty()) {
                for block in blocks.iter().filter(|b| string(b, "kind") == "text") {
                    crate::table_copy::attach(
                        string(block, "text"),
                        rows.iter_mut()
                            .filter(|r| r.blocks.iter().any(|id| id == string(block, "id"))),
                    );
                }
            } else {
                crate::table_copy::attach(
                    string(e, "body"),
                    rows.iter_mut()
                        .filter(|r| r.action.is_empty() && !r.working),
                );
            }
        }
        // Map before the final wrapper: an extremely narrow pane can even wrap
        // a table's borders. Slice the map along with those outer line breaks.
        let mut rows = rows
            .into_iter()
            .flat_map(|row| {
                let mut offset = 0;
                history::wrap(row.line, self.width)
                    .into_iter()
                    .map(move |line| {
                        let text = line.to_string();
                        let table = row
                            .table
                            .as_ref()
                            .map(|table| table.slice(offset..offset + text.len()));
                        offset += text.len();
                        Row {
                            line,
                            text,
                            table,
                            message: row.message,
                            action: row.action.clone(),
                            blocks: row.blocks.clone(),
                            full: row.full,
                            working: row.working,
                            source_start: None,
                        }
                    })
            })
            .collect::<Vec<_>>();
        let mut previous = None;
        let mut offset = 0;
        for row in &mut rows {
            if let Some(start) = row
                .action
                .strip_prefix("userbody:")
                .and_then(|s| s.parse::<usize>().ok())
            {
                if previous != Some(start) {
                    offset = start;
                    previous = Some(start);
                }
                row.source_start = Some(offset);
                offset += row.text.len();
            }
        }
        rows
    }
}
fn file_title(b: &Value) -> String {
    b["files"]
        .as_array()
        .map(|files| {
            files
                .iter()
                .map(|f| {
                    format!(
                        "{} {} +{} -{}",
                        string(f, "action"),
                        string(f, "path"),
                        f["added"],
                        f["deleted"]
                    )
                })
                .collect::<Vec<_>>()
                .join(" · ")
        })
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| format!("文件 · {}", string(b, "title")))
}
fn diff_line(line: &str) -> Line<'static> {
    Line::styled(
        line.to_owned(),
        if line.starts_with('+') {
            Color::Rgb(120, 216, 188)
        } else if line.starts_with('-') {
            Color::Rgb(231, 154, 181)
        } else if line.starts_with("@@") {
            ACCENT
        } else {
            FG
        },
    )
}
fn shell_line(text: &str) -> Line<'static> {
    let spans = text
        .split_inclusive(' ')
        .enumerate()
        .map(|(i, s)| {
            Span::styled(
                s.to_owned(),
                if s.trim_start().starts_with('-') {
                    Color::Rgb(232, 184, 120)
                } else if s.starts_with('"') || s.starts_with('\'') {
                    Color::Rgb(120, 216, 188)
                } else if i == 4 {
                    Color::Rgb(131, 200, 232)
                } else {
                    FG
                },
            )
        })
        .collect::<Vec<_>>();
    Line::from(spans)
}
