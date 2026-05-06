from __future__ import annotations

import sys


def set_terminal_title(text: str) -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write(f"\x1b]2;{text}\x07")
    out.flush()


def push_terminal_title() -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[22;2t")
    out.flush()


def pop_terminal_title() -> None:
    out = sys.__stdout__
    if out is None or not out.isatty():
        return
    out.write("\x1b[23;2t")
    out.flush()


__all__ = ["pop_terminal_title", "push_terminal_title", "set_terminal_title"]
