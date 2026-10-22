//! A controlled rendering experiment, not a second implementation of Codex Room.
mod history;
mod input;
mod terminal;
mod wide_backend;

use crossterm::event::{
    self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent,
    MouseEventKind,
};
use history::{ACCENT, FG, Fixture, History, MUTED, Point, Selection, byte_at_cell, word_span};
use input::Input;
use ratatui::{
    Frame,
    layout::{Position, Rect},
    style::Style,
    text::Line,
    widgets::{Block, Borders, Clear, Paragraph},
};
use std::{
    env,
    error::Error,
    fs,
    io::{self, IsTerminal, Write},
    path::PathBuf,
    process::{Command, Stdio},
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

struct Options {
    full: bool,
    fixture: PathBuf,
    paused: bool,
    metrics: Option<PathBuf>,
    panic: bool,
}

impl Options {
    fn parse() -> Result<Self> {
        let mut result = Self {
            full: false,
            fixture: PathBuf::new(),
            paused: false,
            metrics: None,
            panic: false,
        };
        let mut arguments = env::args().skip(1);
        while let Some(argument) = arguments.next() {
            match argument.as_str() {
                "--mode" => match arguments.next().as_deref() {
                    Some("inline") => result.full = false,
                    Some("fullscreen") => result.full = true,
                    _ => return Err("--mode must be inline or fullscreen".into()),
                },
                "--fixture" => {
                    result.fixture = arguments.next().ok_or("--fixture requires a path")?.into()
                }
                "--paused" => result.paused = true,
                "--metrics" => {
                    result.metrics =
                        Some(arguments.next().ok_or("--metrics requires a path")?.into())
                }
                "--panic-check" => result.panic = true,
                "--help" | "-h" => {
                    println!(
                        "terminal-lab --mode inline|fullscreen --fixture FILE [--paused] [--metrics FILE]\n\
                        Simulated data only. F2 pause/resume, Ctrl+Q quit. Fullscreen: drag, Ctrl+C copy,\n\
                        double-click word, triple-click line, click header to fold, Ctrl+F forward preview.\n\
                        Inline: host-terminal scroll/select/copy; only the input and live tail are redrawn."
                    );
                    std::process::exit(0);
                }
                _ => return Err(format!("unknown argument: {argument}").into()),
            }
        }
        if result.fixture.as_os_str().is_empty() {
            return Err("--fixture is required".into());
        }
        Ok(result)
    }
}

struct Lab {
    full: bool,
    history: History,
    input: Input,
    scroll: usize,
    follow: bool,
    selection: Option<Selection>,
    press: Option<(u16, u16)>,
    press_point: Option<Point>,
    dragging: bool,
    pointer: Option<(u16, u16)>,
    last_click: Option<(Instant, u16, u16, usize)>,
    popup: Option<String>,
    paused: bool,
    block_index: usize,
    position: usize,
    stream: Vec<String>,
    stream_message: Option<usize>,
    next_tick: Instant,
    body: Rect,
    status: String,
    source_lines: usize,
}

impl Lab {
    fn new(options: &Options, data: Fixture, width: u16) -> Self {
        let mut history = History::new(usize::from(width));
        for source in data.history {
            history.push(source, "模拟 Codex");
        }
        Self {
            full: options.full,
            history,
            input: Input::default(),
            scroll: 0,
            follow: true,
            selection: None,
            press: None,
            press_point: None,
            dragging: false,
            pointer: None,
            last_click: None,
            popup: None,
            paused: options.paused,
            block_index: 0,
            position: 0,
            stream: data.stream_blocks,
            stream_message: None,
            next_tick: Instant::now() + Duration::from_millis(600),
            body: Rect::default(),
            status: "模拟数据 · 不调用模型 · F2 暂停／继续 · Ctrl+Q 退出".into(),
            source_lines: data.source_lines,
        }
    }

    fn maximum(&self) -> usize {
        self.history
            .rows
            .len()
            .saturating_sub(self.body.height as usize)
    }

    fn point(&self, x: u16, y: u16) -> Option<Point> {
        if self.history.rows.is_empty() {
            return None;
        }
        let y = y.clamp(self.body.y, self.body.bottom().saturating_sub(1));
        let row = (self.scroll + (y - self.body.y) as usize).min(self.history.rows.len() - 1);
        let column = x.saturating_sub(self.body.x) as usize;
        Some(Point {
            row,
            byte: byte_at_cell(&self.history.rows[row].text, column),
        })
    }

    fn scroll_by(&mut self, amount: isize) {
        self.scroll = self
            .scroll
            .saturating_add_signed(amount)
            .min(self.maximum());
        self.follow = false;
    }

    fn auto_scroll(&mut self) -> bool {
        let (Some(_), Some((x, y))) = (self.press, self.pointer) else {
            return false;
        };
        let amount = if y <= self.body.y {
            -1
        } else if y >= self.body.bottom().saturating_sub(1) {
            1
        } else {
            0
        };
        if amount == 0 {
            return false;
        }
        self.scroll_by(amount);
        if let (Some(head), Some(selection)) = (self.point(x, y), self.selection.as_mut()) {
            selection.head = head;
        }
        true
    }

    fn forward(&mut self) {
        let text = self
            .selection
            .map(|s| s.text(&self.history.rows))
            .unwrap_or_default();
        self.popup = Some(if text.is_empty() {
            "请先拖选一段文字。\n\n这是模拟转发预览，不连接接收者或模型。".into()
        } else {
            format!(
                "已捕获 {} 个字符。仅预览，不发送。\n\n{}",
                text.chars().count(),
                text
            )
        });
    }

    fn mouse(&mut self, mouse: MouseEvent) -> Result<()> {
        if !self.full {
            return Ok(());
        }
        let (x, y) = (mouse.column, mouse.row);
        if self.popup.is_some() {
            if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left)) {
                self.popup = None;
            }
            return Ok(());
        }
        match mouse.kind {
            MouseEventKind::ScrollUp => self.scroll_by(-1),
            MouseEventKind::ScrollDown => self.scroll_by(1),
            MouseEventKind::Down(MouseButton::Left) => {
                if y == self.body.bottom() + 3 {
                    if x < 10 {
                        self.forward();
                    } else if (11..17).contains(&x) {
                        self.follow = true;
                        self.scroll = self.maximum();
                    }
                    return Ok(());
                }
                if !self.body.contains(Position::new(x, y)) {
                    self.selection = None;
                    return Ok(());
                }
                self.follow = false;
                let Some(point) = self.point(x, y) else {
                    return Ok(());
                };
                let count = self
                    .last_click
                    .filter(|(when, cx, cy, _)| {
                        when.elapsed() < Duration::from_millis(400) && *cx == x && *cy == y
                    })
                    .map(|(_, _, _, count)| count % 3 + 1)
                    .unwrap_or(1);
                self.last_click = Some((Instant::now(), x, y, count));
                if count > 1 {
                    let text = &self.history.rows[point.row].text;
                    let (left, right) = if count == 2 {
                        word_span(text, point.byte)
                    } else {
                        (0, text.len())
                    };
                    self.selection = Some(Selection {
                        anchor: Point {
                            row: point.row,
                            byte: left,
                        },
                        head: Point {
                            row: point.row,
                            byte: right,
                        },
                    });
                    self.press = None;
                    self.press_point = None;
                } else {
                    // A single press outside the highlight cancels it immediately.
                    let inside = self
                        .selection
                        .and_then(|s| s.span(point.row, self.history.rows[point.row].text.len()))
                        .is_some_and(|(a, b)| a <= point.byte && point.byte < b);
                    if !inside {
                        self.selection = Some(Selection {
                            anchor: point,
                            head: point,
                        });
                    }
                    self.press = Some((x, y));
                    self.press_point = Some(point);
                    self.dragging = false;
                    self.pointer = Some((x, y));
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
                    }
                    self.pointer = Some((x, y));
                    if let (Some(head), Some(selection)) =
                        (self.point(x, y), self.selection.as_mut())
                    {
                        selection.head = head;
                    }
                }
            }
            MouseEventKind::Up(MouseButton::Left) => {
                if self.press == Some((x, y)) {
                    if let Some(point) = self.point(x, y) {
                        if self.history.rows[point.row].header {
                            let index = self.history.rows[point.row].message;
                            self.history.toggle(index);
                            self.selection = None;
                        }
                    }
                }
                self.press = None;
                self.press_point = None;
                self.pointer = None;
            }
            _ => {}
        }
        Ok(())
    }

    fn key(&mut self, key: KeyEvent) -> Result<bool> {
        if key.kind == KeyEventKind::Release {
            return Ok(true);
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        if ctrl && key.code == KeyCode::Char('q') {
            return Ok(false);
        }
        if self.popup.is_some() {
            self.popup = None;
            return Ok(true);
        }
        match key.code {
            KeyCode::F(2) => self.paused = !self.paused,
            KeyCode::Esc => self.selection = None,
            KeyCode::Char('c') if ctrl => {
                let text = self
                    .selection
                    .map(|s| s.text(&self.history.rows))
                    .unwrap_or_default();
                if !text.is_empty() {
                    match copy(&text) {
                        Ok(()) => self.status = format!("已复制 {} 个字符", text.chars().count()),
                        Err(error) => self.status = format!("复制失败：{error}"),
                    }
                } else if !self.input.text.is_empty() {
                    self.input.take();
                } else {
                    return Ok(false);
                }
            }
            KeyCode::Char('f') if ctrl && self.full => self.forward(),
            KeyCode::Char('u') if ctrl => {
                self.input.take();
            }
            KeyCode::Char('d') if ctrl && self.input.text.is_empty() => return Ok(false),
            KeyCode::Char('j') if ctrl => self.input.insert("\n"),
            KeyCode::Enter if alt || key.modifiers.contains(KeyModifiers::SHIFT) => {
                self.input.insert("\n")
            }
            KeyCode::Enter => { /* Submission is handled by the caller after this method. */ }
            KeyCode::Char(c) if !ctrl && !alt => self.input.insert(&c.to_string()),
            KeyCode::Backspace => self.input.backspace(),
            KeyCode::Delete => self.input.delete(),
            KeyCode::Left => self.input.left(),
            KeyCode::Right => self.input.right(),
            KeyCode::Home => self.input.cursor = 0,
            KeyCode::End if ctrl => {
                self.follow = true;
                self.scroll = self.maximum();
            }
            KeyCode::End => self.input.cursor = self.input.text.len(),
            KeyCode::PageUp if self.full => self.scroll_by(-(self.body.height as isize - 1).max(1)),
            KeyCode::PageDown if self.full => {
                self.scroll_by((self.body.height as isize - 1).max(1))
            }
            _ => {}
        }
        Ok(true)
    }

    fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let input_height = 3.min(area.height.saturating_sub(2));
        let footer = Rect::new(area.x, area.bottom().saturating_sub(1), area.width, 1);
        let input_area = Rect::new(
            area.x,
            footer.y.saturating_sub(input_height),
            area.width,
            input_height,
        );
        let content = Rect::new(
            area.x,
            area.y + 1,
            area.width,
            input_area.y.saturating_sub(area.y + 1),
        );
        let mode = if self.full {
            "Rust Fullscreen · 应用滚动与选区"
        } else {
            "Rust Inline · 终端原生滚动与选区"
        };
        frame.render_widget(
            Line::styled(
                format!(
                    "{mode} · {} 行源文本{}",
                    self.source_lines,
                    if self.paused {
                        " · 已暂停"
                    } else {
                        " · 流式输出中"
                    }
                ),
                MUTED,
            ),
            Rect::new(area.x, area.y, area.width, 1),
        );
        if self.full {
            self.body = content;
            if self.follow && self.press.is_none() && self.selection.is_none() {
                self.scroll = self.maximum();
            }
            self.scroll = self.scroll.min(self.maximum());
            let lines = self
                .history
                .rows
                .iter()
                .enumerate()
                .skip(self.scroll)
                .take(content.height as usize)
                .map(|(index, row)| {
                    self.selection
                        .map(|s| s.highlight(index, row))
                        .unwrap_or_else(|| row.line.clone())
                })
                .collect::<Vec<_>>();
            frame.render_widget(
                Paragraph::new(lines).style(Style::default().fg(FG)),
                content,
            );
        } else if let Some(index) = self.stream_message {
            let lines = self
                .history
                .message_rows(index)
                .iter()
                .map(|row| row.line.clone())
                .collect::<Vec<_>>();
            let skip = lines.len().saturating_sub(content.height as usize);
            frame.render_widget(
                Paragraph::new(lines.into_iter().skip(skip).collect::<Vec<_>>())
                    .style(Style::default().fg(FG)),
                content,
            );
        }
        draw_input(frame, input_area, &self.input);
        let footer_text = if self.full {
            format!("[转发选中] [底部]  ·  {}", self.status)
        } else {
            self.status.clone()
        };
        frame.render_widget(Line::styled(footer_text, MUTED), footer);
        if let Some(text) = &self.popup {
            let width = area.width.saturating_sub(8).min(100);
            let height = area.height.saturating_sub(6).min(20);
            let rect = Rect::new(
                area.x + (area.width - width) / 2,
                area.y + (area.height - height) / 2,
                width,
                height,
            );
            frame.render_widget(Clear, rect);
            let lines = text
                .lines()
                .flat_map(|line| {
                    history::wrap(Line::raw(line.to_owned()), width.saturating_sub(2) as usize)
                })
                .collect::<Vec<_>>();
            frame.render_widget(
                Paragraph::new(lines).style(Style::default().fg(FG)).block(
                    Block::default()
                        .borders(Borders::ALL)
                        .title("模拟转发预览 · 任意键／点击关闭"),
                ),
                rect,
            );
        }
    }
}

