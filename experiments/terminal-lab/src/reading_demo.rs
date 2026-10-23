//! Independent reading comparison. No bridge, Codex process, room or persistence.
#[allow(dead_code)]
#[path = "../../../terminal-ui/src/history.rs"]
mod history;
#[allow(dead_code)]
#[path = "../../../terminal-ui/src/table_copy.rs"]
mod table_copy;
#[path = "../../../terminal-ui/src/terminal.rs"]
mod terminal;
#[path = "../../../terminal-ui/src/wide_backend.rs"]
mod wide_backend;

use crossterm::event::{
    self, Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind,
};
use history::{ACCENT, BG, FG, MUTED, Point, Row, Selection};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Style},
    text::{Line, Span},
    widgets::{Block, Paragraph},
};
use std::{
    collections::HashMap,
    env,
    io::{self, IsTerminal, Write},
    process::{Command, Stdio},
    time::{Duration, Instant},
};
use unicode_width::UnicodeWidthStr;

const MODES: [&str; 6] = [
    "原样",
    "行阅读尺",
    "段落聚焦",
    "段落＋行",
    "窄栏＋聚焦",
    "仅窄栏居中",
];
const DESCRIPTIONS: [&str; 6] = [
    "对照组：保持普通显示；切换方案时沿用同一阅读位置。",
    "只突出当前行与相邻一行，适合逐行跟读。",
    "突出整段；列表按一条，表格按数据行，代码按代码行。",
    "整段保持清晰，当前行加浅色提示；长段收敛到附近五行。",
    "与方案 3 相同，正文最多 92 列并居中，观察短行是否更舒服。",
    "只把正文限制在 92 列并居中；保持原有配色，不加聚焦、压暗或行提示。",
];

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Role,
    User,
    Heading,
    Paragraph,
    List,
    Code,
    Table,
    Tool,
    Diff,
}
struct Unit {
    kind: Kind,
    source: String,
    accent: Color,
}
impl Unit {
    fn new(kind: Kind, source: impl Into<String>) -> Self {
        Self {
            kind,
            source: source.into(),
            accent: Color::Rgb(169, 182, 255),
        }
    }
}

