# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Smoke test for the Tkinter UI in all four modes.

Builds the real window, runs it as a TCP server, points it at itself as a TCP
client, exercises RTU over an in-memory loopback, and fails loudly on any
exception raised inside a Tk callback.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
import tkinter as tk
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool.datastore import DataStore
from creabus_tool.profile import load_profile
from creabus_tool.rtu import LoopbackSerial, SerialSettings
from creabus_tool.server import RtuServer, SimulationRunner, TcpServer
from creabus_tool.ui import ModbusToolApp
from tools.read_meter import ModbusClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES = os.path.join(HERE, "devices")
UI_PORT = 15060          # the port the UI itself serves on
PEER_PORT = 15061        # a second server the UI polls as a client
problems: list[str] = []
steps: list[tuple[str, callable]] = []


def test_refuses_old_tk() -> None:
    """An unusable Tk must say so, not open a window nobody can see.

    macOS still ships Tcl/Tk 8.5.9, which opens the window and then draws
    nothing into it. This runs run_ui() in a subprocess with the minimum
    raised beyond any real Tk, so the refusal path itself is exercised on
    whatever Tk this machine actually has.
    """
    script = textwrap.dedent(
        """
        import sys
        import tkinter.messagebox as messagebox

        shown = []
        messagebox.showerror = lambda title, message=None, **kw: shown.append(message)

        sys.path.insert(0, %r)
        from creabus_tool import compat, ui

        compat.MIN_TK_VERSION = (99, 0)           # no real Tk can satisfy this
        code = ui.run_ui(%r)

        assert code == 3, f"run_ui returned {code}, expected 3"
        assert shown, "no dialog was raised"
        assert "99.0" in shown[0], shown[0]
        print("REFUSED")
        """
    ) % (HERE, DEVICES)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=30)
    expect(result.returncode == 0, result.stderr[-800:])
    expect("REFUSED" in result.stdout, result.stdout)
    expect("99.0" in result.stderr, "the reason was not printed to stderr")


def test_dark_theme_detected() -> None:
    """A dark desktop has to be recognised through Tk's own colour resolution.

    Run in its own process: it changes the ttk theme's colours, which the
    window built below would inherit. macOS reports its background as the
    symbolic "systemWindowBackgroundColor", so the detection has to go
    through winfo_rgb rather than parse a hex string - which is what this
    exercises, with a real theme and a real colour.
    """
    script = textwrap.dedent(
        """
        import sys
        import tkinter as tk
        from tkinter import ttk

        sys.path.insert(0, %r)
        from creabus_tool import theme

        root = tk.Tk()
        root.withdraw()
        style = ttk.Style(root)
        style.theme_use("clam")

        style.configure("TFrame", background="#101010")
        assert theme.is_dark(root), "a near black theme was read as light"
        assert theme.palette_for(root) is theme.DARK

        style.configure("TFrame", background="#ededed")
        assert not theme.is_dark(root), "a near white theme was read as dark"
        assert theme.palette_for(root) is theme.LIGHT

        print("THEME-OK")
        """
    ) % (HERE,)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=30)
    expect(result.returncode == 0, result.stderr[-800:])
    expect("THEME-OK" in result.stdout, result.stdout)


def step(label: str, action) -> None:
    try:
        action()
        print(f"  ok    {label}")
    except Exception:
        problems.append(f"{label}:\n{traceback.format_exc()}")
        print(f"  FAIL  {label}")


