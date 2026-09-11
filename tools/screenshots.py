# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Regenerate the screenshots in docs/ from the real UI, on a clean display.

Run it under a virtual display, so nothing from the desktop of whoever
regenerates them can end up in the image and the result is the same every time:

    xvfb-run -a python tools/screenshots.py

It starts a peer server for the client view to poll, drives the window through
each of the four views, and grabs the window by its X id rather than grabbing
the screen, so every image is exactly the window's own contents: no desktop
behind it, and no window manager decoration to date the image to one platform.

Needs ImageMagick's `import` on the PATH (Debian/Ubuntu: apt install imagemagick).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool.client import ModbusMaster
from creabus_tool.datastore import DataStore
from creabus_tool.editor import ProfileEditor
from creabus_tool.help import show_help
from creabus_tool.pdu import read_request
from creabus_tool.profile import load_profile
from creabus_tool.rtu import LoopbackSerial, RtuMasterTransport, SerialSettings
from creabus_tool.server import RtuServer, SimulationRunner, TcpServer
from creabus_tool.ui import ModbusToolApp
from tools.read_meter import ModbusClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.path.join(HERE, "devices")
DOCS = os.path.join(HERE, "docs")

UI_PORT = 15080          # the port the window itself serves on
PEER_PORT = 15081        # the peer the window polls in client mode
WINDOW_SIZE = "1400x820"


def start_peer() -> tuple[TcpServer, SimulationRunner]:
    """A second server for the window to poll when it is in client mode."""
    profile = load_profile(os.path.join(DEVICES, "sdm120.yaml"))
    store = DataStore(profile)
    server = TcpServer(store, "127.0.0.1", PEER_PORT, log_requests=False)
    simulation = SimulationRunner(store)
    simulation.start()
    server.start()
    for _ in range(100):
        if server.running:
            break
        time.sleep(0.05)
    return server, simulation


def grab(app: ModbusToolApp, name: str) -> None:
    """Save the window - and only the window - to docs/<name>.png."""
    app.update_idletasks()
    window_id = hex(app.winfo_toplevel().winfo_id())
    target = os.path.join(DOCS, f"{name}.png")
    subprocess.run(["import", "-quiet", "-window", window_id, target], check=True)
    print(f"  wrote docs/{name}.png")


def main() -> int:
    if shutil.which("import") is None:
        sys.exit("error: ImageMagick's 'import' is not on the PATH")
    if not os.environ.get("DISPLAY"):
        sys.exit("error: no DISPLAY - run this under xvfb-run")

    os.makedirs(DOCS, exist_ok=True)
    peer_server, peer_simulation = start_peer()

    root = tk.Tk()
    root.geometry(WINDOW_SIZE)
    app = ModbusToolApp(root, DEVICES, os.path.join(DEVICES, "sdm120.yaml"),
                        host="127.0.0.1", port=UI_PORT, role="server", link="tcp")

    # The poller and the servers call back into Tk from their own threads, which
    # is only legal while the real event loop is running - so the whole sequence
    # is scheduled with after() and driven by mainloop(), never by update().
    state: dict = {}

    def serve_tcp() -> None:
        app.start()

    def open_client() -> None:
        state["client"] = ModbusClient("127.0.0.1", UI_PORT, unit=1, timeout=2.0)

    def one_read() -> None:
        client = state.get("client")
        if client is not None:
            client.read("input", 0, 40)
            client.read("input", 72, 8)

    def select(tab: int):
        return lambda: app.notebook.select(tab)

    def shoot(name: str):
        return lambda: grab(app, name)

    def stop_tcp() -> None:
        client = state.pop("client", None)
        if client is not None:
            client.close()
        app.stop()

    def to_client() -> None:
        app.role_var.set("client")
        app.link_var.set("tcp")
        app._on_mode_change()
        app.host_var.set("127.0.0.1")
        app.port_var.set(str(PEER_PORT))
        app.interval_var.set("0.5")

    def poll_peer() -> None:
        app.start()

    def to_rtu() -> None:
        # A real serial port cannot be opened here, so the loopback stands in
        # for the cable. Everything above the port is the code that ships.
        app.stop()
        app.role_var.set("server")
        app.link_var.set("rtu")
        app._on_mode_change()
        app.serial_port_var.set("COM3")
        device_side, master_side = LoopbackSerial.pair("COM3")
        settings = SerialSettings(port="COM3", baudrate=9600)
        app.server = RtuServer(app.store, settings, on_event=app._queue_event,
                               port_factory=lambda: device_side)
        app.server.start()
        # The server was started behind the UI's back, so bring the controls
        # into line with it: they would otherwise still read "stopped".
        app.toggle_button.config(text="Stop server")
        app.state_label.config(text=f"answering on {settings.describe()}",
                               style="Run.TLabel")
        app.reach_label.config(text="as unit id 1")
        app._set_connection_state("disabled")
        state["master"] = ModbusMaster(
            RtuMasterTransport(settings, timeout=1.0,
                               port_factory=lambda: master_side),
            unit=1, retries=0)

    def rtu_read() -> None:
        master = state.get("master")
        if master is not None:
            master.request(read_request(0x04, 0x0000, 40), describe=False)

    def open_editor() -> None:
        state["editor"] = ProfileEditor(root, app.profile, app.devices_dir)

    def pick_in_editor() -> None:
        editor = state["editor"]
        editor.tree.selection_set(editor.rows[4])

    def shoot_editor() -> None:
        editor = state["editor"]
        editor.update_idletasks()
        target = os.path.join(DOCS, "editor.png")
        subprocess.run(["import", "-quiet", "-window", hex(editor.winfo_id()), target],
                       check=True)
        print("  wrote docs/editor.png")

    def close_editor() -> None:
        editor = state.pop("editor")
        editor._dirty = False
        editor.close()

    def open_help() -> None:
        show_help(root)

    def shoot_help() -> None:
        window = root._help_window
        window.update_idletasks()
        target = os.path.join(DOCS, "help.png")
        subprocess.run(["import", "-quiet", "-window", hex(window.winfo_id()), target],
                       check=True)
        print("  wrote docs/help.png")
        window.destroy()

    def finish() -> None:
        if app.server is not None:
            app.server.stop()
        peer_simulation.stop()
        peer_server.stop()
        root.quit()

    # (action, milliseconds to wait before the next one)
    sequence: list[tuple] = [(serve_tcp, 2000), (open_client, 300)]
    sequence += [(one_read, 250)] * 12
    # Selecting a tab and grabbing it are separate steps: the window has to be
    # given the time to actually redraw before the pixels are worth taking.
    sequence += [
        (select(0), 700), (shoot("server-tcp"), 400),
        (select(1), 700), (shoot("traffic"), 400),
        (select(2), 700), (shoot("debug-log"), 400),
        (stop_tcp, 800),
        (to_client, 400),
        (poll_peer, 4000),
        (select(0), 700), (shoot("client-tcp"), 400),
        (to_rtu, 700),
    ]
    sequence += [(rtu_read, 250)] * 6
    sequence += [(select(0), 700), (shoot("server-rtu"), 600)]
    sequence += [
        (open_editor, 1200), (pick_in_editor, 700), (shoot_editor, 500),
        (close_editor, 500),
        (open_help, 1200), (shoot_help, 500),
        (finish, 0),
    ]

    at = 600
    for action, delay in sequence:
        root.after(at, action)
        at += delay

    root.mainloop()
    print("\nscreenshots regenerated in docs/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
