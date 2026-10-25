mod bridge;
mod history;
mod input;
mod paste;
mod table_copy;
mod terminal;
#[cfg(test)]
mod tests;
mod timeline;
mod ui;
mod wide_backend;

use bridge::Bridge;
use crossterm::event::{
    self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
    MouseEventKind,
};
use history::{ACCENT, BG, FG, MUTED, Point, Selection};
use input::{Input, byte_at_char};
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::{Color, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    env,
    io::{self, IsTerminal, Write},
    process::{Command, Stdio},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use timeline::{Timeline, color, duration, string};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

const HELP: &str = "Codex Room · Rust Fullscreen\n\n/add 名字 [目录] [--color blue]   创建成员\n/resume 会话ID [成员名]   接入本机已有会话\n/rename 原名 新名   /del 名字\n/focus 名字   /focus all   /stop 名字\n/model [模型] [强度]   /permissions\n/adhd [on / off]   窄栏居中阅读\n/reconnect   /quit\n\n开头 @a @b 同时发送，各自独立处理；忙时追加到当前轮。\nTab 补全命令、成员、模型、目录、颜色；↑/↓ 选候选。\n空输入框 ↑/↓ 翻历史；Ctrl+J / Shift+Enter 换行。\n长粘贴和图片是块，退格整块删除；Alt+Enter 展开长文本。\nCtrl+Z / Ctrl+Y 撤销 / 重做；Ctrl+U 清空输入。\n\nCtrl+B 成员栏；Ctrl+L 全部/单人；Ctrl+N 创建。\n拖选只覆盖正文；双击片段，三击行；点高亮外取消。\n⌘C / Ctrl+C / Ctrl+Shift+C 复制选区；底部也有「复制」。\n无选区时仅 Ctrl+C 退出；⌘C 保留程序；Ctrl+Q 始终退出。\n拖到边缘自动滚动；滚轮每次 1 行；Ctrl+End 到底部。\n标题点击折叠回复；工具标题点击展开/收起摘要。\n单项点击查看详情；Ctrl+T 切换可见工具摘要。\nCtrl+F 转发选区，含跨过的折叠详情；Esc 取消。\n\n主界面为 Rust；Codex 通信与兼容存储由本地 Python 服务负责。\n原生子代理由父代理通过自然语言创建，自动加入成员。\n可直接 @子代理 续聊；V2 经父代理转交，展示子代理原话。\n子代理支持停止、归档删除和恢复；模型与权限由父会话管理。\n\n独立成员也可公开协作：让 A 用 Room 消息工具联系 B。\n互发原文显示为 A → B 卡片，支持折叠、复制和转发。\n同伴默认 Queue，可显式 Steer；用户消息优先追加。\nRoom list_members 可查成员及最近 3～5 轮简短对话。\n/stop 拦截旧链后续消息；每条链最多 12 条，避免空转。";

#[derive(Clone, Default)]
struct Draft {
    input: Input,
    quote: Option<Value>,
}
struct Submission {
    id: u64,
    prompt: Value,
    view: String,
    draft: Draft,
}
#[derive(Clone)]
struct Item {
    label: String,
    detail: String,
    value: String,
}
enum Modal {
    Picker {
        title: String,
        items: Vec<Item>,
        filter: Input,
        selected: usize,
        action: String,
        data: Value,
    },
    Preview {
        title: String,
        text: String,
        scroll: usize,
    },
    Question {
        event: Value,
        index: usize,
        answers: serde_json::Map<String, Value>,
        input: Input,
    },
}
struct App {
    entries: Vec<Value>,
    index: HashMap<String, usize>,
    members: Vec<Value>,
    timeline: Timeline,
    selected: String,
    show_all: bool,
    connected: bool,
    demo: bool,
    sidebar: bool,
    reading_centered: bool,
    sidebar_top: usize,
    ignore_left_release: bool,
    pending_bottom: bool,
    draft: Draft,
    drafts: HashMap<String, Draft>,
    source: Option<(String, bool, Draft)>,
    input_history: Vec<Value>,
    history_at: Option<usize>,
    history_draft: Input,
    history_edits: HashMap<usize, Input>,
    scroll: usize,
    follow: bool,
    selection: Option<Selection>,
    press: Option<(u16, u16)>,
    press_point: Option<Point>,
    dragging: bool,
    pointer: Option<(u16, u16)>,
    last_click: Option<(Instant, u16, u16, u8)>,
    body: Rect,
    input_area: Rect,
    footer: Rect,
    context: Rect,
    sidebar_area: Rect,
    modal_area: Rect,
    completion_area: Rect,
    modal: Option<Modal>,
    candidates: Vec<Value>,
    candidate: usize,
    completion_id: u64,
    completion_due: Option<Instant>,
    completion_hidden: bool,
    submit_pending: Option<Submission>,
    next_id: u64,
    notice: String,
    notice_at: Instant,
    interactions: Vec<Value>,
    pending_quote: Option<Value>,
    forward_create: bool,
    changed: HashSet<String>,
    rebuild: bool,
    need_save_draft: bool,
    quit: bool,
    service_dead: bool,
    cwd: std::path::PathBuf,
    view_revision: u64,
    modal_list_y: u16,
    modal_list_top: usize,
}
impl App {
    fn new(width: usize) -> Self {
        Self {
            entries: vec![],
            index: HashMap::new(),
            members: vec![],
            timeline: Timeline::new(width.saturating_sub(3)),
            selected: String::new(),
            show_all: true,
            connected: false,
            demo: false,
            sidebar: false,
            reading_centered: false,
            sidebar_top: 0,
            ignore_left_release: false,
            pending_bottom: true,
            draft: Draft::default(),
            drafts: HashMap::new(),
            source: None,
            input_history: vec![],
            history_at: None,
            history_draft: Input::default(),
            history_edits: HashMap::new(),
            scroll: 0,
            follow: true,
            selection: None,
            press: None,
            press_point: None,
            dragging: false,
            pointer: None,
            last_click: None,
            body: Rect::default(),
            input_area: Rect::default(),
            footer: Rect::default(),
            context: Rect::default(),
            sidebar_area: Rect::default(),
            modal_area: Rect::default(),
            completion_area: Rect::default(),
            modal: None,
            candidates: vec![],
            candidate: 0,
            completion_id: 0,
            completion_due: None,
            completion_hidden: false,
            submit_pending: None,
            next_id: 0,
            notice: "正在连接 Codex…".into(),
            notice_at: Instant::now(),
            interactions: vec![],
            pending_quote: None,
            forward_create: false,
            changed: HashSet::new(),
            rebuild: true,
            need_save_draft: false,
            quit: false,
            service_dead: false,
            cwd: env::current_dir().unwrap_or_default(),
            view_revision: 0,
            modal_list_y: 0,
            modal_list_top: 0,
        }
    }
    fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }
    fn key_for(&self) -> String {
        if self.show_all {
            "all".into()
        } else {
            self.selected.clone()
        }
    }
    fn stash(&mut self) {
        let key = self.key_for();
        self.drafts.insert(key, self.draft.clone());
        self.need_save_draft = true;
    }
    fn change_view(&mut self, name: String, all: bool, bridge: &Bridge) {
        self.stash();
        self.selected = name;
        self.show_all = all;
        self.draft = self
            .drafts
            .get(&self.key_for())
            .cloned()
            .unwrap_or_default();
        self.history_at = None;
        self.candidates.clear();
        self.clear_selection();
        self.follow = true;
        self.scroll = usize::MAX;
        self.pending_bottom = true;
        self.timeline.invalidate();
        self.rebuild = true;
        self.view_revision = self.id();
        bridge.send(
            json!({"type":"select","name":self.selected,"show_all":all,"id":self.view_revision}),
        );
    }
    fn clear_selection(&mut self) {
        self.selection = None;
        self.press = None;
        self.press_point = None;
        self.dragging = false;
        self.pointer = None;
    }
    fn notice(&mut self, text: String) {
        self.notice = text;
        self.notice_at = Instant::now();
    }
    fn maximum(&self) -> usize {
        self.timeline
            .rows
            .len()
            .saturating_sub(self.body.height as usize)
    }
    fn scroll_by(&mut self, delta: isize) {
        self.pending_bottom = false;
        self.follow = false;
        self.scroll = self.scroll.saturating_add_signed(delta).min(self.maximum());
    }
    fn point(&self, x: u16, y: u16) -> Option<Point> {
        if self.timeline.rows.is_empty() || self.body.height == 0 {
            return None;
        }
        let y = y.clamp(self.body.y, self.body.bottom().saturating_sub(1));
        let row = (self.scroll + (y - self.body.y) as usize).min(self.timeline.rows.len() - 1);
        let col = x.saturating_sub(self.body.x + 2) as usize;
        Some(Point {
            row,
            byte: history::byte_at_cell(&self.timeline.rows[row].text, col),
        })
    }
    fn auto_scroll(&mut self) -> bool {
        if !self.dragging || self.press.is_none() {
            return false;
        }
        let Some((x, y)) = self.pointer else {
            return false;
        };
        let old = self.scroll;
        if y <= self.body.y {
            self.scroll_by(-1);
        } else if y >= self.body.bottom().saturating_sub(1) {
            self.scroll_by(1);
        }
        if old == self.scroll {
            return false;
        }
        if let Some(point) = self.point(x, y) {
            if let Some(s) = self.selection.as_mut() {
                s.head = point;
            }
        }
        true
    }
    fn rebuild(&mut self) {
        // Freeze the transcript cache while a selection exists. New messages
        // continue into the model; releasing the selection applies them together.
        // The fixed row coordinates keep an off-screen drag anchor stable.
        if self.selection.is_some() || self.press.is_some() {
            return;
        }
        if self.rebuild {
            self.timeline.rebuild(
                &self.entries,
                &self.members,
                &self.selected,
                self.show_all,
                &self.changed,
            );
            self.changed.clear();
            self.rebuild = false;
        }
    }
    fn on_service(&mut self, v: Value) {
        match string(&v, "type") {
            "init" | "replace" => {
                self.entries = v["entries"].as_array().cloned().unwrap_or_default();
                self.index = self
                    .entries
                    .iter()
                    .enumerate()
                    .map(|(i, e)| (string(e, "id").to_owned(), i))
                    .collect();
                self.timeline.invalidate();
                self.rebuild = true;
                self.clear_selection();
                if string(&v, "type") == "init" {
                    self.reading_centered = v["reading_centered"].as_bool().unwrap_or(false);
                    self.cwd = std::path::PathBuf::from(string(&v, "cwd"));
                    self.input_history = v["input_history"].as_array().cloned().unwrap_or_default();
                    if let Some(drafts) = v["drafts"].as_object() {
                        for (key, d) in drafts {
                            self.drafts.insert(
                                key.clone(),
                                Draft {
                                    input: Input::from_prompt(&d["prompt"]),
                                    quote: d["quote"].as_object().map(|_| d["quote"].clone()),
                                },
                            );
                        }
                    }
                    self.draft = self.drafts.get("all").cloned().unwrap_or_default();
                }
            }
            "patch" => {
                for e in v["entries"].as_array().into_iter().flatten() {
                    let id = string(e, "id").to_owned();
                    self.changed.insert(id.clone());
                    if let Some(i) = self.index.get(&id) {
                        self.entries[*i] = e.clone();
                    } else {
                        self.index.insert(id, self.entries.len());
                        self.entries.push(e.clone());
                    }
                }
                self.rebuild = true;
            }
            "members" => {
                let members = v["agents"].as_array().cloned().unwrap_or_default();
                let is_new_selected = !self
                    .members
                    .iter()
                    .any(|m| string(m, "name") == string(&v, "selected"));
                let identities = |ms: &[Value]| {
                    ms.iter()
                        .map(|m| json!([m["name"], m["label"], m["color"], m["cwd"]]))
                        .collect::<Vec<_>>()
                };
                if identities(&self.members) != identities(&members) {
                    self.timeline.invalidate();
                    self.rebuild = true;
                }
                self.members = members;
                self.connected = v["connected"].as_bool().unwrap_or(false);
                self.demo = v["demo"].as_bool().unwrap_or(false);
                let current_view = v["view_revision"].as_u64().unwrap_or(0) >= self.view_revision;
                let selected = if current_view {
                    string(&v, "selected").to_owned()
                } else {
                    self.selected.clone()
                };
                let all = if current_view {
                    v["show_all"].as_bool().unwrap_or(true)
                } else {
                    self.show_all
                };
                if self.selected != selected || self.show_all != all {
                    self.stash();
                    self.selected = selected;
                    self.show_all = all;
                    self.draft = self
                        .drafts
                        .get(&self.key_for())
                        .cloned()
                        .unwrap_or_default();
                    self.timeline.invalidate();
                    self.rebuild = true;
                    self.clear_selection();
                    self.follow = true;
                    self.pending_bottom = true;
                }
                if self.forward_create
                    && is_new_selected
                    && self
                        .members
                        .iter()
                        .any(|m| string(m, "name") == self.selected)
                {
                    self.forward_create = false;
                    if let Some(q) = self.pending_quote.take() {
                        self.draft.quote = Some(q);
                        self.need_save_draft = true;
                    }
                }
                self.drafts.retain(|key, _| {
                    key == "all" || self.members.iter().any(|m| string(m, "name") == key)
                });
                if self.source.as_ref().is_some_and(|(name, all, _)| {
                    !all && !self.members.iter().any(|m| string(m, "name") == name)
                }) {
                    self.source = None;
                }
            }
            "notice" => self.notice(string(&v, "text").into()),
            "fatal" => {
                self.service_dead = true;
                self.connected = false;
                self.notice(string(&v, "text").into());
            }
            "submitted" => {
                if self
                    .submit_pending
                    .as_ref()
                    .is_some_and(|pending| Some(pending.id) == v["id"].as_u64())
                {
                    let Submission {
                        prompt,
                        view,
                        draft,
                        ..
                    } = self.submit_pending.take().unwrap();
                    if v["accepted"].as_bool() == Some(true) {
                        if self.input_history.last() != Some(&prompt) {
                            self.input_history.push(prompt.clone());
                        }
                    } else {
                        if view == self.key_for() {
                            self.draft = merge_drafts(draft, std::mem::take(&mut self.draft));
                        } else {
                            let current = self.drafts.remove(&view).unwrap_or_default();
                            self.drafts.insert(view, merge_drafts(draft, current));
                        }
                        self.need_save_draft = true;
                        self.notice("未发送，输入内容已保留".into());
                    }
                }
            }
            "completion" => {
                if v["id"].as_u64() == Some(self.completion_id)
                    && string(&v, "text") == self.draft.input.text
                    && v["cursor"].as_u64()
                        == Some(
                            self.draft.input.text[..self.draft.input.cursor]
                                .chars()
                                .count() as u64,
                        )
                    && !self.completion_hidden
                {
                    self.candidates = v["candidates"].as_array().cloned().unwrap_or_default();
                    self.candidate = 0;
                }
            }
            "prefill" => {
                self.draft.input = Input::from_prompt(&v["text"]);
                self.edited();
            }
            "choose" => {
                let items: Vec<Item> = v["values"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|m| Item {
                        label: m["label"].as_str().unwrap_or(string(m, "value")).into(),
                        detail: string(m, "detail").into(),
                        value: string(m, "value").into(),
                    })
                    .collect();
                let selected = items
                    .iter()
                    .position(|item| item.value == string(&v, "selected"))
                    .unwrap_or(0);
                let action = v["action"].as_str().unwrap_or("command").to_owned();
                let data = if action == "command" {
                    v["prefix"].clone()
                } else {
                    v["data"].clone()
                };
                self.modal = Some(Modal::Picker {
                    title: string(&v, "title").into(),
                    items,
                    filter: Input::default(),
                    selected,
                    action,
                    data,
                });
            }
            "action" => match string(&v, "action") {
                "bottom" => {
                    self.follow = true;
                    self.pending_bottom = true;
                    self.clear_selection();
                }
                "reading_centered" => {
                    self.reading_centered = v["enabled"].as_bool().unwrap_or(false);
                }
                "members" => {
                    self.sidebar = !self.sidebar;
                    self.timeline.invalidate();
                    self.rebuild = true;
                }
                "help" => self.help(),
                "quit" => self.quit = true,
                _ => {}
            },
            "interaction" => {
                let name = string(&v, "member").to_owned();
                self.interactions.push(v["event"].clone());
                self.interactions.last_mut().unwrap()["_member"] = json!(name);
                self.notice(format!("@{name} 等待回复，点击底部「回复请求」"));
                if name == self.selected && self.modal.is_none() {
                    self.open_interaction();
                }
            }
            "interaction_done" => {
                self.interactions
                    .retain(|e| e["id"].to_string().trim_matches('"') != string(&v, "id"));
            }
            _ => {}
        }
    }
    fn edited(&mut self) {
        self.completion_hidden = false;
        self.completion_due = Some(Instant::now() + Duration::from_millis(35));
        self.candidates.clear();
        self.need_save_draft = true;
    }
    fn complete(&mut self, bridge: &Bridge, explicit: bool) {
        let id = self.id();
        self.completion_id = id;
        self.completion_due = None;
        self.completion_hidden = false;
        bridge.send(json!({"type":"complete","id":id,"text":self.draft.input.text,"cursor":self.draft.input.text[..self.draft.input.cursor].chars().count(),"explicit":explicit}));
    }
    fn accept_completion(&mut self, bridge: &Bridge) {
        if let Some(c) = self.candidates.get(self.candidate).cloned() {
            let text = string(&c, "text");
            let cursor = byte_at_char(text, c["cursor"].as_u64().unwrap_or(0) as usize);
            let start = byte_at_char(
                &self.draft.input.text,
                c["start"].as_u64().unwrap_or(0) as usize,
            );
            let end = byte_at_char(
                &self.draft.input.text,
                c["end"].as_u64().unwrap_or(0) as usize,
            );
            if !self
                .draft
                .input
                .completion(start, end, string(&c, "replacement"), cursor)
            {
                return;
            }
            let member = string(&c, "member");
            if !member.is_empty() {
                self.selected = member.into();
                self.view_revision = self.id();
                bridge.send(json!({"type":"select","name":member,"show_all":self.show_all,"id":self.view_revision}));
            }
            self.candidates.clear();
            self.completion_hidden = true;
            self.completion_due = None;
            self.need_save_draft = true;
        }
    }
    fn submit(&mut self, bridge: &Bridge) {
        if self.submit_pending.is_some() {
            self.notice("正在确认上一条消息…".into());
            return;
        }
        if self.service_dead {
            self.notice("会话服务已退出，草稿保留，请重启".into());
            return;
        }
        if self.draft.input.text.trim().is_empty() && self.draft.quote.is_none() {
            return;
        }
        let id = self.id();
        let prompt = self.draft.input.prompt();
        let preserve_view = self.draft.quote.is_none()
            && self.draft.input.state.attachments.is_empty()
            && self
                .draft
                .input
                .text
                .split_whitespace()
                .next()
                .is_some_and(|s| s.eq_ignore_ascii_case("/adhd"));
        bridge.send(json!({"type":"submit","id":id,"prompt":prompt,"quote":self.draft.quote}));
        self.submit_pending = Some(Submission {
            id,
            prompt,
            view: self.key_for(),
            draft: std::mem::take(&mut self.draft),
        });
        self.history_at = None;
        self.clear_selection();
        if !preserve_view {
            self.follow = true;
        }
        self.need_save_draft = true;
        self.completion_hidden = true;
        self.candidates.clear();
        self.completion_due = None;
        // The payload remains in Submission until ACK, while typing can start
        // immediately. A rejected dispatch restores it with any newer draft.
    }
    fn history_move(&mut self, delta: isize) {
        if self.input_history.is_empty() {
            return;
        }
        if self.history_at.is_none() {
            if delta > 0 || !self.draft.input.text.is_empty() {
                return;
            }
            self.history_draft = self.draft.input.clone();
            self.history_edits.clear();
            self.history_at = Some(self.input_history.len());
        }
        let at = self.history_at.unwrap();
        if at < self.input_history.len() {
            self.history_edits.insert(at, self.draft.input.clone());
        }
        let next = at
            .saturating_add_signed(delta)
            .min(self.input_history.len());
        self.history_at = Some(next);
        self.draft.input = if next == self.input_history.len() {
            self.history_at = None;
            self.history_draft.clone()
        } else {
            self.history_edits
                .get(&next)
                .cloned()
                .unwrap_or_else(|| Input::from_prompt(&self.input_history[next]))
        };
        self.completion_hidden = true;
        self.candidates.clear();
        self.completion_due = None;
        self.need_save_draft = true;
    }
    fn help(&mut self) {
        self.modal = Some(Modal::Preview {
            title: "帮助 · Esc 返回".into(),
            text: HELP.into(),
            scroll: 0,
        });
    }
    fn return_source(&mut self, bridge: &Bridge) {
        if let Some((name, all, draft)) = self.source.take() {
            self.change_view(name, all, bridge);
            self.draft = draft;
            self.need_save_draft = true;
            self.scroll = usize::MAX;
            self.follow = true;
        }
    }
    fn quote(&self) -> Option<Value> {
        let selection = self.selection?;
        let (first, last) = selection.ordered();
        if first == last {
            return None;
        }
        let rows = self
            .timeline
            .rows
            .get(first.row..=last.row.min(self.timeline.rows.len().saturating_sub(1)))?;
        let mut indices = Vec::new();
        for r in rows {
            if !indices.contains(&r.message) {
                indices.push(r.message);
            }
        }
        let source = indices
            .iter()
            .filter_map(|i| self.entries.get(*i))
            .find(|e| string(e, "sender") != "system")
            .map(|e| {
                if e["communication"].is_object() && !self.show_all {
                    self.selected.as_str()
                } else if string(e, "sender") == "you" {
                    string(e, "target")
                } else {
                    string(e, "sender")
                }
            })
            .unwrap_or("");
        let source_label = self
            .members
            .iter()
            .find(|m| string(m, "name") == source)
            .map(|m| string(m, "label"))
            .unwrap_or(source);
        let mut messages = Vec::new();
        for index in indices {
            let e = &self.entries[index];
            if string(e, "sender") == "system" || e["hidden"].as_bool() == Some(true) {
                continue;
            }
            let entry_rows = rows
                .iter()
                .filter(|r| r.message == index)
                .collect::<Vec<_>>();
            let full = entry_rows.iter().any(|r| r.full)
                || (index != self.timeline.rows[first.row].message
                    && index
                        != self.timeline.rows[last.row.min(self.timeline.rows.len() - 1)].message);
            let ids: HashSet<&str> = entry_rows
                .iter()
                .flat_map(|r| r.blocks.iter().map(String::as_str))
                .collect();
            let chosen_text = table_copy::selected_text(
                selection,
                rows.iter()
                    .enumerate()
                    .filter(|(_, r)| {
                        r.message == index
                            && r.blocks.iter().all(|id| {
                                e["blocks"]
                                    .as_array()
                                    .into_iter()
                                    .flatten()
                                    .find(|b| string(b, "id") == id)
                                    .is_none_or(|b| string(b, "kind") == "text")
                            })
                            && !r.working
                            && r.action != "meta"
                            && !r.action.starts_with("collapse:")
                            && !r.action.starts_with("quote:")
                    })
                    .map(|(n, r)| (first.row + n, r)),
            );
            let mut blocks = Vec::new();
            if full {
                blocks = e["blocks"].as_array().cloned().unwrap_or_default();
            } else {
                for b in e["blocks"].as_array().into_iter().flatten() {
                    if string(b, "kind") == "text" {
                        if ids.contains(string(b, "id")) {
                            let text = table_copy::selected_text(
                                selection,
                                rows.iter()
                                    .enumerate()
                                    .filter(|(_, r)| {
                                        r.message == index
                                            && r.blocks.iter().any(|id| id == string(b, "id"))
                                    })
                                    .map(|(n, r)| (first.row + n, r)),
                            );
                            let mut block = b.clone();
                            block["text"] = json!(text);
                            blocks.push(block);
                        }
                    } else if ids.contains(string(b, "id")) {
                        blocks.push(b.clone());
                    }
                }
            }

            let member = if string(e, "sender") == "you" {
                string(e, "target")
            } else {
                string(e, "sender")
            };
            let m = self
                .members
                .iter()
                .find(|m| string(m, "name") == member)
                .unwrap_or(&Value::Null);
            let display = e["display_body"].as_str().unwrap_or(string(e, "body"));
            let prompt =
                Input::from_prompt(&json!({"text":display,"attachments":e["attachments"]}));
            let selected_prompt = if full {
                prompt
            } else {
                let positions = rows
                    .iter()
                    .enumerate()
                    .filter(|(_, r)| r.message == index)
                    .filter_map(|(n, r)| {
                        let offset = r.source_start?;
                        let (a, b) = selection.span(first.row + n, r.text.len())?;
                        Some((offset + a, offset + b))
                    })
                    .collect::<Vec<_>>();
                positions
                    .first()
                    .zip(positions.last())
                    .map(|(a, b)| prompt.slice(a.0, b.1))
                    .unwrap_or_default()
            };
            let text = if string(e, "sender") == "you" {
                selected_prompt.expanded_text()
            } else if full {
                string(e, "body").into()
            } else {
                chosen_text
            };
            let mut images = selected_prompt
                .attachments
                .iter()
                .filter(|a| a.kind == "image")
                .map(|a| json!(a.content))
                .collect::<Vec<_>>();
            let include_quote = full || entry_rows.iter().any(|r| r.action.starts_with("quote:"));
            let forwarded = if include_quote {
                e["quote"].clone()
            } else {
                Value::Null
            };
            if !forwarded.is_null() {
                for m in forwarded["messages"].as_array().into_iter().flatten() {
                    if string(&forwarded, "scope") == "all"
                        || string(&forwarded, "source").is_empty()
                        || m["member"] == forwarded["source"]
                    {
                        images.extend(m["images"].as_array().into_iter().flatten().cloned());
                    }
                }
            }

            let conversation_member = if e["communication"].is_object()
                && (string(e, "sender") == source || string(e, "target") == source)
            {
                source
            } else {
                member
            };
            let recipient = if e["communication"].is_object() {
                self.members
                    .iter()
                    .find(|m| string(m, "name") == string(e, "target"))
                    .map(|m| string(m, "label"))
                    .unwrap_or(string(&e["communication"], "target_label"))
            } else {
                string(m, "label")
            };
            messages.push(json!({"id":e["id"],"member":conversation_member,"speaker":if string(e,"sender")=="you"{"用户"}else{string(m,"label")},"recipient":recipient,"cwd":m["cwd"],"time":e["time"],"text":text,"blocks":blocks,"images":images,"forwarded_quote":forwarded,"communication":e["communication"]}));
        }
        if messages.is_empty() {
            return None;
        }
        Some(
            json!({"id":format!("quote-{}",SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_nanos()),"messages":messages,"source":source,"source_label":source_label,"scope":"source"}),
        )
    }
    fn selected_text(&self) -> String {
        let Some(selection) = self.selection else {
            return String::new();
        };
        let (first, last) = selection.ordered();
        if first == last {
            return String::new();
        }
        let mut parts = Vec::new();
        let mut at = first.row;
        while at <= last.row && at < self.timeline.rows.len() {
            let row = &self.timeline.rows[at];
            if row.source_start.is_some() {
                let message = row.message;
                let e = &self.entries[message];
                let mut positions = Vec::new();
                while at <= last.row && at < self.timeline.rows.len() {
                    let r = &self.timeline.rows[at];
                    if r.message != message || r.source_start.is_none() {
                        break;
                    }
                    if let Some((a, b)) = selection.span(at, r.text.len()) {
                        positions.push((r.source_start.unwrap() + a, r.source_start.unwrap() + b));
                    }
                    at += 1;
                }
                if let Some((start, end)) = positions.first().zip(positions.last()) {
                    let display = e["display_body"].as_str().unwrap_or(string(e, "body"));
                    let prompt =
                        Input::from_prompt(&json!({"text":display,"attachments":e["attachments"]}));
                    parts.push(prompt.slice(start.0, end.1).full_text());
                }
            } else if row.table.is_some() {
                let start = at;
                while at <= last.row
                    && at < self.timeline.rows.len()
                    && self.timeline.rows[at].table.is_some()
                {
                    at += 1;
                }
                let text = table_copy::selected_text(
                    selection,
                    self.timeline.rows[start..at]
                        .iter()
                        .enumerate()
                        .map(|(n, r)| (start + n, r)),
                );
                if !text.is_empty() {
                    parts.push(text);
                }
            } else {
                let text = &row.text;
                let a = if at == first.row {
                    first.byte.min(text.len())
                } else {
                    0
                };
                let b = if at == last.row {
                    last.byte.min(text.len())
                } else {
                    text.len()
                };
                parts.push(text.get(a..b).unwrap_or("").to_owned());
                at += 1;
            }
        }
        parts.join("\n")
    }
    fn copy_selection(&mut self, quit_if_empty: bool) {
        let text = self.selected_text();
        if text.is_empty() {
            if quit_if_empty {
                self.quit = true;
            } else {
                self.notice("先拖选需要复制的文字".into());
            }
            return;
        }
        match copy(&text) {
            Ok(()) => self.notice(format!("已复制 {} 字符", text.chars().count())),
            Err(e) => self.notice(format!("复制失败：{e}")),
        }
    }
    fn forward(&mut self) {
        let Some(quote) = self.quote() else {
            self.notice("先拖选需要转发的对话".into());
            return;
        };
        let mut items = self.member_items();
        items.push(Item {
            label: "＋ 新建 Codex".into(),
            detail: "创建后附上引用，再补充任务".into(),
            value: "+new".into(),
        });
        self.modal = Some(Modal::Picker {
            title: "转发给… · 搜索 / ↑↓ / Enter".into(),
            items,
            filter: Input::default(),
            selected: 0,
            action: "forward".into(),
            data: quote,
        });
    }
    fn member_items(&self) -> Vec<Item> {
        self.members
            .iter()
            .map(|m| Item {
                label: format!("@{}", string(m, "label")),
                detail: format!(
                    "{} · {}{}",
                    if m["busy"].as_bool() == Some(true) {
                        "工作中"
                    } else {
                        "空闲"
                    },
                    string(m, "cwd"),
                    self.members
                        .iter()
                        .find(|p| string(p, "name") == string(m, "parent_name"))
                        .map(|p| format!(" · 子代理 ↳ @{}", string(p, "label")))
                        .unwrap_or_default()
                ),
                value: string(m, "name").into(),
            })
            .collect()
    }
    fn open_interaction(&mut self) {
        if let Some(e) = self
            .interactions
            .iter()
            .find(|e| string(e, "_member") == self.selected)
            .cloned()
        {
            if matches!(
                string(&e, "method"),
                "item/tool/requestUserInput" | "tool/requestUserInput"
            ) {
                self.modal = Some(Modal::Question {
                    event: e,
                    index: 0,
                    answers: serde_json::Map::new(),
                    input: Input::default(),
                });
            } else {
                let detail = serde_json::to_string_pretty(&e["params"]).unwrap_or_default();
                self.modal = Some(Modal::Picker {
                    title: format!("请求批准 · {detail}"),
                    items: vec![
                        Item {
                            label: "拒绝".into(),
                            detail: "".into(),
                            value: "decline".into(),
                        },
                        Item {
                            label: "允许这次".into(),
                            detail: "".into(),
                            value: "accept".into(),
                        },
                    ],
                    filter: Input::default(),
                    selected: 0,
                    action: "approval".into(),
                    data: e,
                });
            }
        }
    }
    fn action(&mut self, action: &str, bridge: &Bridge) {
        self.clear_selection();
        self.follow = false;
        if let Some(id) = action.strip_prefix("collapse:") {
            if let Some(i) = self.index.get(id).copied() {
                let collapsed = !self.entries[i]["collapsed"].as_bool().unwrap_or(false);
                self.entries[i]["collapsed"] = json!(collapsed);
                bridge.send(json!({"type":"collapse","id":id,"collapsed":collapsed}));
                self.changed.insert(id.into());
                self.rebuild = true;
            }
        } else if let Some(key) = action.strip_prefix("tools:") {
            if !self.timeline.groups.remove(key) {
                self.timeline.groups.insert(key.into());
            }
            if let Some((id, _)) = key.rsplit_once(':') {
                self.changed.insert(id.into());
                self.rebuild = true;
            }
        } else if let Some(key) = action.strip_prefix("all:") {
            if !self.timeline.all_details.remove(key) {
                self.timeline.all_details.insert(key.into());
            }
            if let Some((id, _)) = key.rsplit_once(':') {
                self.changed.insert(id.into());
                self.rebuild = true;
            }
        } else if let Some(key) = action.strip_prefix("detail:") {
            if !self.timeline.details.remove(key) {
                self.timeline.details.insert(key.into());
            }
            if let Some((id, _)) = key.rsplit_once(':') {
                self.changed.insert(id.into());
                self.rebuild = true;
            }
        } else if let Some(id) = action.strip_prefix("quote:") {
            if let Some(i) = self.index.get(id) {
                self.modal = Some(Modal::Preview {
                    title: "转发引用 · Esc 返回".into(),
                    text: serde_json::to_string_pretty(&self.entries[*i]["quote"])
                        .unwrap_or_default(),
                    scroll: 0,
                });
            }
        }
    }
    fn mouse(&mut self, mouse: MouseEvent, bridge: &Bridge) -> Result<()> {
        let (x, y) = (mouse.column, mouse.row);
        if mouse.kind == MouseEventKind::Up(MouseButton::Left) && self.ignore_left_release {
            self.ignore_left_release = false;
            self.press = None;
            self.press_point = None;
            self.pointer = None;
            self.dragging = false;
            return Ok(());
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
            // Controls own the entire click, even if opening a panel or
            // switching views changes the layout before the release arrives.
            self.ignore_left_release = true;
        }
        if self.modal.is_some() {
            match mouse.kind {
                MouseEventKind::ScrollUp => self.modal_scroll(-1),
                MouseEventKind::ScrollDown => self.modal_scroll(1),
                MouseEventKind::Down(MouseButton::Left) => {
                    if let Some(Modal::Picker {
                        items,
                        filter,
                        selected,
                        ..
                    }) = &mut self.modal
                    {
                        let shown = filtered_items(items, &filter.text);
                        let at =
                            self.modal_list_top + (y.saturating_sub(self.modal_list_y)) as usize;
                        if self.modal_area.contains(Position::new(x, y))
                            && y >= self.modal_list_y
                            && at < shown.len()
                        {
                            *selected = at;
                            self.choose_modal(bridge)?;
                        }
                    }
                }
                _ => {}
            }
            return Ok(());
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
            if !self.completion_area.is_empty()
                && self.completion_area.contains(Position::new(x, y))
            {
                let top = self
                    .candidate
                    .saturating_sub(self.completion_area.height.saturating_sub(1) as usize);
                self.candidate = (top + (y - self.completion_area.y) as usize)
                    .min(self.candidates.len().saturating_sub(1));
                self.accept_completion(bridge);
                return Ok(());
            }
            if self.footer.contains(Position::new(x, y)) {
                let local_x = x - self.footer.x;
                if local_x < 10 {
                    self.sidebar = !self.sidebar;
                    self.timeline.invalidate();
                    self.rebuild = true;
                } else if local_x < 20 {
                    self.forward();
                } else if local_x < 29 {
                    self.clear_selection();
                    self.follow = true;
                    self.scroll = usize::MAX;
                } else if local_x < 43 {
                    self.return_source(bridge);
                } else if local_x < 57 {
                    self.open_interaction();
                } else if local_x < 65 {
                    self.help();
                } else if local_x < 73 {
                    self.copy_selection(false);
                } else {
                    self.clear_selection();
                }
                return Ok(());
            }
            if self.sidebar && self.sidebar_area.contains(Position::new(x, y)) {
                self.clear_selection();
                if y == self.sidebar_area.y + 1 {
                    self.change_view(self.selected.clone(), true, bridge);
                } else if y == self.sidebar_area.bottom() - 1 {
                    self.draft.input = Input::from_prompt(&json!("/add "));
                    self.edited();
                } else if y >= self.sidebar_area.y + 3 {
                    let index = self.sidebar_top + ((y - self.sidebar_area.y - 3) / 4) as usize;
                    if let Some(m) = self.members.get(index) {
                        self.change_view(string(m, "name").into(), false, bridge);
                    }
                }
                return Ok(());
            }
            if self.context.contains(Position::new(x, y)) && self.draft.quote.is_some() {
                if x >= self.context.right().saturating_sub(10) {
                    self.draft.quote = None;
                    self.need_save_draft = true;
                } else if x >= self.context.right().saturating_sub(24) {
                    let q = self.draft.quote.as_mut().unwrap();
                    q["scope"] = json!(if string(q, "scope") == "all" {
                        "source"
                    } else {
                        "all"
                    });
                    self.need_save_draft = true;
                } else {
                    self.modal = Some(Modal::Preview {
                        title: "引用预览 · Esc 返回".into(),
                        text: serde_json::to_string_pretty(self.draft.quote.as_ref().unwrap())
                            .unwrap_or_default(),
                        scroll: 0,
                    });
                }
                self.clear_selection();
                return Ok(());
            }
        }
        if self.sidebar && self.sidebar_area.contains(Position::new(x, y)) {
            match mouse.kind {
                MouseEventKind::ScrollDown => {
                    self.sidebar_top =
                        (self.sidebar_top + 1).min(self.members.len().saturating_sub(1))
                }
                MouseEventKind::ScrollUp => self.sidebar_top = self.sidebar_top.saturating_sub(1),
                _ => {}
            }
            return Ok(());
        }
        if mouse.kind == MouseEventKind::Down(MouseButton::Left)
            && self.body.contains(Position::new(x, y))
        {
            self.ignore_left_release = false;
        }
        match mouse.kind {
            MouseEventKind::ScrollUp => self.scroll_by(-1),
            MouseEventKind::ScrollDown => self.scroll_by(1),
            MouseEventKind::Down(MouseButton::Left) => {
                if !self.body.contains(Position::new(x, y)) {
                    self.clear_selection();
                    if self.input_area.contains(Position::new(x, y))
                        && y > self.input_area.y
                        && y < self.input_area.bottom().saturating_sub(1)
                    {
                        let width = self.input_area.width.saturating_sub(2).max(1) as usize;
                        let row = self.draft.input.location(width).0;
                        let top =
                            row.saturating_sub(self.input_area.height.saturating_sub(3) as usize);
                        self.draft.input.cursor_at(
                            width,
                            top + (y - self.input_area.y - 1) as usize,
                            x.saturating_sub(self.input_area.x + 1) as usize,
                        );
                        self.edited();
                    }
                    return Ok(());
                }
                let Some(point) = self.point(x, y) else {
                    return Ok(());
                };
                let interactive = ["collapse:", "tools:", "all:", "detail:", "quote:"]
                    .iter()
                    .any(|prefix| self.timeline.rows[point.row].action.starts_with(prefix));
                let count = if interactive {
                    1
                } else if let Some((t, a, b, n)) = self.last_click {
                    if t.elapsed() < Duration::from_millis(350) && a == x && b == y {
                        n % 3 + 1
                    } else {
                        1
                    }
                } else {
                    1
                };
                self.last_click = Some((Instant::now(), x, y, count));
                if count >= 2 {
                    let text = &self.timeline.rows[point.row].text;
                    let (a, b) = if count == 3 {
                        (0, text.len())
                    } else {
                        history::word_span(text, point.byte)
                    };
                    self.selection = Some(Selection {
                        anchor: Point {
                            row: point.row,
                            byte: a,
                        },
                        head: Point {
                            row: point.row,
                            byte: b,
                        },
                    });
                    self.press = None;
                    self.press_point = None;
                    self.follow = false;
                } else {
                    let inside = self
                        .selection
                        .and_then(|s| s.span(point.row, self.timeline.rows[point.row].text.len()))
                        .is_some_and(|(a, b)| a <= point.byte && point.byte < b);
                    if !inside {
                        self.selection = None;
                    }
                    self.press = Some((x, y));
                    self.press_point = Some(point);
                    self.pointer = Some((x, y));
                    self.dragging = false;
                }
            }
            MouseEventKind::Drag(MouseButton::Left) => {
                if self.press.is_some() {
                    if !self.dragging {
                        if let Some(anchor) = self.press_point {
                            self.selection = Some(Selection {
                                anchor,
                                head: anchor,
                            });
                        }
                        self.dragging = true;
                        self.follow = false;
                    }
                    self.pointer = Some((x, y));
                    if let Some(point) = self.point(x, y) {
                        if let Some(s) = self.selection.as_mut() {
                            s.head = point;
                        }
                    }
                }
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if self.press == Some((x, y)) && !self.dragging {
                    if let Some(p) = self.point(x, y) {
                        let action = self.timeline.rows[p.row].action.clone();
                        if !action.is_empty() {
                            self.action(&action, bridge);
                        }
                    }
                }
                self.press = None;
                self.press_point = None;
                self.pointer = None;
                self.dragging = false;
            }
            _ => {}
        }
        Ok(())
    }
    fn modal_scroll(&mut self, delta: isize) {
        if let Some(Modal::Preview { scroll, text, .. }) = self.modal.as_mut() {
            *scroll = scroll
                .saturating_add_signed(delta)
                .min(text.lines().count().saturating_sub(1));
        }
        if let Some(Modal::Picker {
            selected,
            items,
            filter,
            ..
        }) = self.modal.as_mut()
        {
            *selected = selected
                .saturating_add_signed(delta)
                .min(filtered_items(items, &filter.text).len().saturating_sub(1));
        }
    }
    fn choose_modal(&mut self, bridge: &Bridge) -> Result<()> {
        let Some(modal) = self.modal.take() else {
            return Ok(());
        };
        if let Modal::Picker {
            items,
            filter,
            selected,
            action,
            data,
            ..
        } = &modal
        {
            let shown = filtered_items(items, &filter.text);
            if let Some(item) = shown.get(*selected).copied() {
                match action.as_str() {
                    "forward" => {
                        self.source =
                            Some((self.selected.clone(), self.show_all, self.draft.clone()));
                        if item.value == "+new" {
                            self.pending_quote = Some(data.clone());
                            self.forward_create = true;
                            self.draft.input = Input::from_prompt(&json!("/add "));
                            self.edited();
                            self.notice("填写新成员名字和工作目录，创建后会附上引用".into());
                        } else {
                            self.change_view(item.value.clone(), false, bridge);
                            self.draft.quote = Some(data.clone());
                            self.need_save_draft = true;
                        }
                        self.clear_selection();
                    }
                    "command" => {
                        self.draft.input = Input::from_prompt(&json!(format!(
                            "{}{} ",
                            data.as_str().unwrap_or(""),
                            item.value
                        )));
                        self.edited();
                    }
                    "member-model" => {
                        bridge.send(json!({"type":"member-model","member":data["member"],"model":item.value}));
                    }
                    "member-effort" => {
                        bridge.send(json!({"type":"member-effort","member":data["member"],"model":data["model"],"effort":item.value}));
                    }
                    "approval" => {
                        let accepted = item.value == "accept";
                        let result = if string(data, "method") == "item/permissions/requestApproval"
                        {
                            json!({"permissions":if accepted{data["params"]["permissions"].clone()}else{json!({})},"scope":"turn"})
                        } else {
                            json!({"decision":item.value})
                        };
                        bridge.send(json!({"type":"answer","id":data["id"],"result":result}));
                    }
                    _ => {}
                }
            } else {
                self.modal = Some(modal);
            }
        }
        Ok(())
    }
    fn modal_key(&mut self, key: KeyEvent, bridge: &Bridge) -> Result<()> {
        if key.code == KeyCode::Esc {
            // Escape dismisses without answering; the request remains accessible.
            self.modal = None;
            return Ok(());
        }
        if key.code == KeyCode::Enter {
            if let Some(Modal::Question {
                event,
                index,
                answers,
                input,
            }) = self.modal.as_mut()
            {
                let questions = event["params"]["questions"]
                    .as_array()
                    .cloned()
                    .unwrap_or_default();
                if let Some(q) = questions.get(*index) {
                    if input.text.trim().is_empty() {
                        return Ok(());
                    }
                    answers.insert(
                        string(q, "id").into(),
                        json!({"answers":[input.text.trim()]}),
                    );
                    *index += 1;
                    *input = Input::default();
                }
                if *index >= questions.len() {
                    bridge.send(
                        json!({"type":"answer","id":event["id"],"result":{"answers":answers}}),
                    );
                    self.modal = None;
                }
            } else {
                self.choose_modal(bridge)?;
            }
            return Ok(());
        }
        match self.modal.as_mut() {
            Some(Modal::Picker {
                filter, selected, ..
            }) => match key.code {
                KeyCode::Down => self.modal_scroll(1),
                KeyCode::Up => self.modal_scroll(-1),
                KeyCode::Char(c)
                    if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
                {
                    filter.insert(&c.to_string());
                    *selected = 0;
                }
                KeyCode::Backspace => {
                    filter.backspace();
                    *selected = 0;
                }
                _ => {}
            },
            Some(Modal::Preview { .. }) => match key.code {
                KeyCode::Down => self.modal_scroll(1),
                KeyCode::Up => self.modal_scroll(-1),
                KeyCode::PageDown => self.modal_scroll(10),
                KeyCode::PageUp => self.modal_scroll(-10),
                _ => {}
            },
            Some(Modal::Question { input, .. }) => edit_key(input, key),
            None => {}
        }
        Ok(())
    }
    fn key(&mut self, key: KeyEvent, bridge: &Bridge) -> Result<()> {
        if key.kind == KeyEventKind::Release {
            return Ok(());
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        if ctrl && key.code == KeyCode::Char('q') {
            self.quit = true;
            return Ok(());
        }
        if self.modal.is_some() {
            return self.modal_key(key, bridge);
        }
        // Unhandled macOS commands must not become literal draft characters
        // now that the terminal reports Super/Meta keys to the application.
        if key
            .modifiers
            .intersects(KeyModifiers::SUPER | KeyModifiers::META)
            && !is_copy_key(key)
        {
            return Ok(());
        }
        match key.code {
            KeyCode::Char('c' | 'C') if is_copy_key(key) => {
                self.copy_selection(key.modifiers == KeyModifiers::CONTROL)
            }
            KeyCode::Char('f') if ctrl => self.forward(),
            KeyCode::Char('b') if ctrl => {
                self.sidebar = !self.sidebar;
                self.clear_selection();
                self.timeline.invalidate();
                self.rebuild = true;
            }
            KeyCode::Char('l') if ctrl => {
                self.change_view(self.selected.clone(), !self.show_all, bridge)
            }
            KeyCode::Char('n') if ctrl => {
                self.draft.input = Input::from_prompt(&json!("/add "));
                self.edited();
            }
            KeyCode::Char('t') if ctrl => {
                let start = self.scroll;
                let end = (start + self.body.height as usize).min(self.timeline.rows.len());
                let groups = self.timeline.rows[start..end]
                    .iter()
                    .filter_map(|r| r.action.strip_prefix("tools:").map(str::to_owned))
                    .collect::<HashSet<_>>();
                let open = groups.iter().any(|k| !self.timeline.groups.contains(k));
                for k in groups {
                    if open {
                        self.timeline.groups.insert(k.clone());
                    } else {
                        self.timeline.groups.remove(&k);
                    }
                    if let Some((id, _)) = k.rsplit_once(':') {
                        self.changed.insert(id.into());
                    }
                }
                self.clear_selection();
                self.rebuild = true;
            }
            KeyCode::F(1) => self.help(),
            KeyCode::Esc => {
                self.clear_selection();
                self.completion_hidden = true;
                self.candidates.clear();
                self.completion_due = None;
            }
            KeyCode::Tab => {
                if self.candidates.is_empty() {
                    self.complete(bridge, true);
                } else {
                    self.accept_completion(bridge);
                }
            }
            KeyCode::Up if !self.candidates.is_empty() => {
                self.candidate = self.candidate.saturating_sub(1)
            }
            KeyCode::Down if !self.candidates.is_empty() => {
                self.candidate = (self.candidate + 1).min(self.candidates.len() - 1)
            }
            KeyCode::Up => {
                if self.history_at.is_some() || self.draft.input.text.is_empty() {
                    self.history_move(-1);
                } else {
                    self.draft
                        .input
                        .vertical(self.input_area.width.saturating_sub(2) as usize, -1);
                    self.edited();
                }
            }
            KeyCode::Down => {
                if self.history_at.is_some() {
                    self.history_move(1);
                } else {
                    self.draft
                        .input
                        .vertical(self.input_area.width.saturating_sub(2) as usize, 1);
                    self.edited();
                }
            }
            KeyCode::Enter if alt => {
                self.draft.input.expand();
                self.edited();
            }
            KeyCode::Enter if key.modifiers.contains(KeyModifiers::SHIFT) => {
                self.draft.input.insert("\n");
                self.edited();
            }
            KeyCode::Enter => {
                if !self.candidates.is_empty() {
                    self.accept_completion(bridge);
                } else {
                    self.submit(bridge);
                }
            }
            KeyCode::End if ctrl => {
                self.clear_selection();
                self.follow = true;
                self.scroll = usize::MAX;
            }
            KeyCode::PageUp => {
                self.scroll_by(-(self.body.height.saturating_sub(1).max(1) as isize))
            }
            KeyCode::PageDown => self.scroll_by(self.body.height.saturating_sub(1).max(1) as isize),
            KeyCode::Char(c) if ctrl && ('1'..='9').contains(&c) => {
                if let Some(m) = self.members.get((c as u8 - b'1') as usize) {
                    self.change_view(string(m, "name").into(), false, bridge);
                }
            }
            _ => {
                // Keep the submitted snapshot until ACK; edits made while it is
                // in flight are allowed and won't be cleared by its response.
                edit_key(&mut self.draft.input, key);
                self.edited();
            }
        }
        Ok(())
    }
    fn paste(&mut self, text: String, bridge: &Bridge) {
        if let Some(Modal::Picker { filter, .. }) = self.modal.as_mut() {
            filter.insert(&text);
            return;
        }
        if let Some(Modal::Question { input, .. }) = self.modal.as_mut() {
            input.insert(&text);
            return;
        }
        if self.modal.is_some() {
            return;
        }
        let images = paste::images(&text, &self.cwd);
        self.draft.input.paste(&text, &images);
        self.edited();
        let _ = bridge;
    }
}

fn filtered_items<'a>(items: &'a [Item], query: &str) -> Vec<&'a Item> {
    let query = query.to_lowercase();
    let labels = items
        .iter()
        .filter(|i| i.label.to_lowercase().contains(&query))
        .collect::<Vec<_>>();
    if !labels.is_empty() {
        labels
    } else {
        items
            .iter()
            .filter(|i| i.detail.to_lowercase().contains(&query))
            .collect()
    }
}
fn merge_drafts(mut old: Draft, new: Draft) -> Draft {
    if !new.input.text.is_empty() {
        old.input.state.cursor = old.input.text.len();
        if !old.input.text.is_empty() {
            old.input.insert("\n");
        }
        let offset = old.input.text.len();
        old.input.insert(&new.input.text);
        old.input
            .state
            .attachments
            .extend(new.input.attachments.iter().cloned().map(|mut a| {
                a.start += offset;
                a.end += offset;
                a
            }));
    }
    if old.quote.is_none() {
        old.quote = new.quote;
    }
    old
}
fn edit_key(input: &mut Input, key: KeyEvent) {
    let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
    match key.code {
        KeyCode::Char('u') if ctrl => input.clear(),
        KeyCode::Char('z') if ctrl => input.undo(),
        KeyCode::Char('y') if ctrl => input.redo(),
        KeyCode::Char('j') if ctrl => input.insert("\n"),
        KeyCode::Char('a') if ctrl => input.state.cursor = 0,
        KeyCode::Char('e') if ctrl => input.state.cursor = input.text.len(),
        KeyCode::Char(c)
            if !ctrl
                && !key
                    .modifiers
                    .intersects(KeyModifiers::ALT | KeyModifiers::SUPER | KeyModifiers::META) =>
        {
            input.insert(&c.to_string())
        }
        KeyCode::Backspace => input.backspace(),
        KeyCode::Delete => input.delete(),
        KeyCode::Left => input.left(),
        KeyCode::Right => input.right(),
        KeyCode::Home => input.state.cursor = 0,
        KeyCode::End => input.state.cursor = input.text.len(),
        _ => {}
    }
}
fn is_copy_key(key: KeyEvent) -> bool {
    matches!(key.code, KeyCode::Char('c' | 'C'))
        && key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::SUPER | KeyModifiers::META)
        && !key.modifiers.contains(KeyModifiers::ALT)
}
fn copy(text: &str) -> io::Result<()> {
    let (program, args): (&str, &[&str]) = if cfg!(target_os = "macos") {
        ("pbcopy", &[])
    } else if cfg!(windows) {
        ("clip.exe", &[])
    } else if env::var_os("WAYLAND_DISPLAY").is_some() {
        ("wl-copy", &[])
    } else {
        ("xclip", &["-selection", "clipboard"])
    };
    let mut child = Command::new(program)
        .args(args)
        .stdin(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("clipboard stdin"))?
        .write_all(text.as_bytes())?;
    if !child.wait()?.success() {
        return Err(io::Error::other("clipboard helper failed"));
    }
    Ok(())
}