fn fixture() -> Vec<Unit> {
    use Kind::*;
    let mut units = vec![
        Unit::new(User, "› @开发 先跟我讨论一下：为什么多 Agent 会重复调查同一个问题？我们不是已经有 dedup 了吗？先讲当前实现，再讨论应该改什么。"),
        Unit::new(Role, "开发  @开发 · ~/projects/agent-lab"),
        Unit::new(Heading, "当前实现，以及问题究竟发生在哪里"),
        Unit::new(Paragraph, "我们已经有一层消息去重，但它检查的是消息 ID，而不是任务的实际含义。假设 A 先读了一遍配置，随后 B 用另一条消息请求相同的信息，两条消息的 ID 不同，程序会把它们都当成新请求。因此，“有 dedup”与“不会重复调查”之间，还隔着任务边界和上下文共享这两个问题。"),
        Unit::new(Paragraph, "真正需要先弄清楚的是：我们希望避免哪一种重复？网络重试造成的重复投递，应由稳定消息 ID 处理；两个成员针对同一目标重复执行昂贵调查，则需要知道别人已经做了什么。它们看上去相似，但触发条件、有效期以及允许复用的结果都不一样。"),
        Unit::new(Heading, "可以先拆成三个判断"),
        Unit::new(List, "- **投递是否重复？** 同一条消息因网络重试再次出现，直接返回已有回执，不重新唤醒目标成员。这个判断可以完全由程序完成。"),
        Unit::new(List, "- **调查是否可以复用？** 目标相同还不够，还要核对目录、分支、文件版本和证据时效。昨天的结论不一定能回答今天的问题。"),
        Unit::new(List, "- **谁有权决定下一步？** 同伴可以提供资料与建议，但一条同伴消息本身不应被解释成用户批准修改代码。"),
        Unit::new(Tool, "▸ 3 命令 · 1 工具 · 已完成    （模拟记录，不执行命令）"),
        Unit::new(Paragraph, "例如，A 刚确认 `.env` 没有接入缓存开关。你读完这个发现，觉得它值得整理成开发经验，于是把几轮讨论转发给笔记成员。这里真正有价值的操作不是让所有成员自动插话，而是让你能选中准确的材料，并在另一个会话里继续讨论。"),
        Unit::new(Heading, "代码、表格和普通解释，需要不同的阅读粒度"),
        Unit::new(Code, "```python\ndef should_deliver(message, member):\n    # 新求助等待当前工作结束，用户补充可以立即追加。\n    if message.id in delivered_ids:\n        return 'already_delivered'\n    if member.busy and message.source == 'peer':\n        return 'queue'\n    return 'steer' if member.busy else 'start'\n```"),
        Unit::new(Table, "| 类型 | 忙碌时如何处理 | 判断依据 |\n| --- | --- | --- |\n| A | 用户直接补充：追加到当前运行 | 用户随时可以介入自己的任务 |\n| B | 同伴的新求助：排队等待 | 不把独立任务塞进正在进行的工作 |\n| C | 同一协作的补充：显式选择 | 投递方式与修改授权分别判断 |"),
        Unit::new(Paragraph, "这张表描述的是投递时机，不代表任务已经完成。一次 Codex Turn 结束、一条 Room 消息得到回复、用户交代的整个协作任务完成，是三件不同的事。我们需要在界面里让这些状态更容易分辨，而不是只显示一个统一的“完成”。"),
        Unit::new(Diff, "@@ 消息投递示意 @@\n- deliver_everything_immediately(message)\n+ enqueue_peer_request(message)\n+ preserve_user_intervention(message)"),
        Unit::new(Heading, "长段落测试：会不会又亮起一大片？"),
        Unit::new(Paragraph, "如果一个段落写得特别长，单纯把整段全部照亮，未必能帮你定位阅读位置。".to_owned() + &"你可能正在看一个关于上下文压缩的解释：压缩既要保留目标，也要保留尚未结束的依赖。工具回执、文件路径、分支状态和用户刚补充的限制，都可能影响下一步行动。即使这些信息分散在很长的一段文字里，阅读时仍然需要知道自己刚看到哪一句。".repeat(5) + "方案 2 会保留整个长段，方案 3 和 4 则只突出鼠标附近五行。把鼠标在这里上下移动，比较哪一种更容易接着读。"),
        Unit::new(Paragraph, "接下来再换一种情形：你把鼠标移开，去看旁边的文件或者与别人说句话。回到这里时，阅读位置应该还在。点左侧标记或者按空格可以锁定焦点，鼠标离开后不会切到别段。按 Esc 解锁，然后继续用鼠标阅读。"),
        Unit { kind: Role, source: "笔记  @笔记 · ~/notes/development".into(), accent: Color::Rgb(120, 216, 188) },
        Unit::new(Paragraph, "我会把这次讨论整理为一条经验：先确定重复发生在哪一层，再决定去重键、结果有效期和协作边界。笔记应保留触发场景、实际证据和适用条件，这样以后遇到相似问题时，才能知道哪些结论可以直接复用。"),
        Unit::new(Paragraph, "这里还有一个可复制的长链接，用来检查阅读效果是否干扰选字：https://example.com/notes/agent-collaboration/context-retention-and-message-deduplication#reading-demo 。拖选中文或链接，再按 ⌘C；阅读模式不会改写文字。"),
        Unit::new(Heading, "模拟流式输出（F2 开始或暂停）"),
        Unit::new(Paragraph, "新内容会追加在这里。你可以先锁定上面的段落，再按 F2，让输出在后台继续。按 End 查看末尾，Home 回到开头。"),
    ];
    units.shrink_to_fit();
    units
}

#[derive(Clone)]
struct ReadingRow {
    row: Row,
    group: u64,
    kind: Kind,
    accent: Color,
}
struct Cached {
    width: usize,
    source: String,
    rows: Vec<ReadingRow>,
}
#[derive(Clone, Copy)]
struct Anchor {
    group: u64,
    offset: usize,
}

struct Demo {
    units: Vec<Unit>,
    cache: Vec<Option<Cached>>,
    rows: Vec<ReadingRow>,
    groups: HashMap<u64, (usize, usize)>,
    mode: usize,
    dim: bool,
    strength: u8,
    body: Rect,
    text: Rect,
    tabs: Vec<Rect>,
    scroll: usize,
    focus: Option<usize>,
    locked: bool,
    mouse: Option<(u16, u16)>,
    selection: Option<Selection>,
    press: Option<Point>,
    stream: bool,
    stream_tick: usize,
    pending_layout: bool,
    layouts: usize,
    notice: String,
    quit: bool,
}

