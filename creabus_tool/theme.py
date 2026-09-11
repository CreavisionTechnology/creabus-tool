# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Colours that read correctly on a light desktop and on a dark one.

ttk widgets follow the platform appearance on their own, so a Treeview turns
dark when macOS does. Two things do not follow: a colour we set ourselves, and
a classic Tk widget like Text or Listbox, which stays light grey whatever the
desktop is doing. Left alone that gives white text on a pale green row, and a
blazing white Debug log in the middle of an otherwise dark window.

So the window asks which kind of desktop it is on, once, and takes its
highlight and log colours from here.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:                       # pragma: no cover - depends on the install
    tk = ttk = None                       # type: ignore[assignment]

# Anything darker than this counts as a dark desktop. Mid grey is the only
# sensible split; real themes sit far from it in both directions.
DARK_THRESHOLD = 0.5


@dataclass(frozen=True)
class Palette:
    """Every colour the window picks for itself, for one kind of desktop."""

    dark: bool
    # Row highlights in the register and traffic tables: (background, ink).
    # The ink matters as much as the background - setting only the background
    # leaves the theme's own foreground, which is what made polled rows white
    # on pale green.
    hot: tuple[str, str]
    pinned: tuple[str, str]
    failed: tuple[str, str]
    # The Debug log surface, which is a classic Tk widget and so has to be
    # told the appearance the rest of the window already has.
    surface: str
    ink: str
    cursor: str
    select: str
    muted: str
    log: dict[str, str]


LIGHT = Palette(
    dark=False,
    hot=("#e3f5e3", "#17331d"),
    pinned=("#fff4d6", "#4a3300"),
    failed=("#ffe3e3", "#5c1111"),
    surface="#fbfbfb",
    ink="#202020",
    cursor="#202020",
    select="#cfe3f7",
    muted="#5a5a5a",
    log={
        "info": "#1a5fb4",
        "conn": "#1c7430",
        "disconn": "#7a7a7a",
        "request": "#202020",
        "frame": "#8f4fb0",
        "write": "#a15c00",
        "warn": "#b35c00",
        "error": "#c01c28",
    },
)

DARK = Palette(
    dark=True,
    hot=("#1e3a24", "#b9e7c4"),
    pinned=("#4a3a10", "#f4dfa6"),
    failed=("#4a1f1f", "#ffbcbc"),
    surface="#1c1c1c",
    ink="#d8d8d8",
    cursor="#d8d8d8",
    select="#2f4f6f",
    muted="#9a9a9a",
    log={
        "info": "#6ab0ff",
        "conn": "#57d17a",
        "disconn": "#9a9a9a",
        "request": "#d8d8d8",
        "frame": "#c98ae0",
        "write": "#e0a355",
        "warn": "#f0a24a",
        "error": "#ff6b6b",
    },
)


def luminance(rgb: tuple[int, int, int]) -> float:
    """Perceived brightness, 0.0 black to 1.0 white.

    Takes Tk's 16 bit per channel triple, which is what winfo_rgb returns.
    Weighted the usual way: the eye is far more sensitive to green than to
    blue, so a plain average would call some dark blues light.
    """
    red, green, blue = (max(0, min(65535, channel)) / 65535 for channel in rgb)
    return 0.299 * red + 0.587 * green + 0.114 * blue


def is_dark(widget) -> bool:
    """Whether the desktop this window is on is a dark one.

    Asked of the theme rather than of the operating system: what matters is
    the colour actually behind the widgets, and winfo_rgb resolves the
    symbolic names macOS uses ("systemWindowBackgroundColor") to the real one
    for the current appearance.
    """
    if tk is None:
        return False
    candidates = []
    try:
        candidates.append(ttk.Style(widget).lookup("TFrame", "background"))
    except tk.TclError:
        pass
    try:
        candidates.append(widget.winfo_toplevel().cget("background"))
    except tk.TclError:
        pass
    for colour in candidates:
        if not colour:
            continue
        try:
            return luminance(widget.winfo_rgb(colour)) < DARK_THRESHOLD
        except tk.TclError:
            continue
    return False


def palette_for(widget) -> Palette:
    """The palette matching the desktop this widget is on."""
    return DARK if is_dark(widget) else LIGHT
