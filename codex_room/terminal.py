"""Keep the shell's keyboard mode separate from the full-screen UI."""
from __future__ import annotations

import re

from textual.drivers.linux_driver import LinuxDriver


class RoomDriver(LinuxDriver):
    """Use basic Kitty keys and protect the caller's mode with an outer push/pop.

    Some embedded terminals don't isolate keyboard flags between screen buffers.
    Keep an extra saved mode on the caller's screen, and leave Enter, Tab and
    Backspace in their legacy form instead of requesting Textual's full flags (25).
    https://sw.kovidgoyal.net/kitty/keyboard-protocol/
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._room_alt_screen = False
        self._room_keyboard_pushed = False

    def write(self, data: str) -> None:
        if data == "\x1b[?1049h":
            # Save the calling screen's flags before entering the alternate screen.
            data = "\x1b[>0u" + data
            self._room_alt_screen = True
        elif re.fullmatch(r"\x1b\[>\d+u", data):
            data = "\x1b[>1u"
            self._room_keyboard_pushed = True
        elif data == "\x1b[<u":
            if not self._room_keyboard_pushed:
                # Textual also emits a pop when TEXTUAL_DISABLE_KITTY_KEY is set.
                return
            data = "\x1b[=0u" + data
            self._room_keyboard_pushed = False
        elif data == "\x1b[?1049l" and self._room_alt_screen:
            # Restore after switching back, including terminals with shared flags.
            data += "\x1b[<u"
            self._room_alt_screen = False
        super().write(data)

    def close(self) -> None:
        try:
            if self._room_alt_screen:
                self.stop_application_mode()
        finally:
            super().close()