impl Demo {
    fn new(mode: usize) -> Self {
        let units = fixture();
        Self {
            cache: (0..units.len()).map(|_| None).collect(),
            units,
            rows: Vec::new(),
            groups: HashMap::new(),
            mode,
            dim: true,
            strength: 35,
            body: Rect::default(),
            text: Rect::default(),
            tabs: Vec::new(),
            scroll: 0,
            focus: None,
            locked: false,
            mouse: None,
            selection: None,
            press: None,
            stream: false,
            stream_tick: 0,
            pending_layout: true,
            layouts: 0,
            notice: String::new(),
            quit: false,
        }
    }
    fn maximum(&self) -> usize {
        self.rows.len().saturating_sub(self.body.height as usize)
    }
    fn anchor(&self, index: usize) -> Option<Anchor> {
        let row = self.rows.get(index)?;
        Some(Anchor {
            group: row.group,
            offset: index - self.groups[&row.group].0,
        })
    }
    fn locate(&self, anchor: Anchor) -> Option<usize> {
        let &(start, end) = self.groups.get(&anchor.group)?;
        Some((start + anchor.offset).min(end - 1))
    }
    fn layout(&mut self, area: Rect) {
        let width = if matches!(self.mode, 4 | 5) {
            area.width.saturating_sub(4).min(94)
        } else {
            area.width.saturating_sub(2)
        };
        let body = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + 4,
            width,
            area.height.saturating_sub(7),
        );
        let text = Rect::new(
            body.x + 2,
            body.y,
            body.width.saturating_sub(2),
            body.height,
        );
        if self.text.width != text.width {
            self.pending_layout = true;
            self.selection = None;
            self.press = None;
        }
        let old_focus = self.focus.and_then(|r| self.anchor(r));
        let old_top = self.anchor(self.scroll);
        let screen_focus = self
            .focus
            .filter(|r| *r >= self.scroll && *r < self.scroll + self.body.height as usize)
            .map(|r| r - self.scroll);
        self.body = body;
        self.text = text;
        if !self.pending_layout || self.selection.is_some() || self.press.is_some() {
            return;
        }
        let initial = self.rows.is_empty();
        self.layouts += 1;
        self.rows.clear();
        self.groups.clear();
        let width = text.width.max(1) as usize;
        for (unit_id, unit) in self.units.iter().enumerate() {
            if self.cache[unit_id]
                .as_ref()
                .is_none_or(|c| c.width != width || c.source != unit.source)
            {
                self.cache[unit_id] = Some(Cached {
                    width,
                    source: unit.source.clone(),
                    rows: render_unit(unit, unit_id, width),
                });
            }
            self.rows
                .extend(self.cache[unit_id].as_ref().unwrap().rows.iter().cloned());
        }
        for (index, row) in self.rows.iter().enumerate() {
            self.groups
                .entry(row.group)
                .and_modify(|range| range.1 = index + 1)
                .or_insert((index, index + 1));
        }
        self.focus = old_focus.and_then(|a| self.locate(a)).or_else(|| {
            if initial {
                self.rows.iter().position(|r| r.kind == Kind::Paragraph)
            } else {
                None
            }
        });
        if let (Some(y), Some(row)) = (screen_focus, self.focus) {
            self.scroll = row.saturating_sub(y);
        } else if let Some(top) = old_top.and_then(|a| self.locate(a)) {
            self.scroll = top;
        }
        self.scroll = self.scroll.min(self.maximum());
        self.pending_layout = false;
    }
    fn set_mode(&mut self, mode: usize) {
        if mode != self.mode {
            self.mode = mode;
            self.notice.clear();
        }
    }
    fn point(&self, x: u16, y: u16) -> Option<Point> {
        if !self.text.contains((x, y).into()) {
            return None;
        }
        let index = self.scroll + (y - self.text.y) as usize;
        let row = self.rows.get(index)?;
        Some(Point {
            row: index,
            byte: history::byte_at_cell(&row.row.text, (x - self.text.x) as usize),
        })
    }
    fn hover(&mut self, x: u16, y: u16) -> bool {
        self.mouse = Some((x, y));
        if self.locked || self.press.is_some() || self.selection.is_some() {
            return false;
        }
        if let Some(point) = self.point(x, y) {
            if self.rows[point.row].row.text.trim().is_empty() {
                return false;
            }
            if self.focus != Some(point.row) {
                self.focus = Some(point.row);
                return true;
            }
        } else if !self.body.contains((x, y).into()) {
            return self.focus.take().is_some();
        }
        false
    }
    fn scroll(&mut self, amount: isize) {
        self.scroll = self
            .scroll
            .saturating_add_signed(amount)
            .min(self.maximum());
        if let Some((x, y)) = self.mouse {
            self.hover(x, y);
        }
    }
    fn active_range(&self) -> Option<(usize, usize)> {
        let focus = self.focus?;
        let row = self.rows.get(focus)?;
        if matches!(self.mode, 0 | 5) {
            return None;
        }
        if self.mode == 1 {
            return Some((focus.saturating_sub(1), (focus + 2).min(self.rows.len())));
        }
        let &(start, end) = self.groups.get(&row.group)?;
        if self.mode >= 3 && end - start > 8 && !matches!(row.kind, Kind::Table | Kind::Code) {
            Some((focus.saturating_sub(2).max(start), (focus + 3).min(end)))
        } else {
            Some((start, end))
        }
    }
    fn line(&self, index: usize) -> Line<'static> {
        let row = &self.rows[index];
        let active = self.active_range();
        let focused = active.is_some_and(|(a, b)| index >= a && index < b);
        let ruler = matches!(self.mode, 1 | 3 | 4);
        let center = self.focus == Some(index) && matches!(self.mode, 1..=4);
        let tint = if focused && (!self.dim || (center && ruler)) {
            Some(if center && ruler {
                Color::Rgb(48, 56, 49)
            } else {
                Color::Rgb(39, 45, 40)
            })
        } else {
            None
        };
        let mut line = row.row.line.clone();
        let inherited = line.style.fg;
        for span in &mut line.spans {
            let foreground = span.style.fg.or(inherited).unwrap_or(FG);
            if self.mode != 0 && active.is_some() && self.dim && !focused {
                span.style = span.style.fg(dim_color(foreground, self.strength));
            }
            if let Some(bg) = tint {
                span.style = span.style.bg(bg);
            }
        }
        if let Some(bg) = tint {
            line = line.style(Style::default().bg(bg));
        }
        if let Some(selection) = self.selection {
            let mut raw = row.row.clone();
            raw.line = line;
            selection.highlight(index, &raw)
        } else {
            line
        }
    }
    fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        self.layout(area);
        frame.render_widget(Block::default().style(Style::default().fg(FG).bg(BG)), area);
        frame.render_widget(
            Line::from(vec![
                Span::styled(" ADHD 阅读实验 ", Style::default().fg(FG).bold()),
                Span::styled("· 模拟内容 · 不连接 Codex · 不保存房间", MUTED),
            ]),
            Rect::new(area.x, area.y, area.width, 1),
        );
        self.tabs.clear();
        let mut x = area.x + 1;
        for (mode, name) in MODES.iter().enumerate() {
            let label = format!(" {mode} {name} ");
            let width = (label.width() as u16).min(area.right().saturating_sub(x));
            let rect = Rect::new(x, area.y + 1, width, 1);
            self.tabs.push(rect);
            frame.render_widget(
                Line::styled(
                    label,
                    if mode == self.mode {
                        Style::default().fg(BG).bg(ACCENT).bold()
                    } else {
                        Style::default().fg(MUTED)
                    },
                ),
                rect,
            );
            x = x.saturating_add(width + 1);
            if x >= area.right() {
                break;
            }
        }
        frame.render_widget(
            Line::styled(DESCRIPTIONS[self.mode], MUTED),
            Rect::new(area.x + 1, area.y + 2, area.width.saturating_sub(2), 1),
        );
        for index in self.scroll..(self.scroll + self.body.height as usize).min(self.rows.len()) {
            let y = self.body.y + (index - self.scroll) as u16;
            let row = &self.rows[index];
            let marker = if self.focus == Some(index) && matches!(self.mode, 1..=4) {
                if self.locked { "◆ " } else { "› " }
            } else if row.kind == Kind::Role {
                "▎ "
            } else {
                "  "
            };
            frame.render_widget(
                Line::styled(
                    marker,
                    if self.locked && self.focus == Some(index) {
                        Color::Rgb(232, 184, 120)
                    } else {
                        row.accent
                    },
                ),
                Rect::new(self.body.x, y, 2.min(self.body.width), 1),
            );
            frame.render_widget(
                Paragraph::new(self.line(index)).style(Style::default().fg(FG).bg(BG)),
                Rect::new(self.text.x, y, self.text.width, 1),
            );
        }
        let state = format!(
            " {} · {} · {} · {}/{} 行{}",
            MODES[self.mode],
            if matches!(self.mode, 0 | 5) {
                "普通亮度 · 无阅读聚焦".into()
            } else if self.dim {
                format!("其他文字压暗 {}%", self.strength)
            } else {
                "只加浅底色".into()
            },
            if self.locked {
                "◆ 已锁定 · Esc 解锁"
            } else {
                "鼠标跟随 · 空格/左侧标记锁定"
            },
            self.scroll + 1,
            self.rows.len(),
            if self.stream {
                " · 流式模拟中"
            } else {
                ""
            }
        );
        frame.render_widget(
            Line::styled(
                if self.notice.is_empty() {
                    state
                } else {
                    self.notice.clone()
                },
                MUTED,
            ),
            Rect::new(area.x, area.bottom().saturating_sub(3), area.width, 1),
        );
        frame.render_widget(
            Line::styled(
                " 0–5 切方案 · D 明暗/浅底 · +/- 明暗强度 · J/K 上下段 · F2 模拟流式",
                MUTED,
            ),
            Rect::new(area.x, area.bottom().saturating_sub(2), area.width, 1),
        );
        frame.render_widget(
            Line::styled(
                " 滚轮/方向键翻阅 · 拖选后 ⌘C/Ctrl+C 复制 · Esc 取消 · Q/Ctrl+Q 退出",
                MUTED,
            ),
            Rect::new(area.x, area.bottom().saturating_sub(1), area.width, 1),
        );
    }
    fn jump(&mut self, forward: bool) {
        let Some(index) = self.focus else {
            return;
        };
        let (start, end) = self.groups[&self.rows[index].group];
        let mut next = if forward {
            end.min(self.rows.len() - 1)
        } else {
            start.saturating_sub(1)
        };
        while next > 0 && next + 1 < self.rows.len() && self.rows[next].row.text.is_empty() {
            next = if forward { next + 1 } else { next - 1 };
        }
        self.focus = Some(next);
        self.locked = true;
        self.selection = None;
        if next < self.scroll || next >= self.scroll + self.body.height as usize {
            self.scroll = next
                .saturating_sub(self.body.height as usize / 3)
                .min(self.maximum());
        }
    }
    fn selected_text(&self) -> String {
        self.selection
            .map(|s| {
                let (first, last) = s.ordered();
                table_copy::selected_text(
                    s,
                    self.rows
                        .iter()
                        .enumerate()
                        .skip(first.row)
                        .take(last.row - first.row + 1)
                        .map(|(n, r)| (n, &r.row)),
                )
            })
            .unwrap_or_default()
    }
    fn copy(&mut self) {
        let text = self.selected_text();
        if text.is_empty() {
            self.notice = "先拖选文字；阅读锁定本身不是文字选区".into();
            return;
        }
        let (program, args): (&str, &[&str]) = if cfg!(target_os = "macos") {
            ("pbcopy", &[])
        } else if cfg!(windows) {
            ("clip.exe", &[])
        } else if env::var_os("WAYLAND_DISPLAY").is_some() {
            ("wl-copy", &[])
        } else {
            ("xclip", &["-selection", "clipboard"])
        };
        let result = (|| -> io::Result<()> {
            let mut child = Command::new(program)
                .args(args)
                .stdin(Stdio::piped())
                .spawn()?;
            child.stdin.take().unwrap().write_all(text.as_bytes())?;
            if !child.wait()?.success() {
                return Err(io::Error::other("clipboard helper failed"));
            }
            Ok(())
        })();
        self.notice = match result {
            Ok(()) => format!("已复制 {} 字符", text.chars().count()),
            Err(e) => format!("复制失败：{e}"),
        };
    }
    fn event(&mut self, event: Event) -> bool {
        match event {
            Event::Mouse(mouse) => match mouse.kind {
                MouseEventKind::Moved => return self.hover(mouse.column, mouse.row),
                MouseEventKind::ScrollUp => self.scroll(-1),
                MouseEventKind::ScrollDown => self.scroll(1),
                MouseEventKind::Down(MouseButton::Left) => {
                    if let Some(mode) = self
                        .tabs
                        .iter()
                        .position(|r| r.contains((mouse.column, mouse.row).into()))
                    {
                        self.set_mode(mode);
                        return true;
                    }
                    if self.body.contains((mouse.column, mouse.row).into())
                        && mouse.column < self.text.x
                    {
                        let row = self.scroll + (mouse.row - self.body.y) as usize;
                        if row < self.rows.len() {
                            self.locked = !(self.locked && self.focus == Some(row));
                            self.focus = Some(row);
                        }
                        return true;
                    }
                    self.selection = None;
                    self.press = self.point(mouse.column, mouse.row);
                }
                MouseEventKind::Drag(MouseButton::Left) => {
                    if let Some(anchor) = self.press {
                        if let Some(head) = self.point(mouse.column, mouse.row) {
                            self.selection = Some(Selection { anchor, head });
                        }
                    }
                }
                MouseEventKind::Up(MouseButton::Left) => {
                    self.press = None;
                    if self.selection.is_some_and(|s| s.anchor == s.head) {
                        self.selection = None;
                    }
                }
                _ => return false,
            },
            Event::Key(key) if key.kind != KeyEventKind::Release => {
                let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
                let copy = key
                    .modifiers
                    .intersects(KeyModifiers::CONTROL | KeyModifiers::SUPER | KeyModifiers::META)
                    && matches!(key.code, KeyCode::Char('c' | 'C'));
                if copy {
                    if ctrl && self.selection.is_none() {
                        self.quit = true;
                    } else {
                        self.copy();
                    }
                    return true;
                }
                self.notice.clear();
                match key.code {
                    KeyCode::Char('q') => self.quit = true,
                    KeyCode::Char(c @ '0'..='5') => self.set_mode((c as u8 - b'0') as usize),
                    KeyCode::Char('d' | 'D') => self.dim = !self.dim,
                    KeyCode::Char('+' | '=') => self.strength = (self.strength + 5).min(65),
                    KeyCode::Char('-') => self.strength = self.strength.saturating_sub(5).max(10),
                    KeyCode::Char(' ') => {
                        if self.focus.is_none() {
                            self.focus = (self.scroll..self.rows.len())
                                .find(|&n| !self.rows[n].row.text.trim().is_empty());
                        }
                        self.locked = !self.locked;
                    }
                    KeyCode::Char('j' | 'J') => self.jump(true),
                    KeyCode::Char('k' | 'K') => self.jump(false),
                    KeyCode::Up => self.scroll(-1),
                    KeyCode::Down => self.scroll(1),
                    KeyCode::PageUp => self.scroll(-(self.body.height.saturating_sub(2) as isize)),
                    KeyCode::PageDown => self.scroll(self.body.height.saturating_sub(2) as isize),
                    KeyCode::Home => self.scroll = 0,
                    KeyCode::End => self.scroll = self.maximum(),
                    KeyCode::Esc => {
                        if self.selection.is_some() {
                            self.selection = None;
                            self.press = None;
                        } else {
                            self.locked = false;
                        }
                    }
                    KeyCode::F(2) => self.stream = !self.stream,
                    _ => return false,
                }
            }
            Event::Resize(_, _) => {
                self.selection = None;
                self.press = None;
                self.pending_layout = true;
            }
            _ => return false,
        }
        true
    }
    fn tick(&mut self) {
        self.stream_tick += 1;
        let text = "模拟输出：消息在后台继续到达，锁定的位置不会被追底带走。 ";
        let part = text
            .chars()
            .skip((self.stream_tick - 1) % text.chars().count())
            .take(1)
            .collect::<String>();
        self.units.last_mut().unwrap().source.push_str(&part);
        self.pending_layout = true;
    }
}

