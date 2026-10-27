//! Composer-adjacent settings. Selection is transactional until effort is chosen.
use super::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Setting {
    Member,
    Model,
    Effort,
    Fast,
}

pub(super) struct Pane {
    title: String,
    items: Vec<Item>,
    filter: Input,
    selected: usize,
    current: String,
    area: Rect,
    top: usize,
    details: bool,
}
impl Pane {
    fn new(title: &str, items: Vec<Item>, current: &str) -> Self {
        let selected = items.iter().position(|i| i.value == current).unwrap_or(0);
        Self {
            title: title.into(),
            items,
            filter: Input::default(),
            selected,
            current: current.into(),
            area: Rect::default(),
            top: 0,
            details: true,
        }
    }
    fn item_height(&self) -> u16 {
        if self.details { 2 } else { 1 }
    }
    fn visible_items(&self) -> usize {
        (self.area.height.saturating_sub(4) / self.item_height()) as usize
    }
    fn preferred_height(&self) -> u16 {
        (filtered_items(&self.items, &self.filter.text).len().min(12) as u16 * self.item_height()
            + 4)
        .max(6)
    }
    fn shift(&mut self, delta: isize) {
        let count = filtered_items(&self.items, &self.filter.text).len();
        self.selected = self
            .selected
            .saturating_add_signed(delta)
            .min(count.saturating_sub(1));
    }
    fn chosen(&self) -> Option<Item> {
        filtered_items(&self.items, &self.filter.text)
            .get(self.selected)
            .map(|i| (*i).clone())
    }
}

pub(super) struct SettingsMenu {
    kind: Setting,
    member: String,
    request_id: u64,
    left: Pane,
    right: Option<Pane>,
    right_focus: bool,
    model: String,
    loading: bool,
}

fn items(value: &Value) -> Vec<Item> {
    value["values"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|v| Item {
            label: v["label"].as_str().unwrap_or(string(v, "value")).into(),
            detail: string(v, "detail").into(),
            value: string(v, "value").into(),
        })
        .collect()
}

fn compact(text: &str, width: usize) -> String {
    if Span::raw(text).width() <= width {
        return text.into();
    }
    let mut result = String::new();
    let mut cells = 0;
    for c in text.chars() {
        let n = Span::raw(c.to_string()).width();
        if cells + n + 1 > width {
            break;
        }
        result.push(c);
        cells += n;
    }
    result.push('…');
    result
}