fn draw_input(frame: &mut Frame, area: Rect, input: &Input) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title("消息 · Enter 发送 · Alt+Enter 换行")
        .border_style(ACCENT);
    let inner = block.inner(area);
    let width = inner.width.max(1) as usize;
    let mut lines = input
        .text
        .split('\n')
        .flat_map(|line| history::wrap(Line::raw(line.to_owned()), width))
        .collect::<Vec<_>>();
    let (row, column) = input.location(width);
    while lines.len() <= row {
        lines.push(Line::raw(""));
    }
    let top = row.saturating_sub(inner.height.saturating_sub(1) as usize);
    frame.render_widget(block, area);
    frame.render_widget(
        Paragraph::new(lines.into_iter().skip(top).collect::<Vec<_>>())
            .style(Style::default().fg(FG)),
        inner,
    );
    if inner.width > 0 && inner.height > 0 {
        frame.set_cursor_position(Position::new(
            inner.x + column as u16,
            inner.y + (row - top) as u16,
        ));
    }
}

fn copy(text: &str) -> io::Result<()> {
    // No clipboard polling. Only the user's explicit copy action runs a helper.
    let (program, arguments): (&str, &[&str]) = if cfg!(target_os = "macos") {
        ("pbcopy", &[])
    } else if cfg!(windows) {
        ("clip.exe", &[])
    } else if env::var_os("WAYLAND_DISPLAY").is_some() {
        ("wl-copy", &[])
    } else {
        ("xclip", &["-selection", "clipboard"])
    };
    let mut child = Command::new(program)
        .args(arguments)
        .stdin(Stdio::piped())
        .spawn()?;
    child
        .stdin
        .take()
        .ok_or_else(|| io::Error::other("clipboard stdin unavailable"))?
        .write_all(text.as_bytes())?;
    if !child.wait()?.success() {
        return Err(io::Error::other("clipboard helper failed"));
    }
    Ok(())
}

