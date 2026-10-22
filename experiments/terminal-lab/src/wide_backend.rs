//! Ratatui 0.30.2's portable insert_before passes every buffer cell to the
//! backend, including padding cells covered by wide glyphs. Crossterm assumes
//! its input is already filtered (as in normal Buffer diff rendering), which
//! can print a literal extra space after Chinese characters and damage history.
//! Keep that workaround here, independent of selection and Markdown parsing.
use ratatui::{
    backend::{Backend, ClearType, CrosstermBackend, WindowSize},
    buffer::Cell,
    layout::{Position, Size},
};
use std::io::{self, Write};
use unicode_width::UnicodeWidthStr;

pub struct WideBackend<W: Write>(pub CrosstermBackend<W>);

impl<W: Write> Write for WideBackend<W> {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.0.write(buffer)
    }
    fn flush(&mut self) -> io::Result<()> {
        Write::flush(&mut self.0)
    }
}

impl<W: Write> Backend for WideBackend<W> {
    type Error = io::Error;
    fn draw<'a, I>(&mut self, cells: I) -> io::Result<()>
    where
        I: Iterator<Item = (u16, u16, &'a Cell)>,
    {
        let mut covered: Option<(u16, u16)> = None;
        self.0.draw(cells.filter(|(x, y, cell)| {
            if covered.is_some_and(|(row, end)| row == *y && *x < end) {
                return false;
            }
            covered = Some((*y, x.saturating_add(cell.symbol().width().max(1) as u16)));
            true
        }))
    }
    fn append_lines(&mut self, count: u16) -> io::Result<()> {
        self.0.append_lines(count)
    }
    fn hide_cursor(&mut self) -> io::Result<()> {
        self.0.hide_cursor()
    }
    fn show_cursor(&mut self) -> io::Result<()> {
        self.0.show_cursor()
    }
    fn get_cursor_position(&mut self) -> io::Result<Position> {
        self.0.get_cursor_position()
    }
    fn set_cursor_position<P: Into<Position>>(&mut self, position: P) -> io::Result<()> {
        self.0.set_cursor_position(position)
    }
    fn clear(&mut self) -> io::Result<()> {
        self.0.clear()
    }
    fn clear_region(&mut self, kind: ClearType) -> io::Result<()> {
        self.0.clear_region(kind)
    }
    fn size(&self) -> io::Result<Size> {
        self.0.size()
    }
    fn window_size(&mut self) -> io::Result<WindowSize> {
        self.0.window_size()
    }
    fn flush(&mut self) -> io::Result<()> {
        Backend::flush(&mut self.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_buffer_draw_skips_only_the_cells_covered_by_wide_characters() {
        let mut bytes = Vec::<u8>::new();
        let mut backend = WideBackend(CrosstermBackend::new(&mut bytes));
        let cells = [
            Cell::new("甲"),
            Cell::new(" "),
            Cell::new("乙"),
            Cell::new(" "),
            Cell::new("x"),
        ];
        backend
            .draw(cells.iter().enumerate().map(|(x, c)| (x as u16, 0, c)))
            .unwrap();
        let output = String::from_utf8_lossy(&bytes);
        assert!(output.contains('甲') && output.contains('乙') && output.contains('x'));
        assert!(
            !output.contains(' '),
            "wide-cell padding must never become copied text"
        );
    }
}