impl App {
    pub(super) fn draw_settings_bar(&mut self, f: &mut Frame, area: Rect) {
        self.settings_bar = area;
        self.setting_hits.clear();
        self.attention_hit = Rect::default();
        let member = self
            .members
            .iter()
            .find(|m| string(m, "name") == self.selected)
            .cloned()
            .unwrap_or(Value::Null);
        if member.is_null() {
            let width = 7.min(area.width);
            let hit = Rect::new(area.x, area.y, width, area.height);
            f.render_widget(Line::styled("成员 ▾", ACCENT), hit);
            self.setting_hits.push((hit, Setting::Member));
            f.render_widget(
                Line::styled(" · /add 创建 Codex", MUTED),
                Rect::new(area.x + width, area.y, area.width - width, area.height),
            );
            return;
        }
        let identity = color(string(&member, "color"));
        let unread = self.unread_count();
        let badge = if unread > 0 {
            format!(" [{unread} 待查看]")
        } else {
            String::new()
        };
        let badge_width = Span::raw(&badge).width() as u16;
        let budget = (area.width.saturating_sub(30 + badge_width) as usize / 2).clamp(4, 32);
        let fast = if !string(&member, "parent_name").is_empty() {
            "Fast 父会话管理"
        } else {
            match member["fast_enabled"].as_bool() {
                Some(true) => "Fast 开",
                Some(false) => "Fast 关",
                None => "Fast 未知",
            }
        };
        let controls = [
            (
                Setting::Member,
                compact(&format!("@{} ▾", string(&member, "label")), budget),
                identity,
            ),
            (
                Setting::Model,
                compact(
                    &format!(
                        "{} ▾",
                        member["model"]
                            .as_str()
                            .filter(|s| !s.is_empty())
                            .unwrap_or("模型默认")
                    ),
                    budget,
                ),
                MUTED,
            ),
            (
                Setting::Effort,
                format!(
                    "{} ▾",
                    member["effort"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("默认")
                ),
                MUTED,
            ),
            (
                Setting::Fast,
                format!("{fast} ▾"),
                if member["fast_enabled"] == true {
                    FG
                } else {
                    MUTED
                },
            ),
        ];
        let mut x = area.x;
        for (n, (setting, label, fg)) in controls.into_iter().enumerate() {
            if n > 0 && x < area.right() {
                let width = 3.min(area.right() - x);
                f.render_widget(
                    Line::styled(" · ", MUTED),
                    Rect::new(x, area.y, width, area.height),
                );
                x += width;
            }
            let width = (Span::raw(&label).width() as u16).min(area.right().saturating_sub(x));
            if width > 0 {
                let rect = Rect::new(x, area.y, width, area.height);
                f.render_widget(Line::styled(label, fg), rect);
                self.setting_hits.push((rect, setting));
                x += width;
            }
            if n == 0 && badge_width > 0 && x < area.right() {
                let width = badge_width.min(area.right() - x);
                self.attention_hit = Rect::new(x, area.y, width, area.height);
                f.render_widget(
                    Line::styled(&badge, Style::default().fg(FG).bg(Color::Rgb(42, 46, 42))),
                    self.attention_hit,
                );
                x += width;
            }
        }
        if x < area.right() {
            f.render_widget(
                Line::styled(
                    format!(
                        " · {} · {}",
                        if string(&member, "parent_name").is_empty() {
                            string(&member, "permission")
                        } else {
                            "权限由父会话管理"
                        },
                        string(&member, "cwd")
                    ),
                    MUTED,
                ),
                Rect::new(x, area.y, area.right() - x, area.height),
            );
        }
    }

    fn member_options(&self) -> Vec<Item> {
        let mut values = self.member_items();
        values.insert(
            0,
            Item {
                label: "全部消息".into(),
                detail: "查看聊天室，保留当前发送对象".into(),
                value: "+all".into(),
            },
        );
        values.push(Item {
            label: "＋ 添加 Codex".into(),
            detail: "填写 /add 名字，创建新成员".into(),
            value: "+new".into(),
        });
        values
    }

    pub(super) fn toggle_member_menu(&mut self) {
        if self
            .settings
            .as_ref()
            .is_some_and(|m| m.kind == Setting::Member)
        {
            self.settings = None;
            return;
        }
        self.clear_selection();
        self.candidates.clear();
        self.completion_due = None;
        self.completion_hidden = true;
        let current = if self.show_all {
            "+all"
        } else {
            &self.selected
        };
        self.settings = Some(SettingsMenu {
            kind: Setting::Member,
            member: self.selected.clone(),
            request_id: 0,
            left: Pane::new("切换对话对象", self.member_options(), current),
            right: None,
            right_focus: false,
            model: String::new(),
            loading: false,
        });
    }

    pub(super) fn show_unread_member(&mut self, first: Option<String>) {
        if !self
            .settings
            .as_ref()
            .is_some_and(|menu| menu.kind == Setting::Member)
        {
            self.toggle_member_menu();
        }
        if let Some(menu) = self.settings.as_mut() {
            menu.left.filter = Input::default();
            if let Some(first) = first {
                menu.left.selected = menu
                    .left
                    .items
                    .iter()
                    .position(|item| item.value == first)
                    .unwrap_or(0);
            }
        }
    }

    fn refresh_member_menu(&mut self) {
        if !self
            .settings
            .as_ref()
            .is_some_and(|m| m.kind == Setting::Member)
        {
            return;
        }
        let values = self.member_options();
        let busy = self.members.iter().filter(|m| m["busy"] == true).count();
        let unread = self.unread_count();
        let menu = self.settings.as_mut().unwrap();
        // Refresh live status without reordering members or losing a filtered
        // keyboard/mouse choice as another member starts or finishes work.
        let chosen = menu.left.chosen().map(|i| i.value);
        menu.left.items = values;
        if let Some(value) = chosen {
            menu.left.selected = filtered_items(&menu.left.items, &menu.left.filter.text)
                .iter()
                .position(|item| item.value == value)
                .unwrap_or(0);
        }
        menu.left.title = if unread > 0 {
            format!("切换对话对象 · {busy} 工作中 · {unread} 待查看")
        } else {
            format!(
                "切换对话对象 · {busy} 工作中 / {} 空闲",
                self.members.len() - busy
            )
        };
    }

    pub(super) fn open_setting(&mut self, kind: Setting, bridge: &Bridge) {
        if kind == Setting::Member {
            self.toggle_member_menu();
            return;
        }
        if self.settings.as_ref().is_some_and(|m| m.kind == kind) {
            self.settings = None;
            return;
        }
        let member = self
            .members
            .iter()
            .find(|m| string(m, "name") == self.selected)
            .cloned();
        let Some(member) = member else {
            return;
        };
        if !string(&member, "parent_name").is_empty() {
            self.notice("子代理的模型、思考强度和 Fast 由父会话管理，请切到父成员设置".into());
            self.settings = None;
            return;
        }
        self.clear_selection();
        self.candidates.clear();
        self.completion_due = None;
        self.completion_hidden = true;
        let request_id = self.id();
        let (title, values, current, loading) = match kind {
            Setting::Member => unreachable!(),
            Setting::Fast => {
                let supported = member["fast_supported"].as_bool() != Some(false);
                let values = vec![
                    Item {
                        label: "Fast 开启".into(),
                        detail: if supported {
                            "下一轮使用 Fast"
                        } else {
                            "当前模型不支持 Fast"
                        }
                        .into(),
                        value: if supported { "on" } else { "unsupported" }.into(),
                    },
                    Item {
                        label: "Fast 关闭".into(),
                        detail: "下一轮使用标准速度".into(),
                        value: "off".into(),
                    },
                ];
                (
                    "Fast 模式",
                    values,
                    if member["fast_enabled"] == true {
                        "on"
                    } else {
                        "off"
                    }
                    .into(),
                    false,
                )
            }
            Setting::Model => ("选择模型", vec![], String::new(), true),
            Setting::Effort => ("选择思考强度", vec![], String::new(), true),
        };
        let mut left = Pane::new(title, values, &current);
        left.details = !matches!(kind, Setting::Model | Setting::Effort);
        self.settings = Some(SettingsMenu {
            kind,
            member: self.selected.clone(),
            request_id,
            left,
            right: None,
            right_focus: false,
            model: string(&member, "model").into(),
            loading,
        });
        if matches!(kind, Setting::Model | Setting::Effort) {
            bridge.send(
                json!({"type":"member-settings","member":self.selected,"id":request_id,
                "setting":if kind == Setting::Model {"model"} else {"effort"}}),
            );
        }
    }

    pub(super) fn receive_setting(&mut self, value: &Value) {
        let Some(menu) = self.settings.as_mut() else {
            return;
        };
        if value["request_id"].as_u64() != Some(menu.request_id)
            || string(&value["data"], "member") != menu.member
        {
            return;
        }
        let action = string(value, "action");
        let mut pane = Pane::new(
            if action == "member-model" {
                "选择模型"
            } else {
                "选择思考强度"
            },
            items(value),
            string(value, "selected"),
        );
        pane.details = false;
        if action == "member-model" && menu.kind == Setting::Model {
            menu.left = pane;
            menu.loading = false;
        } else if action == "member-effort" && menu.kind == Setting::Model {
            if string(&value["data"], "model") != menu.model {
                return;
            }
            menu.right = Some(pane);
            menu.right_focus = true;
            menu.loading = false;
        } else if action == "member-effort" && menu.kind == Setting::Effort {
            menu.left = pane;
            menu.model = string(&value["data"], "model").into();
            menu.loading = false;
        }
    }

    pub(super) fn setting_error(&mut self, value: &Value) {
        if self
            .settings
            .as_ref()
            .is_some_and(|menu| value["request_id"].as_u64() == Some(menu.request_id))
        {
            self.settings = None;
            self.notice(string(value, "text").into());
        }
    }

    fn choose_setting(&mut self, bridge: &Bridge) {
        let Some(mut menu) = self.settings.take() else {
            return;
        };
        let pane = if menu.right_focus {
            menu.right.as_ref()
        } else {
            Some(&menu.left)
        };
        let Some(item) = pane.and_then(Pane::chosen) else {
            self.settings = Some(menu);
            return;
        };
        match menu.kind {
            Setting::Member => {
                if item.value == "+all" {
                    self.change_view(self.selected.clone(), true, bridge);
                } else if item.value == "+new" {
                    self.draft.input = Input::from_prompt(&json!("/add "));
                    self.edited();
                } else if self.members.iter().any(|m| string(m, "name") == item.value) {
                    self.change_view(item.value, false, bridge);
                } else {
                    self.notice("成员列表已变化，请重新选择".into());
                }
            }
            Setting::Fast => {
                if item.value == "unsupported" {
                    self.notice("当前模型不支持 Fast，请选择其他模型或关闭 Fast".into());
                } else {
                    bridge.send(json!({"type":"member-fast","member":menu.member,"enabled":item.value=="on"}));
                }
            }
            Setting::Model if !menu.right_focus => {
                menu.model = item.value.clone();
                menu.left.current = item.value;
                menu.right = None;
                menu.right_focus = true;
                menu.loading = true;
                menu.request_id = self.id();
                bridge.send(
                    json!({"type":"member-model","member":menu.member,"model":menu.model,
                    "placement":"status","request_id":menu.request_id}),
                );
                self.settings = Some(menu);
            }
            _ => {
                bridge.send(json!({"type":"member-effort","member":menu.member,"model":menu.model,"effort":item.value}));
            }
        }
    }

    pub(super) fn setting_paste(&mut self, text: &str) {
        let Some(menu) = self.settings.as_mut() else {
            return;
        };
        let pane = if menu.right_focus {
            menu.right.as_mut()
        } else {
            Some(&mut menu.left)
        };
        if let Some(pane) = pane {
            pane.filter
                .insert(&text.lines().collect::<Vec<_>>().join(" "));
            pane.selected = 0;
        }
    }

    pub(super) fn setting_key(&mut self, key: KeyEvent, bridge: &Bridge) {
        if key.code == KeyCode::Esc {
            self.settings = None;
            return;
        }
        if key.code == KeyCode::Enter {
            self.choose_setting(bridge);
            return;
        }
        let Some(menu) = self.settings.as_mut() else {
            return;
        };
        if key.code == KeyCode::Left {
            menu.right_focus = false;
            return;
        }
        if matches!(key.code, KeyCode::Right | KeyCode::Tab) && menu.right.is_some() {
            menu.right_focus = !menu.right_focus;
            return;
        }
        let pane = if menu.right_focus {
            menu.right.as_mut()
        } else {
            Some(&mut menu.left)
        };
        let Some(pane) = pane else {
            return;
        };
        match key.code {
            KeyCode::Down => pane.shift(1),
            KeyCode::Up => pane.shift(-1),
            KeyCode::Char(c)
                if key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT =>
            {
                pane.filter.insert(&c.to_string());
                pane.selected = 0;
            }
            KeyCode::Backspace => {
                pane.filter.backspace();
                pane.selected = 0;
            }
            _ => {}
        }
    }

    pub(super) fn setting_mouse(&mut self, mouse: MouseEvent, bridge: &Bridge) {
        let point = Position::new(mouse.column, mouse.row);
        let Some(menu) = self.settings.as_mut() else {
            return;
        };
        let right = menu.right.as_ref().is_some_and(|p| p.area.contains(point));
        let left = menu.left.area.contains(point);
        if mouse.kind == MouseEventKind::Down(MouseButton::Left) && !left && !right {
            self.settings = None;
            return;
        }
        if !left && !right {
            return;
        }
        menu.right_focus = right;
        let pane = if right {
            menu.right.as_mut().unwrap()
        } else {
            &mut menu.left
        };
        match mouse.kind {
            MouseEventKind::ScrollUp => pane.shift(-1),
            MouseEventKind::ScrollDown => pane.shift(1),
            MouseEventKind::Down(MouseButton::Left) => {
                let list_y = pane.area.y + 2;
                let item_height = pane.item_height();
                let list_end = list_y + pane.visible_items() as u16 * item_height;
                if mouse.row >= list_y && mouse.row < list_end {
                    let index = pane.top + ((mouse.row - list_y) / item_height) as usize;
                    if index < filtered_items(&pane.items, &pane.filter.text).len() {
                        pane.selected = index;
                        self.choose_setting(bridge);
                    }
                }
            }
            _ => {}
        }
    }

    pub(super) fn draw_setting_menu(&mut self, f: &mut Frame, main: Rect) {
        self.refresh_member_menu();
        let Some(menu) = self.settings.as_mut() else {
            return;
        };
        // A resize may hide a pane; never retain its old clickable coordinates.
        menu.left.area = Rect::default();
        if let Some(right) = menu.right.as_mut() {
            right.area = Rect::default();
        }
        let anchor = self
            .setting_hits
            .iter()
            .find(|(_, kind)| *kind == menu.kind)
            .map(|(r, _)| r.x)
            .unwrap_or(main.x);
        let space = self.settings_bar.y.saturating_sub(main.y + 1);
        if space < 3 || main.width < 4 {
            return;
        }
        let height = space.min(16).min(
            menu.left
                .preferred_height()
                .max(menu.right.as_ref().map(Pane::preferred_height).unwrap_or(6)),
        );
        let two = menu.kind == Setting::Model
            && (menu.right.is_some() || menu.right_focus)
            && main.width >= 52;
        let left_width = if two {
            (main.width.saturating_sub(1) / 2).min(46)
        } else {
            main.width
                .min(if menu.kind == Setting::Member { 54 } else { 44 })
        };
        let right_width = if two {
            main.width.saturating_sub(left_width + 1).min(42)
        } else {
            0
        };
        let total = left_width + if two { 1 + right_width } else { 0 };
        let x = anchor.min(main.right().saturating_sub(total)).max(main.x);
        let area = Rect::new(x, self.settings_bar.y - height, left_width, height);
        let members = &self.members;
        if !two && menu.right_focus {
            menu.left.area = Rect::default();
            if let Some(right) = menu.right.as_mut() {
                draw_pane(f, right, area, true, members, false, &self.interactions);
            } else {
                draw_loading(f, area, "读取思考强度…");
            }
        } else {
            draw_pane(
                f,
                &mut menu.left,
                area,
                !menu.right_focus,
                members,
                menu.kind == Setting::Member,
                &self.interactions,
            );
            if two {
                let rect = Rect::new(x + left_width + 1, area.y, right_width, height);
                if let Some(right) = menu.right.as_mut() {
                    draw_pane(
                        f,
                        right,
                        rect,
                        menu.right_focus,
                        members,
                        false,
                        &self.interactions,
                    );
                } else {
                    draw_loading(f, rect, "读取思考强度…");
                }
            } else if menu.loading {
                draw_loading(f, area, "正在读取选项…");
            }
        }
    }
}

fn draw_loading(f: &mut Frame, area: Rect, title: &str) {
    f.render_widget(Clear, area);
    f.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .title(title)
            .style(Style::default().fg(MUTED).bg(BG)),
        area,
    );
}
fn draw_pane(
    f: &mut Frame,
    pane: &mut Pane,
    area: Rect,
    focused: bool,
    members: &[Value],
    member_menu: bool,
    interactions: &[Value],
) {
    pane.area = area;
    f.render_widget(Clear, area);
    f.render_widget(
        Block::default()
            .borders(Borders::ALL)
            .title(pane.title.as_str())
            .border_style(if focused { ACCENT } else { MUTED })
            .style(Style::default().fg(FG).bg(BG)),
        area,
    );
    let width = area.width.saturating_sub(2);
    f.render_widget(
        Line::styled(format!("筛选：{}", pane.filter.text), MUTED),
        Rect::new(area.x + 1, area.y + 1, width, 1),
    );
    let shown = filtered_items(&pane.items, &pane.filter.text);
    pane.selected = pane.selected.min(shown.len().saturating_sub(1));
    let visible = pane.visible_items();
    pane.top = pane.selected.saturating_sub(visible.saturating_sub(1));
    for (index, item) in shown.iter().enumerate().skip(pane.top).take(visible) {
        let y = area.y + 2 + (index - pane.top) as u16 * pane.item_height();
        let member = if member_menu {
            members.iter().find(|m| string(m, "name") == item.value)
        } else {
            None
        };
        let fg = member.map(|m| color(string(m, "color"))).unwrap_or(FG);
        let waiting = member.is_some_and(|m| {
            interactions
                .iter()
                .any(|e| string(e, "_member") == string(m, "name"))
        });
        let unread = member.is_some_and(|m| !string(m, "unread_entry_id").is_empty());
        let status = member.map(|m| {
            if waiting {
                "待回复"
            } else if m["busy"] == true {
                "⠋ 工作中"
            } else if unread {
                "结果未读"
            } else {
                "○ 空闲"
            }
        });
        let status_width = status
            .map(|s| Span::raw(s).width() as u16 + 2)
            .unwrap_or(0)
            .min(width.saturating_sub(4));
        let label_width = width.saturating_sub(status_width);
        let bg = if index == pane.selected {
            Color::Rgb(48, 54, 55)
        } else if unread {
            Color::Rgb(38, 43, 40)
        } else {
            BG
        };
        f.render_widget(
            Line::from(vec![
                Span::styled(
                    if item.value == pane.current {
                        "✓ "
                    } else {
                        "  "
                    },
                    MUTED,
                ),
                Span::styled(
                    if member_menu {
                        if unread { "● " } else { "  " }
                    } else {
                        ""
                    },
                    fg,
                ),
                Span::styled(
                    compact(
                        &item.label,
                        label_width.saturating_sub(if member_menu { 4 } else { 2 }) as usize,
                    ),
                    fg,
                ),
            ])
            .style(Style::default().bg(bg)),
            Rect::new(area.x + 1, y, label_width, 1),
        );
        if let Some(status) = status {
            f.render_widget(
                Line::styled(
                    status,
                    Style::default()
                        .fg(
                            if member.is_some_and(|m| m["busy"] == true) || waiting || unread {
                                FG
                            } else {
                                MUTED
                            },
                        )
                        .bg(bg),
                )
                .right_aligned(),
                Rect::new(area.right() - 1 - status_width, y, status_width, 1),
            );
        }
        if pane.details {
            let detail = member
                .map(|m| {
                    let parent = members
                        .iter()
                        .find(|p| string(p, "name") == string(m, "parent_name"));
                    format!(
                        "{}{}",
                        parent
                            .map(|p| format!("子代理 · @{} · ", string(p, "label")))
                            .unwrap_or_default(),
                        string(m, "cwd")
                    )
                })
                .unwrap_or_else(|| item.detail.clone());
            f.render_widget(
                Line::styled(
                    format!("  {}", compact(&detail, width.saturating_sub(2) as usize)),
                    MUTED,
                ),
                Rect::new(area.x + 1, y + 1, width, 1),
            );
        }
    }
    if shown.is_empty() {
        f.render_widget(
            Line::styled("没有匹配项", MUTED),
            Rect::new(area.x + 1, area.y + 2, width, 1),
        );
    }
    f.render_widget(
        Line::styled("↑↓ / 滚轮 · Enter 确认 · Esc 取消", MUTED),
        Rect::new(area.x + 1, area.bottom().saturating_sub(2), width, 1),
    );
    if focused && width > 6 {
        let x = (area.x + 7 + Span::raw(&pane.filter.text[..pane.filter.cursor]).width() as u16)
            .min(area.right() - 2);
        f.set_cursor_position((x, area.y + 1));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn app() -> App {
        let mut app = App::new(130);
        app.members = vec![json!({"name":"A","label":"项目负责人第二人很长的中文名字",
            "model":"gpt-6.1-sol","effort":"xhigh","color":"#7dcce3",
            "fast_enabled":true,"permission":"full","cwd":"/example"})];
        app.selected = "A".into();
        app.show_all = false;
        app
    }
    fn menu(kind: Setting) -> SettingsMenu {
        SettingsMenu {
            kind,
            member: "A".into(),
            request_id: 7,
            left: Pane::new(
                "选项",
                vec![Item {
                    label: "模型".into(),
                    detail: "说明".into(),
                    value: "other".into(),
                }],
                "",
            ),
            right: None,
            right_focus: false,
            model: "other".into(),
            loading: true,
        }
    }

    #[test]
    fn unicode_controls_keep_distinct_click_targets_during_notices() {
        let mut app = app();
        app.notice("正在显示一条临时提示".into());
        for width in [56, 80, 130, 160] {
            let mut terminal = Terminal::new(TestBackend::new(width, 32)).unwrap();
            terminal.draw(|f| app.draw(f)).unwrap();
            assert_eq!(app.setting_hits.len(), 4);
            for (index, (area, _)) in app.setting_hits.iter().enumerate() {
                assert_eq!(area.y, app.settings_bar.y);
                assert!(area.width > 0 && area.right() <= width);
                if index > 0 {
                    assert!(app.setting_hits[index - 1].0.right() < area.x);
                }
            }
            let member = app.setting_hits[0].0;
            assert_eq!(
                terminal.backend().buffer()[(member.x, member.y)].fg,
                color("#7dcce3")
            );
        }
    }

    #[test]
    fn cascades_anchor_above_composer_and_drop_hidden_hit_areas_on_resize() {
        let mut app = app();
        let mut selection = menu(Setting::Model);
        selection.right = Some(Pane::new("思考强度", vec![], ""));
        selection.right_focus = true;
        selection.loading = false;
        app.settings = Some(selection);
        for (width, height) in [(150, 40), (60, 32), (48, 24), (80, 32), (3, 8), (150, 40)] {
            let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
            terminal.draw(|f| app.draw(f)).unwrap();
            let menu = app.settings.as_ref().unwrap();
            for area in [menu.left.area, menu.right.as_ref().unwrap().area] {
                if !area.is_empty() {
                    assert!(area.bottom() <= app.settings_bar.y);
                    assert!(area.x >= app.input_area.x && area.right() <= width);
                }
            }
            if app.input_area.width >= 52 {
                assert!(menu.left.area.right() < menu.right.as_ref().unwrap().area.x);
            } else {
                assert!(menu.left.area.is_empty());
            }
        }
    }

    #[test]
    fn late_settings_responses_cannot_replace_another_member_or_canceled_menu() {
        let mut app = app();
        app.settings = Some(menu(Setting::Model));
        let mut response = json!({"type":"choose","placement":"status","request_id":6,
            "action":"member-model","data":{"member":"A"},"values":[]});
        app.on_service(response.clone());
        assert!(app.settings.as_ref().unwrap().loading);
        response["request_id"] = json!(7);
        response["data"]["member"] = json!("B");
        app.on_service(response.clone());
        assert!(app.settings.as_ref().unwrap().loading);
        response["data"]["member"] = json!("A");
        app.on_service(response.clone());
        assert!(!app.settings.as_ref().unwrap().loading);
        response["action"] = json!("member-effort");
        response["data"]["model"] = json!("old-model");
        app.on_service(response.clone());
        assert!(app.settings.as_ref().unwrap().right.is_none());
        response["data"]["model"] = json!("other");
        app.on_service(response.clone());
        assert!(app.settings.as_ref().unwrap().right.is_some());
        app.on_service(json!({"type":"setting-error","request_id":6,"text":"过期错误"}));
        assert!(app.settings.is_some());
        app.settings = None;
        app.on_service(response);
        assert!(app.settings.is_none());
    }

    #[test]
    fn member_menu_updates_status_and_members_without_losing_filtered_choice() {
        use unicode_width::UnicodeWidthStr;
        let mut app = app();
        app.members
            .push(json!({"name":"B","label":"B","color":"#78d8bc","busy":true,"cwd":"/example"}));
        app.toggle_member_menu();
        let pane = &mut app.settings.as_mut().unwrap().left;
        pane.filter.insert("example");
        pane.selected = 1;
        let mut terminal = Terminal::new(TestBackend::new(110, 32)).unwrap();
        let text = |buffer: &ratatui::buffer::Buffer| {
            (0..buffer.area.height)
                .map(|y| {
                    let mut text = String::new();
                    let mut x = 0;
                    while x < buffer.area.width {
                        let symbol = buffer[(x, y)].symbol();
                        text.push_str(symbol);
                        x += symbol.width().max(1) as u16;
                    }
                    text
                })
                .collect::<Vec<_>>()
                .join("\n")
        };
        terminal.draw(|f| app.draw(f)).unwrap();
        let initial = text(terminal.backend().buffer());
        assert!(
            initial
                .lines()
                .any(|line| line.contains("@B") && line.contains("工作中"))
        );
        let body = app.body;
        app.members[0]["busy"] = json!(true);
        app.members[1]["busy"] = json!(false);
        app.members[1]["label"] = json!("B 改名后很长很长的成员名称");
        app.members
            .push(json!({"name":"C","label":"C","cwd":"/example","parent_name":"A"}));
        app.on_service(json!({"type":"members","agents":app.members,"selected":"A","show_all":false,"connected":true}));
        terminal.draw(|f| app.draw(f)).unwrap();
        let pane = &app.settings.as_ref().unwrap().left;
        assert_eq!(pane.filter.text, "example");
        assert_eq!(pane.chosen().unwrap().value, "B");
        let current = text(terminal.backend().buffer());
        assert!(
            current
                .lines()
                .any(|line| line.contains("@B 改名") && line.contains("空闲"))
        );
        assert!(current.contains("1 工作中 / 2 空闲"));
        assert!(current.contains("子代理 · @"));
        assert_eq!(app.body, body);
        assert_eq!(app.input_area.width, 110);
        assert!(!current.contains("[成员]"));
        app.on_service(json!({"type":"interaction","member":"B","event":{"method":"item/tool/requestUserInput"}}));
        terminal.draw(|f| app.draw(f)).unwrap();
        assert!(
            text(terminal.backend().buffer())
                .lines()
                .any(|line| line.contains("@B 改名") && line.contains("待回复"))
        );
        app.toggle_member_menu();
        assert!(app.settings.is_none());
        assert_eq!(app.body, body);
    }

    #[test]
    fn empty_room_member_menu_has_clickable_entry_and_add_action() {
        let mut app = App::new(80);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        assert_eq!(app.setting_hits.len(), 1);
        assert!(app.setting_hits[0].1 == Setting::Member);
        app.on_service(json!({"type":"action","action":"members"}));
        terminal.draw(|f| app.draw(f)).unwrap();
        let pane = &app.settings.as_ref().unwrap().left;
        assert_eq!(pane.items.len(), 2);
        assert_eq!(pane.items.last().unwrap().value, "+new");
        assert_eq!(app.body.x, 0);
        assert_eq!(app.body.width, 80);
        app.on_service(json!({"type":"action","action":"members"}));
        assert!(app.settings.is_none());
    }

    #[test]
    fn unread_badge_opens_the_first_unread_member_without_reordering_or_acknowledging() {
        let mut app = app();
        app.members
            .push(json!({"name":"B","label":"B","busy":true,"unread_entry_id":"offscreen-result"}));
        for width in [56, 80, 130] {
            let mut terminal = Terminal::new(TestBackend::new(width, 32)).unwrap();
            terminal.draw(|f| app.draw(f)).unwrap();
            assert!(!app.attention_hit.is_empty());
            assert_eq!(app.unread_count(), 1);
            for (rect, _) in &app.setting_hits {
                assert!(rect.intersection(app.attention_hit).is_empty());
            }
        }
        app.open_unread_menu();
        assert_eq!(
            app.settings.as_ref().unwrap().left.chosen().unwrap().value,
            "B"
        );
        assert_eq!(app.members[0]["name"], "A");
        assert_eq!(app.unread_count(), 1);
        assert!(app.pending_reads.is_empty());
    }
}