fn dim_color(color: Color, amount: u8) -> Color {
    let rgb = match color {
        Color::Rgb(r, g, b) => (r, g, b),
        Color::Black | Color::DarkGray => (120, 126, 120),
        Color::Red | Color::LightRed => (231, 154, 181),
        Color::Green | Color::LightGreen => (120, 216, 188),
        Color::Blue | Color::LightBlue => (131, 180, 232),
        Color::Yellow | Color::LightYellow => (232, 184, 120),
        Color::Cyan | Color::LightCyan => (131, 200, 200),
        Color::Magenta | Color::LightMagenta => (190, 170, 225),
        _ => (218, 216, 206),
    };
    let mix = |v: u8, bg: u8| {
        ((u16::from(v) * (100 - u16::from(amount)) + u16::from(bg) * u16::from(amount)) / 100) as u8
    };
    Color::Rgb(mix(rgb.0, 30), mix(rgb.1, 33), mix(rgb.2, 31))
}

fn render_unit(unit: &Unit, id: usize, width: usize) -> Vec<ReadingRow> {
    let lines = if matches!(
        unit.kind,
        Kind::Role | Kind::Heading | Kind::Tool | Kind::Diff | Kind::User
    ) {
        unit.source
            .lines()
            .map(|line| {
                Line::styled(
                    line.to_owned(),
                    match unit.kind {
                        Kind::Role => Style::default().fg(unit.accent).bold(),
                        Kind::Heading => Style::default().fg(FG).bold(),
                        Kind::Tool => Style::default().fg(MUTED),
                        Kind::Diff if line.starts_with('+') => {
                            Style::default().fg(Color::Rgb(120, 216, 188))
                        }
                        Kind::Diff if line.starts_with('-') => {
                            Style::default().fg(Color::Rgb(231, 154, 181))
                        }
                        _ => Style::default().fg(FG),
                    },
                )
            })
            .collect()
    } else {
        history::parse(&unit.source, width)
    };
    let mut raw = lines
        .into_iter()
        .map(|line| Row {
            text: line.to_string(),
            line,
            message: id,
            action: String::new(),
            blocks: Vec::new(),
            full: false,
            working: false,
            source_start: None,
            table: None,
        })
        .collect::<Vec<_>>();
    if unit.kind == Kind::Table {
        table_copy::attach(&unit.source, raw.iter_mut());
    }
    let mut output = Vec::new();
    let base = (id as u64) << 32;
    let mut table_row = 0;
    for (line_index, row) in raw.into_iter().enumerate() {
        if unit.kind == Kind::Table
            && row.text.starts_with('│')
            && row
                .text
                .split('│')
                .nth(1)
                .is_some_and(|s| !s.trim().is_empty())
        {
            table_row += 1;
        }
        let group = base
            + if matches!(unit.kind, Kind::Code | Kind::Diff) {
                line_index as u64
            } else if unit.kind == Kind::Table {
                table_row
            } else {
                0
            };
        let mut offset = 0;
        for line in history::wrap(row.line, width) {
            let text = line.to_string();
            let table = row
                .table
                .as_ref()
                .map(|t| t.slice(offset..offset + text.len()));
            offset += text.len();
            output.push(ReadingRow {
                row: Row {
                    line,
                    text,
                    table,
                    message: id,
                    action: String::new(),
                    blocks: Vec::new(),
                    full: false,
                    working: false,
                    source_start: None,
                },
                group,
                kind: unit.kind,
                accent: unit.accent,
            });
        }
    }
    output.push(ReadingRow {
        row: Row {
            line: Line::raw(""),
            text: String::new(),
            message: id,
            action: String::new(),
            blocks: Vec::new(),
            full: false,
            working: false,
            source_start: None,
            table: None,
        },
        group: base + u32::MAX as u64,
        kind: unit.kind,
        accent: unit.accent,
    });
    output
}

