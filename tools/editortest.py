# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Smoke test for the profile editor, the register menu and the help window.

Drives the real widgets rather than a stand-in: the editor is opened on a real
profile, edited, saved to a temporary file and loaded back again, and a single
register is fetched from a live server over TCP.

    python tools/editortest.py
    xvfb-run -a python tools/editortest.py        (headless Linux)
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool.datastore import DataStore
from creabus_tool.editor import ProfileEditor
from creabus_tool.help import SECTIONS, reflow, show_help
from creabus_tool.profile import load_profile
from creabus_tool.server import SimulationRunner, TcpServer
from creabus_tool.ui import ModbusToolApp

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.path.join(HERE, "devices")
UI_PORT = 15090          # the window's own server, unused but bound
PEER_PORT = 15091        # the peer the window polls in client mode

passed = failed = 0
state: dict = {}


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def guard(action):
    """Run a step so that raising counts as a failure.

    Tk swallows exceptions raised inside an after() callback: it prints a
    traceback and carries on. Without this a step that blew up would leave the
    run reporting no failures at all.
    """
    def run() -> None:
        global failed
        try:
            action()
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {action.__name__} raised {type(exc).__name__}: {exc}")
    run.__name__ = action.__name__
    return run


def main() -> int:
    profile = load_profile(os.path.join(DEVICES, "sdm120.yaml"))
    peer_store = DataStore(profile)
    peer = TcpServer(peer_store, "127.0.0.1", PEER_PORT, log_requests=False)
    simulation = SimulationRunner(peer_store)
    simulation.start()
    peer.start()
    time.sleep(0.4)

    root = tk.Tk()
    root.geometry("1400x820")
    app = ModbusToolApp(root, DEVICES, os.path.join(DEVICES, "sdm120.yaml"),
                        host="127.0.0.1", port=UI_PORT, role="server", link="tcp")

    # -- addresses ---------------------------------------------------------
    def address_default() -> None:
        shown = str(app.tree.item("voltage")["values"][1])
        check("the address column starts in protocol style",
              shown.startswith("0 /"), shown)

    def address_conventional() -> None:
        app.conventional_var.set(True)
        app.on_address_style_change()
        shown = str(app.tree.item("voltage")["values"][1])
        check("the 4xxxx toggle shows 30001 for input register 0",
              shown == "30001", shown)
        check("the column heading follows the toggle",
              app.tree.heading("address")["text"] == "4xxxx / 3xxxx",
              app.tree.heading("address")["text"])

    def address_back() -> None:
        app.conventional_var.set(False)
        app.on_address_style_change()
        shown = str(app.tree.item("voltage")["values"][1])
        check("toggling back restores the protocol address",
              shown.startswith("0 /"), shown)

    # -- the register menu -------------------------------------------------
    def menu_built() -> None:
        app.tree.selection_set("voltage")
        register = app.selected_register()
        check("selected_register finds the highlighted row",
              register is not None and register.name == "voltage")
        check("the context menu has entries", app.register_menu.index("end") is not None)

    def copy_conventional() -> None:
        app.tree.selection_set("voltage")
        app._copy_register("conventional")
        check("copying the 4xxxx address yields 30001",
              app.clipboard_get() == "30001", app.clipboard_get())

    def copy_row() -> None:
        app._copy_register("row")
        text = app.clipboard_get()
        check("copying a row gives tab separated text with both addresses",
              "\t" in text and "Voltage" in text and "30001" in text, text[:70])

    # -- reading one register ----------------------------------------------
    def read_without_polling() -> None:
        """The whole point: one register, no Start, no lingering connection."""
        check("nothing is polling", app.poller is None or not app.poller.running)
        check("and there is no open link to borrow", app.master_link is None)
        app.store.blank()                   # so any value must come off the wire
        app.tree.selection_set("voltage")
        app.read_selected()

    def read_without_polling_landed() -> None:
        register = next(r for r in app.store.snapshot() if r.name == "voltage")
        check("Read now connected on its own and fetched a value",
              register.value is not None and not register.error,
              f"value={register.value} error={register.error}")
        check("and it did not leave a connection behind", app.master_link is None)

    def to_client() -> None:
        app.stop()
        app.role_var.set("client")
        app.link_var.set("tcp")
        app._on_mode_change()
        app.host_var.set("127.0.0.1")
        app.port_var.set(str(PEER_PORT))
        app.interval_var.set("5")           # slow, so "Read now" is what lands

    def start_client() -> None:
        app.start()

    def read_one() -> None:
        app.tree.selection_set("phase_angle")
        app.read_selected()

    def read_one_landed() -> None:
        register = next(r for r in app.store.snapshot() if r.name == "phase_angle")
        check("Read now fetched a value for that one register",
              register.value is not None and not register.error,
              f"value={register.value} error={register.error}")

    # -- help ---------------------------------------------------------------
    def help_opens() -> None:
        show_help(root)
        check("the help window opens", root._help_window.winfo_exists())
        check("help covers every section", len(SECTIONS) >= 9, str(len(SECTIONS)))

    def help_is_single() -> None:
        before = root._help_window
        show_help(root)
        check("asking again raises the same window, it does not open a second",
              root._help_window is before)

    def help_reflows() -> None:
        body = dict(SECTIONS)["Addresses: 0 or 40001?"]
        out = reflow(body)
        check("help joins prose into paragraphs the widget can wrap",
              "Modbus carries a zero based address on the wire. Device manuals" in out)
        check("help leaves the indented address table alone",
              any(line.startswith("    holding registers") for line in out.splitlines()))

    def theme_applied() -> None:
        palette = app.palette
        check("the debug log takes its surface from the palette, not a fixed white",
              str(app.log_text.cget("background")) == palette.surface,
              f"{app.log_text.cget('background')} vs {palette.surface}")
        check("the debug log ink comes from the palette too",
              str(app.log_text.cget("foreground")) == palette.ink)
        check("a polled row sets an ink colour, not only a background",
              str(app.tree.tag_configure("hot", "foreground")) == palette.hot[1],
              str(app.tree.tag_configure("hot", "foreground")))
        check("a failed row does the same",
              str(app.tree.tag_configure("failed", "foreground")) == palette.failed[1])

    def fonts_are_fixed_width() -> None:
        """The log and the help tables are aligned with spaces, so this matters.

        ("TkFixedFont", 9) silently resolves to the proportional default,
        because Tk reads the first element as a family name. Measuring is the
        only way to tell: the font object reports whatever it fell back to
        without ever raising.
        """
        for label, spec in (("log", app.mono_font), ("log bold", app.mono_bold)):
            measured = tkfont.Font(root=root, font=spec)
            narrow = measured.measure("iiiiiiiiii")
            wide = measured.measure("MMMMMMMMMM")
            check(f"the {label} font is genuinely fixed width", narrow == wide,
                  f"{spec} resolved to {measured.actual('family')}")

    def help_lists_every_profile() -> None:
        """Adding a profile must not leave the help describing the old set."""
        body = dict(SECTIONS)["Device profiles"]
        shipped = sorted(
            os.path.splitext(name)[0] for name in os.listdir(DEVICES)
            if name.endswith((".yaml", ".yml", ".json")) and not name.startswith("_"))
        missing = [name for name in shipped if name not in body]
        check("the help names every profile that ships", not missing, str(missing))
        check("and there is a section for the editor",
              "The profile editor" in dict(SECTIONS))

    def menubar_present() -> None:
        bar = getattr(app, "menubar", None)
        check("the menu bar was created", bar is not None)
        if bar is not None:
            labels = [bar.entrycget(i, "label") for i in range(bar.index("end") + 1)
                      if bar.type(i) == "cascade"]
            check("it carries Profile, View and Help",
                  {"Profile", "View", "Help"} <= set(labels), str(labels))

    def buttons_present() -> None:
        # A menu bar is not drawn at all on a bare X server with no window
        # manager, so the same actions have to be reachable as buttons.
        # Walked to any depth: which frame a button sits in is a layout
        # decision and has moved once already.
        def buttons(widget) -> list[str]:
            found = []
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    found.append(str(child.cget("text")))
                found += buttons(child)
            return found

        texts = buttons(app)
        check("Edit..., New... and Help are on screen as buttons as well",
              {"Edit...", "New...", "Help"} <= set(texts), str(sorted(set(texts))))
        check("Rescan, Reload and Open folder are no longer buttons",
              not ({"Rescan", "Reload", "Open folder"} & set(texts)),
              str(sorted(set(texts))))

    # -- the editor ---------------------------------------------------------
    def editor_opens() -> None:
        editor = ProfileEditor(root, app.profile, app.devices_dir)
        state["editor"] = editor
        check("the editor lists every register",
              len(editor.rows) == len(app.profile.registers),
              f"{len(editor.rows)} vs {len(app.profile.registers)}")

    def editor_adds() -> None:
        editor = state["editor"]
        before = len(editor.registers)
        editor.add_register()
        check("Add appends a register", len(editor.registers) == before + 1)
        check("the new register is selected",
              editor._selected_index() == len(editor.registers) - 1)

    def editor_edits() -> None:
        editor = state["editor"]
        editor.field_vars["name"].set("editor_made_this")
        editor._field_changed("name")
        editor.field_vars["unit"].set("kW")
        editor._field_changed("unit")
        check("editing a field updates the register behind it",
              editor.registers[-1]["name"] == "editor_made_this",
              str(editor.registers[-1]))

    def editor_duplicates() -> None:
        editor = state["editor"]
        before = len(editor.registers)
        editor.duplicate_register()
        new, original = editor.registers[-1], editor.registers[-2]
        check("Duplicate copies it with a fresh name and a free address",
              len(editor.registers) == before + 1
              and new["name"] != original["name"]
              and new["address"] != original["address"],
              f"{new} / {original}")

    def editor_saves() -> None:
        editor = state["editor"]
        target = os.path.join(tempfile.mkdtemp(), "edited.yaml")
        editor._path = target
        editor.save()
        check("Save writes the file", os.path.exists(target))
        if not os.path.exists(target):
            return
        reloaded = load_profile(target)
        check("the saved profile loads again",
              len(reloaded.registers) == len(editor.registers),
              f"{len(reloaded.registers)} vs {len(editor.registers)}")
        check("the register added in the editor survived the round trip",
              "editor_made_this" in [r.name for r in reloaded.registers])

    def editor_refuses_invalid() -> None:
        editor = state["editor"]
        # walk mode with no min or max is exactly what the loader rejects
        editor.registers.append({"name": "not_valid", "table": "holding",
                                 "address": "0x7000", "type": "float32", "mode": "walk"})
        check("Check refuses a profile that would not load",
              editor._validated() is None)
        editor.registers.pop()

    def editor_closes() -> None:
        editor = state.pop("editor")
        editor._dirty = False           # skip the confirmation prompt
        editor.close()
        check("the editor closes", not editor.winfo_exists())

    def finish() -> None:
        app.stop()
        simulation.stop()
        peer.stop()
        root.quit()

    sequence: list[tuple] = [
        (address_default, 350), (address_conventional, 350), (address_back, 350),
        (menu_built, 350), (copy_conventional, 350), (copy_row, 350),
        (to_client, 500),
        (read_without_polling, 2500), (read_without_polling_landed, 350),
        (start_client, 2500), (read_one, 1500), (read_one_landed, 350),
        (help_opens, 350), (help_is_single, 350), (help_reflows, 350),
        (theme_applied, 350), (fonts_are_fixed_width, 350),
        (help_lists_every_profile, 350),
        (menubar_present, 350), (buttons_present, 350),
        (editor_opens, 600), (editor_adds, 350), (editor_edits, 350),
        (editor_duplicates, 350), (editor_saves, 500),
        (editor_refuses_invalid, 350), (editor_closes, 350),
        (finish, 0),
    ]
    at = 700
    for action, delay in sequence:
        root.after(at, action if action is finish else guard(action))
        at += delay

    root.mainloop()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