fn run(options: &Options, data: Fixture) -> Result<()> {
    let load = Instant::now();
    let width = crossterm::terminal::size()?.0;
    let mut lab = Lab::new(options, data, width);
    let (mut terminal, _guard) = terminal::setup(options.full)?;
    if !options.full {
        let lines = lab
            .history
            .rows
            .iter()
            .map(|r| r.line.clone())
            .collect::<Vec<_>>();
        terminal::print_history(&mut terminal, &lines)?;
    }
    lab.status = format!(
        "初始化 {:.2}s · F2 暂停／继续 · Ctrl+Q 退出",
        load.elapsed().as_secs_f64()
    );
    let mut dirty = true;
    let mut last_draw = Instant::now() - Duration::from_secs(1);
    let mut last_auto = Instant::now();
    let mut frames = 0u64;
    let mut max_draw = 0u128;
    let mut total_draw = 0u128;
    loop {
        if options.panic && frames > 0 {
            panic!("terminal-lab cleanup probe");
        }
        if !lab.paused && Instant::now() >= lab.next_tick && !lab.stream.is_empty() {
            lab.next_tick = Instant::now() + Duration::from_millis(60);
            let source = &lab.stream[lab.block_index % lab.stream.len()];
            lab.position = (lab.position + 4).min(source.chars().count());
            let partial: String = source.chars().take(lab.position).collect();
            if lab.stream_message.is_none() {
                lab.stream_message = Some(lab.history.push(partial, "模拟 Codex · 流式输出"));
            } else {
                lab.history.update_tail(partial);
            }
            if lab.position == source.chars().count() {
                if !options.full {
                    let index = lab.stream_message.unwrap();
                    let rows = lab
                        .history
                        .message_rows(index)
                        .iter()
                        .map(|r| r.line.clone())
                        .collect::<Vec<_>>();
                    terminal::print_history(&mut terminal, &rows)?;
                }
                lab.stream_message = None;
                lab.position = 0;
                lab.block_index += 1;
            }
            dirty = true;
        }
        if last_auto.elapsed() >= Duration::from_millis(30) {
            dirty |= lab.auto_scroll();
            last_auto = Instant::now();
        }
        if event::poll(Duration::from_millis(4))? {
            let event = event::read()?;
            match event {
                Event::Key(key) => {
                    let submit = key.code == KeyCode::Enter
                        && key.modifiers.is_empty()
                        && key.kind != KeyEventKind::Release
                        && lab.popup.is_none();
                    if !lab.key(key)? {
                        break;
                    }
                    if submit {
                        let text = lab.input.take();
                        if text.trim() == "/quit" {
                            break;
                        }
                        if text.trim() == "/pause" {
                            lab.paused = !lab.paused;
                        } else if !text.is_empty() {
                            // End the current simulated block before appending a query;
                            // subsequent updates always belong to the latest message.
                            if let Some(index) = lab.stream_message.take() {
                                if !options.full {
                                    let lines = lab
                                        .history
                                        .message_rows(index)
                                        .iter()
                                        .map(|r| r.line.clone())
                                        .collect::<Vec<_>>();
                                    terminal::print_history(&mut terminal, &lines)?;
                                }
                                lab.position = 0;
                                lab.block_index += 1;
                            }
                            let index = lab.history.push(text, "你");
                            if !options.full {
                                let lines = lab
                                    .history
                                    .message_rows(index)
                                    .iter()
                                    .map(|r| r.line.clone())
                                    .collect::<Vec<_>>();
                                terminal::print_history(&mut terminal, &lines)?;
                            }
                        }
                    }
                }
                Event::Paste(text) => lab.input.insert(&text),
                Event::Mouse(mouse) => {
                    if mouse.kind == MouseEventKind::Moved {
                        continue;
                    }
                    lab.mouse(mouse)?;
                }
                Event::Resize(width, _) => {
                    terminal.autoresize()?;
                    lab.history.reflow(width as usize);
                    lab.selection = None;
                    lab.press = None;
                    lab.press_point = None;
                    lab.status = if options.full {
                        "已重排宽度；旧选区已清除（本次实验限制）"
                    } else {
                        "已重排当前输入区；历史由终端管理"
                    }
                    .into();
                }
                _ => {}
            }
            dirty = true;
        }
        if dirty && last_draw.elapsed() >= Duration::from_millis(8) {
            let before = Instant::now();
            terminal.draw(|frame| lab.draw(frame))?;
            let elapsed = before.elapsed().as_micros();
            total_draw += elapsed;
            max_draw = max_draw.max(elapsed);
            frames += 1;
            last_draw = Instant::now();
            dirty = false;
        }
    }
    if !options.full {
        terminal::finish_inline(&mut terminal)?;
    }
    if let Some(path) = &options.metrics {
        fs::write(
            path,
            serde_json::to_vec_pretty(&serde_json::json!({
                "mode": if options.full { "fullscreen" } else { "inline" },
                "source_lines": lab.source_lines, "rendered_rows": lab.history.rows.len(), "frames": frames,
                "mean_draw_us": total_draw / u128::from(frames.max(1)), "max_draw_us": max_draw,
                "note": "Application draw time only; not terminal presentation or end-to-end selection latency."
            }))?,
        )?;
    }
    Ok(())
}

fn main() {
    let result = (|| -> Result<()> {
        let options = Options::parse()?;
        if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
            return Err("Run this experiment in an interactive terminal.".into());
        }
        let data: Fixture = serde_json::from_slice(&fs::read(&options.fixture)?)?;
        run(&options, data)
    })();
    if let Err(error) = result {
        terminal::restore();
        eprintln!("terminal-lab: {error}");
        std::process::exit(1);
    }
}