fn run(mode: usize) -> io::Result<()> {
    let (mut terminal, _guard) = terminal::setup(true)?;
    let mut demo = Demo::new(mode);
    let mut dirty = true;
    let mut last_frame = Instant::now() - Duration::from_secs(1);
    let mut last_tick = Instant::now();
    while !demo.quit {
        if demo.stream && last_tick.elapsed() >= Duration::from_millis(70) {
            demo.tick();
            dirty = true;
            last_tick = Instant::now();
        }
        if event::poll(Duration::from_millis(8))? {
            dirty |= demo.event(event::read()?);
        }
        if dirty && last_frame.elapsed() >= Duration::from_millis(16) {
            terminal.draw(|frame| demo.draw(frame))?;
            dirty = false;
            last_frame = Instant::now();
        }
    }
    Ok(())
}

fn main() {
    let args = env::args().skip(1).collect::<Vec<_>>();
    if args.iter().any(|s| s == "--help" || s == "-h") {
        println!(
            "Codex Room ADHD reading demo\n\n0 原样 · 1 行阅读尺 · 2 段落聚焦 · 3 段落＋行 · 4 窄栏聚焦 · 5 仅窄栏居中\nD 切换明暗/浅底 · +/- 强度 · 空格锁定 · J/K 上下段\nF2 模拟流式 · 鼠标拖选 · Cmd+C 复制 · Q/Ctrl+Q 退出\n\n不调用 Codex，不读取或写入房间。"
        );
        return;
    }
    let mode = args
        .windows(2)
        .find(|p| p[0] == "--mode")
        .and_then(|p| p[1].parse::<usize>().ok())
        .filter(|&m| m < MODES.len())
        .unwrap_or(3);
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        eprintln!("请在交互式终端运行阅读 Demo。");
        std::process::exit(1);
    }
    if let Err(error) = run(mode) {
        terminal::restore();
        eprintln!("Reading demo: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn demo() -> Demo {
        let mut demo = Demo::new(3);
        demo.layout(Rect::new(0, 0, 120, 38));
        demo
    }

    #[test]
    fn modes_differ_on_long_paragraphs_and_narrow_layout_preserves_group() {
        let mut demo = demo();
        let (&group, &(start, end)) = demo
            .groups
            .iter()
            .find(|(_, (start, end))| end - start > 8)
            .unwrap();
        demo.focus = Some((start + end) / 2);
        demo.scroll = demo.focus.unwrap().saturating_sub(8);
        demo.set_mode(2);
        assert_eq!(demo.active_range(), Some((start, end)));
        demo.set_mode(3);
        let (a, b) = demo.active_range().unwrap();
        assert_eq!(b - a, 5);
        demo.set_mode(1);
        let (a, b) = demo.active_range().unwrap();
        assert_eq!(b - a, 3);
        demo.set_mode(4);
        demo.layout(Rect::new(0, 0, 160, 38));
        assert_eq!(demo.text.width, 92);
        assert_eq!(demo.rows[demo.focus.unwrap()].group, group);
        demo.set_mode(5);
        demo.layout(Rect::new(0, 0, 160, 38));
        assert_eq!(demo.text.width, 92);
        assert_eq!(demo.active_range(), None);
        for dim in [false, true] {
            demo.dim = dim;
            for (index, row) in demo.rows.iter().enumerate() {
                assert_eq!(
                    demo.line(index),
                    row.row.line,
                    "pure centering must retain original styles"
                );
            }
        }
        demo.set_mode(0);
        assert_eq!(demo.active_range(), None);
    }

    #[test]
    fn hover_does_not_relayout_and_selection_or_lock_takes_priority() {
        let mut demo = demo();
        let row = demo.focus.unwrap();
        let x = demo.text.x + 3;
        let y = demo.text.y + row as u16;
        assert!(!demo.hover(x, y));
        assert!(!demo.hover(x + 1, y));
        let layouts = demo.layouts;
        demo.layout(Rect::new(0, 0, 120, 38));
        assert_eq!(demo.layouts, layouts);
        demo.locked = true;
        assert!(!demo.hover(x, y + 1));
        assert_eq!(demo.focus, Some(row));
        demo.locked = false;
        demo.selection = Some(Selection {
            anchor: Point { row, byte: 0 },
            head: Point { row, byte: 3 },
        });
        assert!(!demo.hover(x, y + 1));
        assert_eq!(demo.focus, Some(row));
        demo.selection = None;
        assert!(demo.hover(0, 0));
        assert_eq!(demo.focus, None);
    }

    #[test]
    fn streaming_does_not_reflow_selected_history_and_unfreezes_after_cancel() {
        let mut demo = demo();
        let row = demo.focus.unwrap();
        demo.selection = Some(Selection {
            anchor: Point { row, byte: 0 },
            head: Point { row, byte: 3 },
        });
        let before = demo.rows[row].row.text.clone();
        let layouts = demo.layouts;
        for _ in 0..15 {
            demo.tick();
            demo.layout(Rect::new(0, 0, 120, 38));
        }
        assert_eq!(demo.layouts, layouts);
        assert_eq!(demo.rows[row].row.text, before);
        demo.selection = None;
        demo.layout(Rect::new(0, 0, 120, 38));
        assert_eq!(demo.layouts, layouts + 1);
    }

    #[test]
    fn table_selection_copies_logical_rows_without_grid() {
        let mut demo = demo();
        let first = demo
            .rows
            .iter()
            .position(|r| r.kind == Kind::Table)
            .unwrap();
        let last = demo
            .rows
            .iter()
            .rposition(|r| r.kind == Kind::Table && !r.row.text.is_empty())
            .unwrap();
        demo.selection = Some(Selection {
            anchor: Point {
                row: first,
                byte: 0,
            },
            head: Point {
                row: last,
                byte: demo.rows[last].row.text.len(),
            },
        });
        let copied = demo.selected_text();
        assert!(!copied.contains('│'));
        assert!(copied.starts_with("类型\t忙碌时如何处理\t判断依据\n"));
        assert!(copied.contains("B\t同伴的新求助：排队等待\t不把独立任务塞进正在进行的工作"));
    }
}