def expect(condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(detail or "condition not met")


def reads_of(app, name: str) -> int:
    values = app.tree.item(name, "values")
    return int(values[10]) if str(values[10]).strip() else 0


def value_of(app, name: str) -> str:
    return str(app.tree.item(name, "values")[4])


def traffic_has(app, needle: str) -> bool:
    return any(needle in " ".join(str(v) for v in app.traffic.item(item, "values"))
               for item in app.traffic.get_children())


def log_has(app, needle: str) -> bool:
    return needle in app.log_text.get("1.0", "end")


def main() -> int:
    # done first, in its own process, while this one still has no Tk root
    step("an unusable Tk is refused with a reason", test_refuses_old_tk)
    step("a dark desktop selects the dark palette", test_dark_theme_detected)

    # modal dialogs would block the mainloop, so record them instead of showing
    from tkinter import messagebox
    dialogs: list[tuple] = []
    for name in ("showerror", "showwarning", "showinfo"):
        setattr(messagebox, name, lambda *a, **k: dialogs.append(a))

    # a second server for the UI to poll when it is in client mode
    peer_profile = load_profile(os.path.join(DEVICES, "sdm120.yaml"))
    peer_store = DataStore(peer_profile)
    peer_sim = SimulationRunner(peer_store, tick=0.1)
    peer_sim.start()
    peer = TcpServer(peer_store, "127.0.0.1", PEER_PORT, log_requests=False)
    peer.start()

    root = tk.Tk()
    root.geometry("1260x820")
    root.report_callback_exception = lambda *exc: problems.append(
        "tk callback: " + "".join(traceback.format_exception(*exc)))
    app = ModbusToolApp(root, DEVICES, os.path.join(DEVICES, "sdm120.yaml"),
                       "127.0.0.1", UI_PORT)

    def poll_the_ui_server() -> None:
        """Stay connected for the whole server phase, so Connections has a row."""
        try:
            client = ModbusClient("127.0.0.1", UI_PORT, 1)
            client.write_registers(0x0000, [0x4348, 0x0000])     # 200.0, once
            try:
                client.read("input", 0xF000, 2)
            except RuntimeError:
                pass                                             # expected, feeds the log
            deadline = time.time() + 6.0
            while time.time() < deadline:
                try:
                    client.read("input", 0, 40)
                except (ConnectionError, OSError):
                    break          # the UI stopped its server, which ends this phase
                time.sleep(0.15)
            client.close()
        except Exception:
            problems.append("probe client: " + traceback.format_exc())

    # ---------------------------------------------------------- server / TCP
    at = 400
    plan = [
        ("server+TCP starts", lambda: (app.start(), expect(app.busy))),
        ("probe client runs", lambda: threading.Thread(target=poll_the_ui_server).start()),
    ]
    for label, action in plan:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 400

    checks = [
        ("29 register rows", lambda: expect(len(app.tree.get_children()) == 29)),
        ("connection logged", lambda: expect(log_has(app, "client connected"))),
        ("traffic table filled", lambda: expect(len(app.traffic.get_children()) >= 5)),
        ("traffic names FC04", lambda: expect(traffic_has(app, "FC04"))),
        ("read counters move", lambda: expect(reads_of(app, "voltage") > 0)),
        ("unread registers stay blank", lambda: expect(reads_of(app, "serial_number") == 0)),
        ("connections tab lists the peer",
         lambda: expect(len(app.client_tree.get_children()) >= 1)),
        ("word order switches live", lambda: (app.word_order_var.set("low word first (CDAB)"),
                                              app._apply_protocol(),
                                              expect(app.store.profile.word_order == "little"))),
        ("word order switches back", lambda: (app.word_order_var.set("high word first (ABCD)"),
                                              app._apply_protocol(),
                                              expect(app.store.profile.word_order == "big"))),
        ("unit id changes live", lambda: (app.unit_var.set("3"),
                                          expect(app.store.profile.unit_id == 3),
                                          app.unit_var.set("1"))),
        ("hold a value", lambda: (app.store.set_value("voltage", 111.0, pin=True),
                                  expect(app.store.snapshot()[0].pinned))),
        ("release all", lambda: (app.release_all(),
                                 expect(not any(r.pinned for r in app.store.snapshot())))),
        ("switch device to sdm630",
         lambda: (app.load_device(os.path.join(DEVICES, "sdm630.yaml")),
                  expect(len(app.tree.get_children()) == 45), expect(app.busy))),
        ("switch device back",
         lambda: (app.load_device(os.path.join(DEVICES, "sdm120.yaml")),
                  expect(len(app.tree.get_children()) == 29))),
        ("server+TCP stops", lambda: (app.stop(), expect(not app.busy))),
    ]
    for label, action in checks:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 250

    # ---------------------------------------------------------- client / TCP
    def to_client_tcp() -> None:
        app.role_var.set("client")
        app.link_var.set("tcp")
        app._on_mode_change()
        expect(not app.is_server)
        expect(app.toggle_button.cget("text") == "Start polling")
        expect(app.value_button.cget("text") == "Write to device...")

    at += 300
    client_steps = [
        ("switch to client + TCP", to_client_tcp),
        ("client starts blank", lambda: expect(value_of(app, "voltage") == "-")),
        ("point it at the peer server", lambda: (app.host_var.set("127.0.0.1"),
                                                    app.port_var.set(str(PEER_PORT)),
                                                    app.interval_var.set("0.3"))),
        ("client starts polling", lambda: (app.start(), expect(app.busy))),
    ]
    for label, action in client_steps:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 400

    at += 1200          # let a couple of cycles run
    client_checks = [
        ("values arrive from the peer", lambda: expect(value_of(app, "voltage") != "-",
                                                       value_of(app, "voltage"))),
        ("the value matches what the peer serves",
         lambda: expect(abs(float(value_of(app, "voltage"))
                            - [r for r in peer_store.snapshot()
                               if r.name == "voltage"][0].value) < 0.01,
                        value_of(app, "voltage"))),
        ("client traffic is recorded", lambda: expect(traffic_has(app, "FC04"))),
        ("connections tab shows the link",
         lambda: expect(len(app.client_tree.get_children()) == 1)),
        ("the poller is cycling", lambda: expect(app.poller.cycles >= 1, str(app.poller.cycles))),
        ("write to the device from the client",
         lambda: (app.master_link.write_register_value(
             app.store.profile, app.store.profile.by_name("relay_pulse_width"), 175.0),
             time.sleep(0.2),
             expect(abs([r for r in peer_store.snapshot()
                         if r.name == "relay_pulse_width"][0].value - 175.0) < 0.01))),
        ("client stops", lambda: (app.stop(), expect(not app.busy))),
    ]
    for label, action in client_checks:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 250

    # ---------------------------------------------------------- server / RTU
    #  A real port cannot be opened in a test, so the loopback stands in for the
    #  cable; everything above the port is the code that ships.
    device_side, master_side = LoopbackSerial.pair("uitest")

    def to_server_rtu() -> None:
        app.role_var.set("server")
        app.link_var.set("rtu")
        app._on_mode_change()
        expect(app.toggle_button.cget("text") == "Start server")

    def start_rtu_server() -> None:
        settings = SerialSettings(port="uitest", baudrate=19200)
        app.server = RtuServer(app.store, settings, on_event=app._queue_event,
                               port_factory=lambda: device_side)
        app.server.start()
        expect(app.busy)

    def talk_over_rtu() -> None:
        from creabus_tool.client import ModbusMaster
        from creabus_tool.pdu import read_request
        from creabus_tool.rtu import RtuMasterTransport
        settings = SerialSettings(port="uitest", baudrate=19200)
        master = ModbusMaster(RtuMasterTransport(settings, timeout=1.0,
                                                 port_factory=lambda: master_side),
                              unit=1, retries=0)
        for _ in range(3):
            master.request(read_request(0x04, 0x0000, 40), describe=False)

    at += 300
    rtu_steps = [
        ("switch to server + RTU", to_server_rtu),
        ("RTU panel replaces the TCP panel",
         lambda: expect(app.rtu_frame.winfo_manager() == "grid"
                        and app.tcp_frame.winfo_manager() == "")),
        ("RTU server starts on the loopback", start_rtu_server),
        ("a master talks to it", talk_over_rtu),
    ]
    for label, action in rtu_steps:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 400

    at += 700
    rtu_checks = [
        ("RTU requests were served",
         lambda: expect(app.server.stats.snapshot()["requests"] >= 3,
                        str(app.server.stats.snapshot()["requests"]))),
        ("RTU frames counted", lambda: expect(app.server.rtu_stats.frames_in >= 3)),
        ("the bus master appears in Connections",
         lambda: expect(len(app.client_tree.get_children()) == 1)),
        ("RTU server stops", lambda: (app.stop(), expect(not app.busy))),
        ("starting RTU with no port fails politely",
         lambda: (dialogs.clear(), app.serial_port_var.set(""), app.start(),
                  expect(not app.busy), expect(bool(dialogs), "no error dialog was raised"))),
    ]
    for label, action in rtu_checks:
        root.after(at, lambda a=action, l=label: step(l, a))
        at += 250

    at += 400
    root.after(at, lambda: step("close", app.on_close))
    root.mainloop()

    peer.stop()
    peer_sim.stop()

    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(problem)
        return 1
    print("\nUI smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
