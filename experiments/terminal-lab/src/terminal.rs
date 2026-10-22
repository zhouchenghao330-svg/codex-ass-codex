use crossterm::{
    cursor::{MoveTo, Show},
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{self, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    Terminal, TerminalOptions, Viewport,
    backend::CrosstermBackend,
    text::Line,
    widgets::{Paragraph, Widget},
};
use std::{
    io::{self, Write},
    sync::atomic::{AtomicBool, Ordering},
};

use crate::wide_backend::WideBackend;

pub type Tui = Terminal<WideBackend<io::Stdout>>;
static ACTIVE: AtomicBool = AtomicBool::new(false);
static FULL: AtomicBool = AtomicBool::new(false);

pub struct Guard;
impl Drop for Guard {
    fn drop(&mut self) {
        restore();
    }
}

pub fn restore() {
    if !ACTIVE.swap(false, Ordering::SeqCst) {
        return;
    }
    let mut out = io::stdout();
    let _ = execute!(out, DisableMouseCapture, DisableBracketedPaste, Show);
    if FULL.load(Ordering::SeqCst) {
        let _ = execute!(out, LeaveAlternateScreen);
    }
    let _ = terminal::disable_raw_mode();
    let _ = out.flush();
}

pub fn setup(fullscreen: bool) -> io::Result<(Tui, Guard)> {
    terminal::enable_raw_mode()?;
    FULL.store(fullscreen, Ordering::SeqCst);
    ACTIVE.store(true, Ordering::SeqCst);
    let guard = Guard;
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        previous(info);
    }));
    let mut out = io::stdout();
    execute!(out, EnableBracketedPaste)?;
    let viewport = if fullscreen {
        execute!(out, EnterAlternateScreen, EnableMouseCapture)?;
        Viewport::Fullscreen
    } else {
        // Native selection is a host-terminal operation in this mode.
        execute!(out, DisableMouseCapture)?;
        Viewport::Inline(9)
    };
    let terminal = Terminal::with_options(
        WideBackend(CrosstermBackend::new(out)),
        TerminalOptions { viewport },
    )?;
    Ok((terminal, guard))
}

pub fn print_history(terminal: &mut Tui, lines: &[Line<'static>]) -> io::Result<()> {
    // Bound temporary buffers for the 10,000-line workload. Use the portable
    // insert_before implementation: partial-region CSI S can lose scrollback
    // in some xterm.js hosts. No per-frame replay of committed history.
    for chunk in lines.chunks(512) {
        terminal.insert_before(chunk.len() as u16, |buffer| {
            Paragraph::new(chunk.to_vec()).render(buffer.area, buffer);
        })?;
    }
    Ok(())
}

pub fn finish_inline(terminal: &mut Tui) -> io::Result<()> {
    let area = terminal.get_frame().area();
    terminal.clear()?;
    execute!(terminal.backend_mut(), MoveTo(0, area.y), Show)?;
    writeln!(terminal.backend_mut(), "\r")?;
    terminal.backend_mut().flush()
}
