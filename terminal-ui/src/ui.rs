use super::*;
use ratatui::style::Modifier;

impl App {
    pub(super) fn draw(&mut self, f: &mut Frame) {
        let area = f.area();
        f.render_widget(Block::default().style(Style::default().fg(FG).bg(BG)), area);
        let side_width = if self.sidebar && area.width >= 65 {
            24
        } else {
            0
        };
        let main = Rect::new(
            area.x + side_width,
            area.y,
            area.width.saturating_sub(side_width),
            area.height,
        );
        let input_width = main.width.saturating_sub(4).max(1) as usize;
        let height =
            (input::location(&self.draft.input.text, input_width).0 + 1).clamp(1, 8) as u16;
        let input_height = (height + 2).min(main.height.saturating_sub(3));
        self.footer = Rect::new(main.x, main.bottom().saturating_sub(1), main.width, 1);
        self.input_area = Rect::new(
            main.x,
            self.footer.y.saturating_sub(input_height),
            main.width,
            input_height,
        );
        let context_height = if self.draft.quote.is_some() { 1 } else { 0 };
        self.context = Rect::new(
            main.x,
            self.input_area.y.saturating_sub(context_height),
            main.width,
            context_height,
        );
        let status_height = 1u16.min(self.context.y.saturating_sub(main.y));
        let status = Rect::new(
            main.x,
            self.context.y.saturating_sub(status_height),
            main.width,
            status_height,
        );
        let body_width = if self.reading_centered {
            main.width.min(95)
        } else {
            main.width
        };
        self.body = Rect::new(
            main.x + (main.width - body_width) / 2,
            main.y + 1,
            body_width,
            status.y.saturating_sub(main.y + 1),
        );
        let width = self.body.width.saturating_sub(3).max(1) as usize;
        let anchor = if self.timeline.width != width && !self.follow && !self.pending_bottom {
            self.timeline.anchor(self.scroll)
        } else {
            None
        };
        if self.timeline.width != width {
            self.clear_selection();
            self.timeline.width = width;
            self.timeline.invalidate();
            self.rebuild = true;
        }
        self.rebuild();
        if let Some(anchor) = anchor {
            if let Some(row) = self.timeline.locate(&anchor) {
                self.scroll = row;
            }
        }
        if self.pending_bottom && self.selection.is_none() && self.press.is_none() {
            self.scroll = self.maximum();
            self.follow = true;
            self.pending_bottom = false;
        }
        if self.follow && self.selection.is_none() && self.press.is_none() {
            self.scroll = self.maximum();
        }
        self.scroll = self.scroll.min(self.maximum());
        let mode = if self.demo {
            "演示 · 不调用 Codex"
        } else if self.connected {
            "已连接"
        } else {
            "离线"
        };
        let view = if self.show_all {
            "全部消息".into()
        } else {
            self.members
                .iter()
                .find(|m| string(m, "name") == self.selected)
                .map(|m| format!("@{}", string(m, "label")))
                .unwrap_or_else(|| "没有成员".into())
        };
        let busy = self
            .members
            .iter()
            .filter(|m| m["busy"].as_bool() == Some(true))
            .count();
        f.render_widget(
            Line::styled(
                format!(
                    " Codex Room · Rust · {view} · {mode} · {busy} 工作中{}",
                    if self.reading_centered {
                        " · 居中阅读"
                    } else {
                        ""
                    }
                ),
                MUTED,
            ),
            Rect::new(main.x, main.y, main.width, 1),
        );
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64();
        let spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][(now * 10.) as usize % 10];
        for (screen_y, (row_index, row)) in self
            .timeline
            .rows
            .iter()
            .enumerate()
            .skip(self.scroll)
            .take(self.body.height as usize)
            .enumerate()
        {
            let y = self.body.y + screen_y as u16;
            let e = &self.entries[row.message];
            let sender = string(e, "sender");
            let member = self
                .members
                .iter()
                .find(|m| string(m, "name") == sender)
                .unwrap_or(&Value::Null);
            let rail = if sender == "you" || sender == "system" {
                "  "
            } else {
                "▎ "
            };
            f.render_widget(
                Line::styled(rail, color(string(member, "color"))),
                Rect::new(self.body.x, y, 2.min(self.body.width), 1),
            );
            let line = if row.working {
                let elapsed = now - e["started_epoch"].as_f64().unwrap_or(now);
                Line::styled(
                    format!(
                        "{spinner} Working · {} · {}",
                        string(member, "phase"),
                        duration(elapsed)
                    ),
                    MUTED,
                )
            } else if let Some(selection) = self.selection {
                selection.highlight(row_index, row)
            } else {
                row.line.clone()
            };
            let bg = if sender == "you" {
                Color::Rgb(42, 46, 42)
            } else {
                BG
            };
            f.render_widget(
                Paragraph::new(line).style(Style::default().fg(FG).bg(bg)),
                Rect::new(self.body.x + 2, y, self.body.width.saturating_sub(3), 1),
            );
        }
        if self.timeline.rows.is_empty() {
            f.render_widget(
                Paragraph::new("用 /add 名字 [目录] 创建 Codex，或 /resume 会话ID。\n@ 点名，Tab 补全，F1 查看帮助。")
                    .style(Style::default().fg(MUTED)),
                Rect::new(
                    self.body.x + 2,
                    self.body.y + 1,
                    self.body.width.saturating_sub(4),
                    self.body.height.saturating_sub(1),
                ),
            );
        }
        if self.maximum() > 0 && self.body.height > 0 {
            let y = self.body.y
                + (self.scroll * self.body.height.saturating_sub(1) as usize / self.maximum())
                    as u16;
            f.render_widget(
                Line::styled("▐", MUTED),
                Rect::new(self.body.right().saturating_sub(1), y, 1, 1),
            );
        }
        let selected = self
            .members
            .iter()
            .find(|m| string(m, "name") == self.selected)
            .unwrap_or(&Value::Null);
        let identity_color = color(string(selected, "color"));
        let status_line = if self.notice_at.elapsed() < Duration::from_secs(7) {
            Line::styled(self.notice.clone(), MUTED)
        } else {
            Line::from(vec![
                Span::styled(format!("@{}", string(selected, "label")), identity_color),
                Span::styled(
                    format!(
                        " · {} · {} · {} · {}",
                        string(selected, "model"),
                        string(selected, "effort"),
                        if string(selected, "parent_name").is_empty() {
                            string(selected, "permission")
                        } else {
                            "权限由父会话管理"
                        },
                        string(selected, "cwd")
                    ),
                    MUTED,
                ),
            ])
        };
        f.render_widget(status_line, status);
        if let Some(quote) = &self.draft.quote {
            let scope = if string(quote, "scope") == "all" {
                "整个选区".into()
            } else {
                format!("与 @{}", string(quote, "source_label"))
            };
            let n = quote["messages"].as_array().map(Vec::len).unwrap_or(0);
            f.render_widget(
                Line::styled(
                    format!("引用：{scope} · {n} 条 · 含折叠详情（点击预览）"),
                    ACCENT,
                ),
                self.context,
            );
            let right = Rect::new(
                self.context.right().saturating_sub(24),
                self.context.y,
                24.min(self.context.width),
                1,
            );
            f.render_widget(Line::styled("[切换范围]    [移除引用]", MUTED), right);
        }
        let label = string(selected, "label");
        let title = if self.submit_pending.is_some() {
            Line::styled("正在确认发送…", MUTED)
        } else {
            Line::from(vec![
                Span::styled("给 ", MUTED),
                Span::styled(format!("@{label}"), identity_color),
                Span::styled(" 发消息 · Enter 发送 · Ctrl+J 换行", MUTED),
            ])
        };
        draw_input(f, self.input_area, &self.draft.input, title);
        for (x, width, text) in [
            (0, 10, "[成员]"),
            (10, 10, "[转发]"),
            (20, 9, "[底部]"),
            (29, 14, "[返回来源]"),
            (43, 14, "[回复请求]"),
            (57, 8, "[帮助]"),
            (65, 8, "[复制]"),
        ] {
            if x < self.footer.width {
                f.render_widget(
                    Line::styled(text, MUTED),
                    Rect::new(
                        self.footer.x + x,
                        self.footer.y,
                        width.min(self.footer.width - x),
                        1,
                    ),
                );
            }
        }
        self.sidebar_area = Rect::new(area.x, area.y, side_width, area.height);
        if side_width > 0 {
            let side = self.sidebar_area;
            f.render_widget(
                Block::default()
                    .borders(Borders::RIGHT)
                    .border_style(MUTED)
                    .style(Style::default().bg(BG)),
                side,
            );
            f.render_widget(
                Line::styled(format!(" 成员 · {}", self.members.len()), MUTED),
                Rect::new(side.x, side.y, side.width.saturating_sub(1), 1),
            );
            f.render_widget(
                Line::styled(" 全部消息", if self.show_all { ACCENT } else { MUTED }),
                Rect::new(side.x, side.y + 1, side.width.saturating_sub(1), 1),
            );
            for (i, m) in self.members.iter().skip(self.sidebar_top).enumerate() {
                let y = side.y + 3 + (i * 4) as u16;
                if y + 3 >= side.bottom().saturating_sub(1) {
                    break;
                }
                let active = string(m, "name") == self.selected && !self.show_all;
                let marker = if active { "▸" } else { " " };
                let label = string(m, "label");
                let parent = self
                    .members
                    .iter()
                    .find(|p| string(p, "name") == string(m, "parent_name"));
                let lines = vec![
                    Line::styled(
                        format!(
                            "{marker} {}{label}",
                            if parent.is_some() { "↳ " } else { "" }
                        ),
                        color(string(m, "color")),
                    ),
                    Line::styled(
                        format!(
                            "  {} {}",
                            if m["busy"].as_bool() == Some(true) {
                                "⠋ 工作中"
                            } else {
                                "● 空闲"
                            },
                            if self
                                .interactions
                                .iter()
                                .any(|e| string(e, "_member") == string(m, "name"))
                            {
                                "待回复"
                            } else {
                                ""
                            }
                        ),
                        MUTED,
                    ),
                    Line::styled(
                        format!(
                            "  {}",
                            parent
                                .map(|p| format!("子代理 · @{}", string(p, "label")))
                                .unwrap_or_else(|| string(m, "cwd")
                                    .rsplit(['/', '\\'])
                                    .find(|s| !s.is_empty())
                                    .unwrap_or("")
                                    .to_owned())
                        ),
                        MUTED,
                    ),
                ];
                f.render_widget(
                    Paragraph::new(lines).style(Style::default().bg(if active {
                        Color::Rgb(42, 46, 42)
                    } else {
                        BG
                    })),
                    Rect::new(side.x, y, side.width - 1, 3),
                );
            }
            f.render_widget(
                Line::styled(" ＋ 添加", MUTED),
                Rect::new(
                    side.x,
                    side.bottom().saturating_sub(1),
                    side.width.saturating_sub(1),
                    1,
                ),
            );
        }
        self.completion_area = Rect::default();
        if !self.candidates.is_empty() && self.modal.is_none() {
            let count = self.candidates.len().min(7).min(self.input_area.y as usize) as u16;
            let top = self
                .candidate
                .saturating_sub(count.saturating_sub(1) as usize);
            self.completion_area = Rect::new(
                main.x + 2,
                self.input_area.y.saturating_sub(count),
                main.width.saturating_sub(4),
                count,
            );
            f.render_widget(Clear, self.completion_area);
            for (i, c) in self
                .candidates
                .iter()
                .skip(top)
                .take(count as usize)
                .enumerate()
            {
                let at = top + i;
                let selected = at == self.candidate;
                let member = self.members.iter().find(|m| {
                    !string(c, "member").is_empty() && string(m, "name") == string(c, "member")
                });
                let name_color = member
                    .map(|m| color(string(m, "color")))
                    .unwrap_or(if selected { ACCENT } else { FG });
                let name_style = if selected {
                    Style::default().fg(name_color).add_modifier(Modifier::BOLD)
                } else {
                    Style::default().fg(name_color)
                };
                let line = Line::from(vec![
                    Span::styled(
                        if selected { "› " } else { "  " },
                        if selected { ACCENT } else { MUTED },
                    ),
                    Span::styled(string(c, "value").to_owned(), name_style),
                    Span::styled(format!("  {}", string(c, "detail")), MUTED),
                ]);
                f.render_widget(
                    Paragraph::new(line).style(Style::default().bg(Color::Rgb(40, 44, 40))),
                    Rect::new(
                        self.completion_area.x,
                        self.completion_area.y + i as u16,
                        self.completion_area.width,
                        1,
                    ),
                );
            }
        }
        if self.modal.is_some() {
            self.draw_modal(f, area);
        }
    }
    fn draw_modal(&mut self, f: &mut Frame, area: Rect) {
        let width = area.width.saturating_sub(4).min(110);
        let height = area.height.saturating_sub(4).min(26);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        self.modal_area = rect;
        f.render_widget(Clear, rect);
        let inner = Rect::new(
            rect.x + 1,
            rect.y + 1,
            rect.width.saturating_sub(2),
            rect.height.saturating_sub(2),
        );
        let Some(modal) = self.modal.as_mut() else {
            return;
        };
        match modal {
            Modal::Preview {
                title,
                text,
                scroll,
            } => {
                f.render_widget(
                    Block::default()
                        .borders(Borders::ALL)
                        .title(title.as_str())
                        .style(Style::default().fg(FG).bg(BG)),
                    rect,
                );
                let lines = text
                    .split('\n')
                    .flat_map(|l| history::wrap(Line::raw(l.to_owned()), inner.width as usize))
                    .collect::<Vec<_>>();
                *scroll = (*scroll).min(lines.len().saturating_sub(1));
                f.render_widget(
                    Paragraph::new(
                        lines
                            .into_iter()
                            .skip(*scroll)
                            .take(inner.height as usize)
                            .collect::<Vec<_>>(),
                    )
                    .style(Style::default().fg(FG).bg(BG)),
                    inner,
                );
            }
            Modal::Picker {
                title,
                items,
                filter,
                selected,
                action,
                data,
            } => {
                f.render_widget(
                    Block::default()
                        .borders(Borders::ALL)
                        .title(if action == "approval" {
                            "请求批准 · Esc 稍后"
                        } else {
                            title.as_str()
                        })
                        .style(Style::default().fg(FG).bg(BG)),
                    rect,
                );
                let mut start = inner.y + 2;
                if action == "approval" {
                    let details = serde_json::to_string_pretty(&data["params"]).unwrap_or_default();
                    let count = inner.height.saturating_sub(4).min(14);
                    let lines = details
                        .lines()
                        .take(count as usize)
                        .map(|l| Line::styled(l.to_owned(), MUTED))
                        .collect::<Vec<_>>();
                    f.render_widget(
                        Paragraph::new(lines),
                        Rect::new(inner.x, inner.y, inner.width, count),
                    );
                    start = inner.y + count;
                } else {
                    f.render_widget(
                        Line::styled(format!("搜索：{}", filter.text), ACCENT),
                        Rect::new(inner.x, inner.y, inner.width, 1),
                    );
                    f.set_cursor_position(Position::new(
                        (inner.x + 8 + filter.text.as_str().chars().count() as u16)
                            .min(inner.right().saturating_sub(1)),
                        inner.y,
                    ));
                    if matches!(action.as_str(), "member-model" | "member-effort") {
                        f.render_widget(
                            Line::styled("↑↓ / 鼠标选择 · Enter 确认 · Esc 保留原配置", MUTED),
                            Rect::new(inner.x, inner.y + 1, inner.width, 1),
                        );
                    }
                }
                self.modal_list_y = start;
                let available = inner.bottom().saturating_sub(start) as usize;
                let shown = filtered_items(items, &filter.text);
                *selected = (*selected).min(shown.len().saturating_sub(1));
                let top = selected.saturating_sub(available.saturating_sub(1));
                self.modal_list_top = top;
                for (i, item) in shown.iter().skip(top).take(available).enumerate() {
                    let line = Line::from(vec![
                        Span::styled(
                            format!(
                                "{} {} ",
                                if top + i == *selected { "›" } else { " " },
                                item.label
                            ),
                            if top + i == *selected { ACCENT } else { FG },
                        ),
                        Span::styled(item.detail.clone(), MUTED),
                    ]);
                    f.render_widget(line, Rect::new(inner.x, start + i as u16, inner.width, 1));
                }
            }
            Modal::Question {
                event,
                index,
                input,
                ..
            } => {
                f.render_widget(
                    Block::default()
                        .borders(Borders::ALL)
                        .title("Codex 需要补充信息 · Enter 回复 · Esc 稍后")
                        .style(Style::default().fg(FG).bg(BG)),
                    rect,
                );
                let q = &event["params"]["questions"][*index];
                let mut lines = history::wrap(
                    Line::raw(string(q, "question").to_owned()),
                    inner.width as usize,
                );
                for option in q["options"].as_array().into_iter().flatten() {
                    lines.extend(history::wrap(
                        Line::styled(
                            format!(
                                "· {} {}",
                                string(option, "label"),
                                string(option, "description")
                            ),
                            MUTED,
                        ),
                        inner.width as usize,
                    ));
                }
                f.render_widget(
                    Paragraph::new(lines),
                    Rect::new(
                        inner.x,
                        inner.y,
                        inner.width,
                        inner.height.saturating_sub(3),
                    ),
                );
                if q["isSecret"].as_bool() == Some(true) {
                    let masked = Input::from_prompt(&json!("•".repeat(input.text.chars().count())));
                    draw_input(
                        f,
                        Rect::new(inner.x, inner.bottom().saturating_sub(3), inner.width, 3),
                        &masked,
                        Line::from("输入答案"),
                    );
                } else {
                    draw_input(
                        f,
                        Rect::new(inner.x, inner.bottom().saturating_sub(3), inner.width, 3),
                        input,
                        Line::from("输入答案或选项名称"),
                    );
                }
            }
        }
    }
    pub(super) fn save_drafts(&mut self, bridge: &Bridge) {
        self.stash();
        let drafts = self
            .drafts
            .iter()
            .map(|(k, d)| {
                (
                    k.clone(),
                    json!({"prompt":d.input.prompt(),"quote":d.quote}),
                )
            })
            .collect::<serde_json::Map<_, _>>();
        let pending = self
            .submit_pending
            .as_ref()
            .map(|p| json!({"id":p.id,"view":p.view,"prompt":p.prompt,"quote":p.draft.quote}));
        bridge.send(json!({"type":"drafts","drafts":drafts,"pending":pending}));
        self.need_save_draft = false;
    }
}
fn draw_input(f: &mut Frame, area: Rect, input: &Input, title: Line<'_>) {
    if area.height < 2 || area.width < 3 {
        return;
    }
    let block = Block::default()
        .borders(Borders::ALL)
        .title(title)
        .border_style(MUTED);
    let inner = block.inner(area);
    let width = inner.width.max(1) as usize;
    let mut lines = input
        .text
        .split('\n')
        .flat_map(|l| history::wrap(Line::raw(l.to_owned()), width))
        .collect::<Vec<_>>();
    let (row, column) = input.location(width);
    while lines.len() <= row {
        lines.push(Line::raw(""));
    }
    let top = row.saturating_sub(inner.height.saturating_sub(1) as usize);
    f.render_widget(block, area);
    f.render_widget(
        Paragraph::new(
            lines
                .into_iter()
                .skip(top)
                .take(inner.height as usize)
                .collect::<Vec<_>>(),
        )
        .style(Style::default().fg(FG).bg(BG)),
        inner,
    );
    if inner.width > 0 && inner.height > 0 {
        f.set_cursor_position(Position::new(
            inner.x + (column as u16).min(inner.width - 1),
            inner.y + (row - top) as u16,
        ));
    }
}
