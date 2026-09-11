# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Product identity, and the small amount of UI that shows it.

Kept in one module so the name, the version and the vendor are stated once and
the rest of the code (and the build script, and the tests) can ask for them.

The branding in the window is deliberately quiet: an icon, one muted line in
the status bar, and an About dialog behind it. Nothing blocks, nothing nags and
nothing has to be dismissed before the tool can be used.
"""

from __future__ import annotations

import webbrowser

from . import compat

# The identity below is needed by the headless modes and by --version, which
# have to work on a machine with no Tk at all - a server, a container, a
# minimal Linux install. Only the widget helpers further down need tkinter, so
# a missing Tk costs you the About box, not the ability to run the tool.
try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:                       # pragma: no cover - depends on the install
    tk = ttk = None                       # type: ignore[assignment]

VERSION = "0.0.3"
APP_NAME = "CreaBus Tool"
TAGLINE = "Modbus simulator and client, over TCP and RTU"
VENDOR = "Creavision Technology"
VENDOR_URL = "https://creavisiontechnology.com"
PROJECT_URL = "https://github.com/CreavisionTechnology/creabus-tool"
LICENCE = "GPL-3.0-or-later"
LICENCE_NAME = "GNU General Public License v3 or later"
COPYRIGHT = f"Copyright (c) 2026 {VENDOR}"

# Creavision's palette, used for the accents only so the window keeps the
# native look of whatever platform it is running on.
INK = "#16302A"
ACCENT = "#5B9E73"
MUTED = "#6B6B6B"

# Icon sizes offered to the window manager, largest last.
_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def _photo(filename: str):
    """Load a PNG from ``assets/``, or None if it is missing or unreadable.

    Tk 8.6 reads PNG natively. On anything older, or in a stripped down build,
    the branding simply does not appear rather than stopping the app.
    """
    if tk is None:
        return None
    try:
        return tk.PhotoImage(file=compat.asset_path(filename))
    except Exception:
        return None


def apply_window_icon(window) -> None:
    """Give the window (and its taskbar entry) the CreaBus Tool icon."""
    images = [image for image in (_photo(f"icon-{size}.png") for size in _ICON_SIZES)
              if image is not None]
    if not images:
        return
    try:
        window.iconphoto(True, *images)
    except Exception:
        return
    # Tk keeps no reference of its own, so the images must outlive this call.
    window._brand_icons = images  # type: ignore[attr-defined]


def window_title(detail: str = "") -> str:
    """The window title: the product name, then whatever is going on."""
    return f"{APP_NAME} - {detail}" if detail else APP_NAME


def attach_attribution(parent, font: tuple):
    """The muted 'Creavision Technology' line, clickable, for the status bar."""
    frame = ttk.Frame(parent)
    logo = _photo("creavision-20.png")
    if logo is not None:
        label = ttk.Label(frame, image=logo)
        label.image = logo  # type: ignore[attr-defined]
        label.pack(side="left", padx=(0, 4))
    ttk.Label(frame, text=VENDOR, style="Brand.TLabel", font=font).pack(side="left")

    def open_about(_event=None) -> None:
        show_about(parent.winfo_toplevel())

    for widget in (frame, *frame.winfo_children()):
        widget.bind("<Button-1>", open_about)
        try:
            widget.configure(cursor="hand2")
        except tk.TclError:
            pass
    return frame


def show_about(parent) -> None:
    """A small modal About box. Opened from the status bar, never on its own."""
    window = tk.Toplevel(parent)
    window.title(f"About {APP_NAME}")
    window.transient(parent)
    window.resizable(False, False)
    apply_window_icon(window)

    body = ttk.Frame(window, padding=18)
    body.pack(fill="both", expand=True)

    icon = _photo("icon-64.png")
    if icon is not None:
        holder = ttk.Label(body, image=icon)
        holder.image = icon  # type: ignore[attr-defined]
        holder.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 16))

    ttk.Label(body, text=f"{APP_NAME} {VERSION}",
              font=("TkDefaultFont", 13, "bold")).grid(row=0, column=1, sticky="w")
    ttk.Label(body, text=TAGLINE, foreground=MUTED).grid(row=1, column=1, sticky="w",
                                                         pady=(2, 10))

    details = ttk.Frame(body)
    details.grid(row=2, column=1, sticky="w")

    vendor_row = ttk.Frame(details)
    vendor_row.pack(anchor="w")
    mark = _photo("creavision-24.png")
    if mark is not None:
        holder = ttk.Label(vendor_row, image=mark)
        holder.image = mark  # type: ignore[attr-defined]
        holder.pack(side="left", padx=(0, 6))
    ttk.Label(vendor_row, text=f"by {VENDOR}").pack(side="left")

    ttk.Label(details, text=COPYRIGHT, foreground=MUTED).pack(anchor="w", pady=(8, 0))
    # The GPL asks an interactive program to show these: the licence, that
    # there is no warranty, and that the user is free to pass it on.
    ttk.Label(details, text=f"{LICENCE_NAME}.", foreground=MUTED).pack(anchor="w")
    ttk.Label(details,
              text="This program comes with ABSOLUTELY NO WARRANTY.\n"
                   "It is free software, and you are welcome to redistribute it\n"
                   "under the terms of the GPL. See the LICENSE file for details.",
              foreground=MUTED, justify="left").pack(anchor="w")

    links = ttk.Frame(details)
    links.pack(anchor="w", pady=(10, 0))
    for text, url in ((VENDOR_URL, VENDOR_URL), ("Source code", PROJECT_URL)):
        link = ttk.Label(links, text=text, foreground="#1a5fb4", cursor="hand2")
        link.pack(side="left", padx=(0, 14))
        link.bind("<Button-1>", lambda _e, target=url: _open_url(target))

    ttk.Button(body, text="Close", command=window.destroy).grid(
        row=3, column=1, sticky="e", pady=(18, 0))

    window.bind("<Escape>", lambda _e: window.destroy())
    window.update_idletasks()
    _centre_on(window, parent)
    window.grab_set()
    window.focus_set()


def _open_url(url: str) -> None:
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass


def _centre_on(window, parent) -> None:
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_height()) // 3
        window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except tk.TclError:
        pass
