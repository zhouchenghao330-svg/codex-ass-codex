use crossterm::{
    cursor::Show,
    event::{DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture},
    execute,
    terminal::{self, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{Terminal, TerminalOptions, Viewport, backend::CrosstermBackend};
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