fn run(args: Vec<String>) -> Result<()> {
    let mut bridge = Bridge::start(&args)?;
    let (mut terminal, guard) = terminal::setup(true)?;
    let mut app = App::new(terminal.size()?.width as usize);
    let mut dirty = true;
    let mut last_draw = Instant::now() - Duration::from_secs(1);
    let mut last_auto = Instant::now();
    let mut last_tick = Instant::now();
    let mut last_save = Instant::now();
    while !app.quit {
        let start = Instant::now();
        while let Ok(message) = bridge.events.try_recv() {
            app.on_service(message);
            dirty = true;
            if start.elapsed() > Duration::from_millis(4) {
                break;
            }
        }
        if app.completion_due.is_some_and(|t| Instant::now() >= t) {
            app.complete(&bridge, false);
        }
        if last_auto.elapsed() >= Duration::from_millis(30) {
            dirty |= app.auto_scroll();
            last_auto = Instant::now();
        }
        if last_tick.elapsed() >= Duration::from_millis(100) {
            if app
                .members
                .iter()
                .any(|m| m["busy"].as_bool() == Some(true))
            {
                dirty = true;
            }
            last_tick = Instant::now();
        }
        if app.need_save_draft && last_save.elapsed() >= Duration::from_millis(500) {
            app.save_drafts(&bridge);
            last_save = Instant::now();
        }
        if event::poll(Duration::from_millis(4))? {
            let changed = match event::read()? {
                Event::Key(k) => {
                    app.key(k, &bridge)?;
                    true
                }
                Event::Mouse(m) => {
                    if m.kind == MouseEventKind::Moved {
                        false
                    } else {
                        app.mouse(m, &bridge)?;
                        true
                    }
                }
                Event::Paste(text) => {
                    app.paste(text, &bridge);
                    true
                }
                Event::Resize(_, _) => {
                    terminal.autoresize()?;
                    app.clear_selection();
                    app.timeline.invalidate();
                    app.rebuild = true;
                    true
                }
                _ => false,
            };
            dirty |= changed;
        }
        if dirty && last_draw.elapsed() >= Duration::from_millis(8) {
            terminal.draw(|f| app.draw(f))?;
            dirty = false;
            last_draw = Instant::now();
        }
    }
    app.save_drafts(&bridge);
    drop(guard); // Shell input modes recover before any slow cancellation.
    eprintln!("正在保存会话并停止尚未完成的 Codex…");
    bridge.close();
    Ok(())
}
fn main() {
    let args = env::args().skip(1).collect::<Vec<_>>();
    if args.iter().any(|a| a == "--help" || a == "-h") {
        println!(
            "Codex Room · Rust Fullscreen\n\nuv run codex_room [--cwd PATH] [--demo]\nuv run codex_room --legacy-ui   旧 Textual 界面\n\nF1 帮助 · Ctrl+Q 退出"
        );
        return;
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        eprintln!("请在交互式终端运行 Codex Room。");
        std::process::exit(1);
    }
    if let Err(e) = run(args) {
        terminal::restore();
        eprintln!("Codex Room: {e}");
        std::process::exit(1);
    }
}
