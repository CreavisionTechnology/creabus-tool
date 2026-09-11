# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Tkinter control panel: simulate a Modbus device, or poll a real one.

Four combinations, one window:

    server + TCP    listen on a socket and pretend to be a device
    server + RTU    answer on a serial port and pretend to be a device
    client + TCP    poll a real device over the network
    client + RTU    poll a real device over RS-485 / a USB serial adapter
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import branding, compat, theme
from .editor import ProfileEditor
from .help import show_help
from .client import Block, ModbusMaster, Poller, TcpMasterTransport, plan_blocks
from .datastore import DataStore, decode_words
from .pdu import ModbusError, TransportError
from .profile import (
    BIT_TABLES,
    DeviceProfile,
    ProfileError,
    discover_profiles,
    load_profile,
    parse_profile,
)
from .rtu import COMMON_BAUD_RATES, PARITIES, RtuMasterTransport, SerialSettings, list_serial_ports
from .server import RtuServer, SimulationRunner, TcpServer, local_addresses

MAX_LOG_LINES = 4000
MAX_TRAFFIC_ROWS = 500
MAX_TRAFFIC_INSERTS_PER_REFRESH = 60
RECENTLY_READ_SECONDS = 3.0

# The server, poller and simulation threads hand events to the UI through a
# queue. A busy bus can produce them faster than a 60 Hz window can draw them,
# so the queue is bounded: past this point the oldest events are dropped and
# counted, which costs a line in the log but keeps memory flat under load.
MAX_QUEUED_EVENTS = 5000
MAX_EVENTS_PER_DRAIN = 600

BOLD_KINDS = ("conn", "write", "warn", "error")

WORD_ORDERS = {"high word first (ABCD)": "big", "low word first (CDAB)": "little"}
BYTE_ORDERS = {"normal (AB)": "big", "swapped (BA)": "little"}
GAP_POLICIES = {"read back as zero": "zero", "exception 0x02": "exception"}


def _reverse(mapping: dict[str, str], value: str, default: str) -> str:
    for label, stored in mapping.items():
        if stored == value:
            return label
    return default


class ModbusToolApp(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        devices_dir: str,
        profile_path: str = "",
        host: str = "0.0.0.0",
        port: int = 502,
        role: str = "server",
        link: str = "tcp",
        serial_port: str = "",
        autostart: bool = False,
    ):
        super().__init__(master, padding=8)
        self.master.title(branding.window_title())
        branding.apply_window_icon(self.master)
        self.devices_dir = os.path.abspath(devices_dir)
        self.events: queue.Queue = queue.Queue(maxsize=MAX_QUEUED_EVENTS)
        self._events_dropped = 0
        self._events_lock = threading.Lock()
        self._closing = False
        self._drain_job: str | None = None
        self._refresh_job: str | None = None
        self.profile: DeviceProfile | None = None
        self._profile_mtime = 0.0
        self.store: DataStore | None = None
        self.simulation: SimulationRunner | None = None
        self.server = None                     # TcpServer or RtuServer
        self.master_link: ModbusMaster | None = None
        self.poller: Poller | None = None
        self._rows: dict[str, tuple] = {}
        self._traffic_pending: list[tuple] = []
        self._traffic_dropped = 0
        self._traffic_seq = 0
        self._syncing = False
        self._serial_ports: dict[str, str] = {}
        self._last_request_count = 0
        self._last_rate_time = time.monotonic()
        self._rate = 0.0

        self.body_font, self.mono_font, self.mono_bold = compat.ui_fonts()

        self.profile_paths = discover_profiles(self.devices_dir)
        if profile_path:
            profile_path = os.path.abspath(profile_path)
            if profile_path not in self.profile_paths:
                self.profile_paths.insert(0, profile_path)

        self.role_var = tk.StringVar(value=role)
        self.link_var = tk.StringVar(value=link)
        # Device manuals number registers from one with a table digit in front
        # (holding 0 is "40001"); the protocol carries the zero based address.
        # Both are shown so neither camp has to convert in their head.
        self.conventional_var = tk.BooleanVar(value=False)
        self._build_widgets()
        self.pack(fill="both", expand=True)

        self.host_var.set(host)
        self.port_var.set(str(port))
        if serial_port:
            self.serial_port_var.set(serial_port)

        start_with = profile_path or self._default_profile()
        if start_with:
            self.device_var.set(self._label_for(start_with))
            self.load_device(start_with)
        else:
            self.log("error", f"no device profiles found in {self.devices_dir}")

        self._apply_mode_widgets()
        self._drain_job = self.after(120, self._drain_events)
        self._refresh_job = self.after(500, self._refresh)
        self.master.protocol("WM_DELETE_WINDOW", self.on_close)
        if autostart:
            self.after(300, self.start)

    # ================================================================== setup
    def _default_profile(self) -> str:
        for path in self.profile_paths:
            if "sdm120" in os.path.basename(path).lower():
                return path
        return self.profile_paths[0] if self.profile_paths else ""

    @staticmethod
    def _label_for(path: str) -> str:
        return os.path.basename(path)

    def _path_for(self, label: str) -> str:
        for path in self.profile_paths:
            if self._label_for(path) == label:
                return path
        return ""

    @property
    def is_server(self) -> bool:
        return self.role_var.get() == "server"

    @property
    def is_rtu(self) -> bool:
        return self.link_var.get() == "rtu"

    @property
    def busy(self) -> bool:
        """True while a server is listening or a poller is running."""
        if self.server is not None and self.server.running:
            return True
        return self.poller is not None and self.poller.running

    def _build_widgets(self) -> None:
        style = ttk.Style()
        chosen = compat.pick_theme(list(style.theme_names()))
        if chosen:
            style.theme_use(chosen)
        # Only now: the palette is read off the theme that is actually in use,
        # so it has to be picked after theme_use and before anything coloured.
        self.palette = theme.palette_for(self)
        style.configure("Status.TLabel", padding=(6, 3))
        style.configure("Run.TLabel", foreground=self.palette.log["conn"],
                        font=(*self.body_font, "bold"))
        style.configure("Stop.TLabel", foreground=self.palette.log["error"],
                        font=(*self.body_font, "bold"))
        style.configure("Hint.TLabel", foreground=self.palette.muted)
        style.configure("Brand.TLabel", foreground=self.palette.muted)

        self._build_menubar()
        self._build_device_box()
        self._build_connection_box()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, pady=(8, 6))
        self.notebook = notebook
        self._build_register_tab(notebook)
        self._build_traffic_tab(notebook)
        self._build_log_tab(notebook)
        self._build_link_tab(notebook)

        self.status_var = tk.StringVar(value="idle")
        footer = ttk.Frame(self, relief="sunken", borderwidth=1)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w").pack(side="left", fill="x", expand=True)
        branding.attach_attribution(footer, self.body_font).pack(side="right",
                                                                padx=(10, 8))
        # Out of the profile row, where it sat among buttons that do something
        # to the profile and it did not. Down here it is always in the same
        # place whatever tab is open, and still reachable when the menu bar is
        # not drawn at all, which is the case on a bare X server.
        help_button = ttk.Button(footer, text="Help", width=6,
                                 command=lambda: show_help(self.winfo_toplevel()))
        help_button.pack(side="right", padx=(0, 4))

    def _build_device_box(self) -> None:
        box = ttk.LabelFrame(self, text="Device profile", padding=8)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Profile:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(
            box, textvariable=self.device_var, state="readonly",
            values=[self._label_for(p) for p in self.profile_paths],
        )
        self.device_box.grid(row=0, column=1, sticky="ew")
        self.device_box.bind("<<ComboboxSelected>>", self._on_device_selected)
        # Opening the list is the moment the answer has to be right, so the
        # folder is re-read then. Rescan and Reload were buttons for this and
        # no longer need to be: an instance binding runs before the class one
        # that posts the list, so the values are already updated when it drops.
        for sequence in ("<Button-1>", "<FocusIn>"):
            self.device_box.bind(sequence, self._refresh_profiles, add="+")

        buttons = ttk.Frame(box)
        buttons.grid(row=0, column=2, sticky="e", padx=(6, 0))
        # These two duplicate the Profile menu on purpose: a menu bar is easy
        # to miss, and on a bare X server with no window manager it is not
        # drawn at all. Everything else lives in the menu only.
        ttk.Button(buttons, text="Edit...", width=8,
                   command=self.edit_profile).pack(side="left")
        ttk.Button(buttons, text="New...", width=8,
                   command=self.new_profile).pack(side="left", padx=(4, 0))

        self.device_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.device_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

    def _build_connection_box(self) -> None:
        box = ttk.LabelFrame(self, text="Connection", padding=8)
        box.pack(fill="x", pady=(6, 0))
        box.columnconfigure(0, weight=1)

        # --- role and link ----------------------------------------------------
        picker = ttk.Frame(box)
        picker.grid(row=0, column=0, sticky="ew")
        ttk.Label(picker, text="Role:", width=7).pack(side="left")
        ttk.Radiobutton(picker, text="Simulate a device (server)", value="server",
                        variable=self.role_var,
                        command=self._on_mode_change).pack(side="left")
        ttk.Radiobutton(picker, text="Poll a device (client)", value="client",
                        variable=self.role_var,
                        command=self._on_mode_change).pack(side="left", padx=(12, 0))
        ttk.Label(picker, text="      Link:").pack(side="left")
        ttk.Radiobutton(picker, text="Modbus TCP", value="tcp", variable=self.link_var,
                        command=self._on_mode_change).pack(side="left", padx=(4, 0))
        ttk.Radiobutton(picker, text="Modbus RTU (serial)", value="rtu",
                        variable=self.link_var,
                        command=self._on_mode_change).pack(side="left", padx=(12, 0))

        ttk.Separator(box, orient="horizontal").grid(row=1, column=0, sticky="ew", pady=6)

        self._build_tcp_frame(box)
        self._build_rtu_frame(box)
        self._build_modbus_frame(box)
        self._build_server_behaviour(box)
        self._build_client_behaviour(box)

        actions = ttk.Frame(box)
        actions.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        self.toggle_button = ttk.Button(actions, text="Start server", width=15,
                                        command=self.toggle)
        self.toggle_button.pack(side="left")
        self.state_label = ttk.Label(actions, text="stopped", style="Stop.TLabel")
        self.state_label.pack(side="left", padx=10)
        self.reach_label = ttk.Label(actions, text="", style="Hint.TLabel")
        self.reach_label.pack(side="left", padx=10)

    def _build_tcp_frame(self, box: ttk.LabelFrame) -> None:
        frame = ttk.Frame(box)
        frame.grid(row=2, column=0, sticky="ew")
        self.tcp_frame = frame

        self.host_label = ttk.Label(frame, text="Bind IP:", width=22, anchor="w")
        self.host_label.grid(row=0, column=0, sticky="w", pady=3)
        self.host_var = tk.StringVar(value="0.0.0.0")
        self.host_box = ttk.Combobox(frame, textvariable=self.host_var,
                                     values=local_addresses(), width=22)
        self.host_box.grid(row=0, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="TCP port:").grid(row=0, column=2, sticky="e", padx=(14, 4))
        self.port_var = tk.StringVar(value="502")
        self.port_entry = ttk.Entry(frame, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=3, sticky="w")

        self.tcp_hint = ttk.Label(frame, text="", style="Hint.TLabel")
        self.tcp_hint.grid(row=0, column=4, sticky="w", padx=(14, 0))

    def _build_rtu_frame(self, box: ttk.LabelFrame) -> None:
        frame = ttk.Frame(box)
        frame.grid(row=3, column=0, sticky="ew")
        self.rtu_frame = frame

        ttk.Label(frame, text="Serial port:", width=22, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3)
        self.serial_port_var = tk.StringVar()
        self.serial_box = ttk.Combobox(frame, textvariable=self.serial_port_var, width=34)
        self.serial_box.grid(row=0, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Button(frame, text="Refresh", width=9,
                   command=self.refresh_serial_ports).grid(row=0, column=3, padx=6)
        self.serial_hint = ttk.Label(frame, text="", style="Hint.TLabel")
        self.serial_hint.grid(row=0, column=4, sticky="w", padx=(8, 0))

        ttk.Label(frame, text="Baud rate:", width=22, anchor="w").grid(
            row=1, column=0, sticky="w", pady=3)
        self.baud_var = tk.StringVar(value="9600")
        ttk.Combobox(frame, textvariable=self.baud_var, width=10,
                     values=[str(b) for b in COMMON_BAUD_RATES]).grid(
            row=1, column=1, sticky="w", pady=3)

        ttk.Label(frame, text="Parity:").grid(row=1, column=2, sticky="e", padx=(14, 4))
        self.parity_var = tk.StringVar(value="none")
        ttk.Combobox(frame, textvariable=self.parity_var, state="readonly", width=8,
                     values=list(PARITIES)).grid(row=1, column=3, sticky="w")

        bits = ttk.Frame(frame)
        bits.grid(row=1, column=4, sticky="w", padx=(14, 0))
        ttk.Label(bits, text="Data bits:").pack(side="left")
        self.databits_var = tk.StringVar(value="8")
        ttk.Combobox(bits, textvariable=self.databits_var, state="readonly", width=4,
                     values=["7", "8"]).pack(side="left", padx=(4, 10))
        ttk.Label(bits, text="Stop bits:").pack(side="left")
        self.stopbits_var = tk.StringVar(value="1")
        ttk.Combobox(bits, textvariable=self.stopbits_var, state="readonly", width=4,
                     values=["1", "2"]).pack(side="left", padx=4)

        self.refresh_serial_ports(quiet=True)

    def _build_modbus_frame(self, box: ttk.LabelFrame) -> None:
        frame = ttk.Frame(box)
        frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        self.modbus_frame = frame

        ttk.Label(frame, text="Modbus address (unit id):", width=22, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3)
        self.unit_var = tk.StringVar(value="1")
        self.unit_entry = ttk.Entry(frame, textvariable=self.unit_var, width=8)
        self.unit_entry.grid(row=0, column=1, sticky="w", pady=3)

        self.any_unit_var = tk.BooleanVar(value=False)
        self.any_unit_check = ttk.Checkbutton(frame, text="answer any unit id",
                                              variable=self.any_unit_var,
                                              command=self._apply_protocol)
        self.any_unit_check.grid(row=0, column=2, columnspan=2, sticky="w", padx=(14, 0))

        ttk.Label(frame, text="32-bit word order:", width=22, anchor="w").grid(
            row=1, column=0, sticky="w", pady=3)
        self.word_order_var = tk.StringVar(value="high word first (ABCD)")
        word_box = ttk.Combobox(frame, textvariable=self.word_order_var, state="readonly",
                                values=list(WORD_ORDERS), width=22)
        word_box.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        word_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_protocol())

        ttk.Label(frame, text="Byte order:").grid(row=1, column=3, sticky="e", padx=(14, 4))
        self.byte_order_var = tk.StringVar(value="normal (AB)")
        byte_box = ttk.Combobox(frame, textvariable=self.byte_order_var, state="readonly",
                                values=list(BYTE_ORDERS), width=14)
        byte_box.grid(row=1, column=4, sticky="w")

        byte_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_protocol())
        self.gap_label = ttk.Label(frame, text="Undefined address:")
        self.gap_label.grid(row=1, column=5, sticky="e", padx=(14, 4))
        self.gap_var = tk.StringVar(value="read back as zero")
        self.gap_box = ttk.Combobox(frame, textvariable=self.gap_var, state="readonly",
                                    values=list(GAP_POLICIES), width=18)
        self.gap_box.grid(row=1, column=6, sticky="w")
        self.gap_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_protocol())

    def _build_server_behaviour(self, box: ttk.LabelFrame) -> None:
        frame = ttk.Frame(box)
        frame.grid(row=5, column=0, sticky="ew")
        self.server_frame = frame

        ttk.Label(frame, text="Response delay:", width=22, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3)
        delay = ttk.Frame(frame)
        delay.grid(row=0, column=1, sticky="w", pady=3)
        self.delay_var = tk.StringVar(value="0")
        ttk.Entry(delay, textvariable=self.delay_var, width=8).pack(side="left")
        ttk.Label(delay, text=" ms").pack(side="left")

        ttk.Label(frame, text="Max clients:").grid(row=0, column=2, sticky="e", padx=(14, 4))
        self.max_clients_var = tk.StringVar(value="16")
        ttk.Entry(frame, textvariable=self.max_clients_var, width=8).grid(
            row=0, column=3, sticky="w")
        self.max_clients_hint = ttk.Label(frame, text="(0 = unlimited, TCP only)",
                                          style="Hint.TLabel")
        self.max_clients_hint.grid(row=0, column=4, sticky="w", padx=(6, 0))

        for var in (self.unit_var, self.delay_var, self.max_clients_var):
            var.trace_add("write", lambda *_a: self._apply_protocol())

    def _build_client_behaviour(self, box: ttk.LabelFrame) -> None:
        frame = ttk.Frame(box)
        frame.grid(row=5, column=0, sticky="ew")
        self.client_frame = frame

        ttk.Label(frame, text="Poll every:", width=22, anchor="w").grid(
            row=0, column=0, sticky="w", pady=3)
        interval = ttk.Frame(frame)
        interval.grid(row=0, column=1, sticky="w", pady=3)
        # Ten seconds, not one: the default points at a real meter, and a
        # once-a-second poll of somebody's switchboard is rude by default.
        self.interval_var = tk.StringVar(value="10.0")
        ttk.Entry(interval, textvariable=self.interval_var, width=8).pack(side="left")
        ttk.Label(interval, text=" s").pack(side="left")

        ttk.Label(frame, text="Timeout:").grid(row=0, column=2, sticky="e", padx=(14, 4))
        timeout = ttk.Frame(frame)
        timeout.grid(row=0, column=3, sticky="w")
        self.timeout_var = tk.StringVar(value="1000")
        ttk.Entry(timeout, textvariable=self.timeout_var, width=8).pack(side="left")
        ttk.Label(timeout, text=" ms").pack(side="left")

        ttk.Label(frame, text="Retries:").grid(row=0, column=4, sticky="e", padx=(14, 4))
        self.retries_var = tk.StringVar(value="1")
        ttk.Entry(frame, textvariable=self.retries_var, width=6).grid(row=0, column=5, sticky="w")

        self.blocks_label = ttk.Label(frame, text="", style="Hint.TLabel")
        self.blocks_label.grid(row=0, column=6, sticky="w", padx=(14, 0))

    # ---------------------------------------------------------------- the tabs
    def _build_register_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Registers")

        columns = ("table", "address", "label", "type", "value", "unit",
                   "mode", "interval", "next", "updates", "reads", "lastread")
        headings = {
            "table": ("Table", 62), "address": ("Address", 120), "label": ("Register", 190),
            "type": ("Type", 62), "value": ("Value", 100), "unit": ("Unit", 48),
            "mode": ("Mode", 84), "interval": ("Every", 52), "next": ("Next", 52),
            "updates": ("Changes", 62), "reads": ("Reads", 55), "lastread": ("Last read", 78),
        }
        numeric = ("value", "interval", "next", "updates", "reads", "lastread")
        wrapper = ttk.Frame(tab)
        wrapper.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrapper, columns=columns, show="headings", selectmode="browse")
        for column in columns:
            title, width = headings[column]
            anchor = "e" if column in numeric else "w"
            self.tree.heading(column, text=title, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor, stretch=column == "label")
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for tag in ("hot", "pinned", "failed"):
            background, ink = getattr(self.palette, tag)
            self.tree.tag_configure(tag, background=background, foreground=ink)
        self.tree.bind("<Double-1>", lambda _e: self.activate_selected())
        self._build_register_menu()
        # Button-3 everywhere, plus the two gestures macOS uses for a right click
        for sequence in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            self.tree.bind(sequence, self._popup_register_menu)

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(6, 0))
        self.value_button = ttk.Button(bar, text="Set value...", command=self.edit_selected)
        self.value_button.pack(side="left")
        self.release_button = ttk.Button(bar, text="Release", command=self.release_selected)
        self.release_button.pack(side="left", padx=4)
        self.release_all_button = ttk.Button(bar, text="Release all", command=self.release_all)
        self.release_all_button.pack(side="left")
        ttk.Checkbutton(bar, text="4xxxx addresses", variable=self.conventional_var,
                        command=self.on_address_style_change).pack(side="left", padx=(12, 0))
        self.register_hint = ttk.Label(bar, style="Hint.TLabel", text="")
        self.register_hint.pack(side="left", padx=8)

    def _build_traffic_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Traffic")

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        self.traffic_paused = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Pause", variable=self.traffic_paused).pack(side="left")
        self.traffic_hint = ttk.Label(bar, text="newest first", style="Hint.TLabel")
        self.traffic_hint.pack(side="left", padx=10)
        ttk.Button(bar, text="Clear", command=self.clear_traffic).pack(side="right")

        columns = ("time", "client", "function", "address", "count", "result")
        headings = {"time": ("Time", 100), "client": ("Peer", 160),
                    "function": ("Function", 200), "address": ("Address", 130),
                    "count": ("Qty", 50), "result": ("Registers / result", 420)}
        wrapper = ttk.Frame(tab)
        wrapper.pack(fill="both", expand=True)
        self.traffic = ttk.Treeview(wrapper, columns=columns, show="headings")
        for column in columns:
            title, width = headings[column]
            anchor = "e" if column == "count" else "w"
            self.traffic.heading(column, text=title, anchor=anchor)
            self.traffic.column(column, width=width, anchor=anchor,
                                stretch=column == "result")
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.traffic.yview)
        self.traffic.configure(yscrollcommand=scroll.set)
        self.traffic.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for tag, source in (("failed", self.palette.failed), ("write", self.palette.pinned)):
            background, ink = source
            self.traffic.tag_configure(tag, background=background, foreground=ink)

    def _build_log_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Debug log")

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 6))
        self.log_requests_var = tk.BooleanVar(value=True)
        self.log_frames_var = tk.BooleanVar(value=False)
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Log requests", variable=self.log_requests_var,
                        command=self._apply_log_options).pack(side="left")
        ttk.Checkbutton(bar, text="Log raw frames (hex)", variable=self.log_frames_var,
                        command=self._apply_log_options).pack(side="left", padx=8)
        ttk.Checkbutton(bar, text="Auto scroll", variable=self.autoscroll_var).pack(side="left")
        ttk.Label(bar, text="   Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.filter_var, width=22).pack(side="left", padx=4)
        ttk.Label(bar, text="(only lines containing this text)",
                  style="Hint.TLabel").pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_log).pack(side="right")
        ttk.Button(bar, text="Save...", command=self.save_log).pack(side="right", padx=4)

        wrapper = ttk.Frame(tab)
        wrapper.pack(fill="both", expand=True)
        wrapper.rowconfigure(0, weight=1)
        wrapper.columnconfigure(0, weight=1)
        self.log_text = tk.Text(wrapper, height=14, wrap="none", state="disabled",
                                font=self.mono_font, relief="flat",
                                background=self.palette.surface,
                                foreground=self.palette.ink,
                                insertbackground=self.palette.cursor,
                                selectbackground=self.palette.select,
                                highlightthickness=0)
        yscroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.log_text.yview)
        xscroll = ttk.Scrollbar(wrapper, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        for kind, colour in self.palette.log.items():
            font = self.mono_bold if kind in BOLD_KINDS else self.mono_font
            self.log_text.tag_configure(kind, foreground=colour, font=font)

    def _build_link_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=6)
        notebook.add(tab, text="Connections")
        self.link_tab = tab
        columns = ("peer", "since", "requests", "exceptions", "last", "doing")
        headings = {"peer": ("Peer", 190), "since": ("Since", 100),
                    "requests": ("Requests", 80), "exceptions": ("Errors", 65),
                    "last": ("Last seen", 90), "doing": ("Last request", 440)}
        self.client_tree = ttk.Treeview(tab, columns=columns, show="headings", height=8)
        for column in columns:
            title, width = headings[column]
            anchor = "e" if column in ("requests", "exceptions") else "w"
            self.client_tree.heading(column, text=title, anchor=anchor)
            self.client_tree.column(column, width=width, anchor=anchor,
                                    stretch=column == "doing")
        self.client_tree.pack(fill="both", expand=True)
        self.client_hint = ttk.Label(tab, text="not running", style="Hint.TLabel")
        self.client_hint.pack(anchor="w", pady=(6, 0))

    # ========================================================== mode switching
    def _on_mode_change(self) -> None:
        if self.busy:
            self.stop()
        self._apply_mode_widgets()
        if self.profile is not None:
            self.load_device(self.profile.source_path)

    def _apply_mode_widgets(self) -> None:
        server, rtu = self.is_server, self.is_rtu

        if rtu:
            self.tcp_frame.grid_remove()
            self.rtu_frame.grid()
        else:
            self.rtu_frame.grid_remove()
            self.tcp_frame.grid()
        if server:
            self.client_frame.grid_remove()
            self.server_frame.grid()
        else:
            self.server_frame.grid_remove()
            self.client_frame.grid()

        self.host_label.config(text="Bind IP:" if server else "Device address:")
        self.tcp_hint.config(text="0.0.0.0 listens on every interface" if server
                             else "the IP or host name of the device to poll")
        self.any_unit_check.state(["!disabled"] if server else ["disabled"])
        for widget in (self.gap_label, self.gap_box):
            widget.grid() if server else widget.grid_remove()
        self.toggle_button.config(text="Start server" if server else "Start polling")
        self.value_button.config(text="Set value..." if server else "Write to device...")
        for button in (self.release_button, self.release_all_button):
            button.state(["!disabled"] if server else ["disabled"])
        self.register_hint.config(
            text="green = read by a client just now   |   double click a row to hold it"
            if server else
            "green = read just now   |   right click a row to read or write it, "
            "polling or not")
        self.client_hint.config(text="not running")
        if not self.is_server:
            self.serial_hint.config(text=compat.serial_permission_note())
        else:
            self.serial_hint.config(text="")

    # ============================================================ device load
    def load_device(self, path: str) -> None:
        was_busy = self.busy
        if was_busy:
            self.stop()
        try:
            profile = load_profile(path)
        except ProfileError as exc:
            self.log("error", f"could not load {os.path.basename(path)}: {exc}")
            messagebox.showerror("Device profile", str(exc), parent=self)
            return

        if self.simulation is not None:
            self.simulation.stop()
            self.simulation = None

        self.profile = profile
        self._profile_mtime = self._mtime_of(path)
        self.store = DataStore(profile, on_event=self._queue_event)
        if self.is_server:
            self.simulation = SimulationRunner(self.store)
            self.simulation.start()
        else:
            # nothing is simulated in client mode: the values come off the wire
            self.store.blank()

        self._syncing = True
        self.unit_var.set(str(profile.unit_id))
        self.any_unit_var.set(profile.accept_any_unit_id)
        self.word_order_var.set(_reverse(WORD_ORDERS, profile.word_order,
                                         "high word first (ABCD)"))
        self.byte_order_var.set(_reverse(BYTE_ORDERS, profile.byte_order, "normal (AB)"))
        self.gap_var.set(_reverse(GAP_POLICIES, profile.gap_policy, "read back as zero"))
        self._syncing = False

        blocks = plan_blocks(profile)
        self.blocks_label.config(text=f"{len(blocks)} request(s) per cycle "
                                      f"for {len(profile.registers)} registers")
        self.device_label.config(
            text=f"{profile.title} - {len(profile.registers)} registers"
                 + (f" - {profile.description}" if profile.description else "")
        )
        self.master.title(branding.window_title(
            f"{'simulating' if self.is_server else 'polling'} {profile.title}"))
        self._rebuild_register_rows()
        self.log("info", f"loaded '{profile.title}' from {path}")
        if was_busy:
            self.start()

    def _on_device_selected(self, _event=None) -> None:
        path = self._path_for(self.device_var.get())
        if path:
            self.load_device(path)

    def rescan_devices(self) -> None:
        self._scan_profiles()
        self.log("info", f"{len(self.profile_paths)} device profile(s) in {self.devices_dir}")

    def _scan_profiles(self) -> bool:
        """Re-read the folder into the dropdown. True if the list changed."""
        found = discover_profiles(self.devices_dir)
        current = self.profile.source_path if self.profile else ""
        if current and current not in found and os.path.isfile(current):
            found.insert(0, current)          # loaded from elsewhere, keep it listed
        if found == self.profile_paths:
            return False
        self.profile_paths = found
        self.device_box.config(values=[self._label_for(path) for path in found])
        return True

    def _refresh_profiles(self, _event=None) -> None:
        """Pick up profiles added, removed or edited since the list last opened.

        Quiet on purpose: this runs every time the dropdown is touched, so it
        only says anything when something actually changed.
        """
        if self._scan_profiles():
            self.log("info", f"{len(self.profile_paths)} device profile(s) "
                             f"in {self.devices_dir}")
        self._reload_if_file_changed()

    @staticmethod
    def _mtime_of(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    def _reload_if_file_changed(self) -> None:
        """Re-read the loaded profile if it was edited outside the window."""
        if self.profile is None or not self.profile.source_path:
            return
        stamp = self._mtime_of(self.profile.source_path)
        if self._profile_mtime and stamp > self._profile_mtime:
            self.log("info", f"{os.path.basename(self.profile.source_path)} "
                             "changed on disk, reloading")
            self.reload_device()

    def reload_device(self) -> None:
        if self.profile is not None:
            self.load_device(self.profile.source_path)

    def open_devices_folder(self) -> None:
        try:
            compat.open_folder(self.devices_dir)
        except (AttributeError, OSError) as exc:
            self.log("warn", f"cannot open {self.devices_dir}: {exc}")

    def refresh_serial_ports(self, quiet: bool = False) -> None:
        ports = list_serial_ports()
        self._serial_ports = {f"{device} - {description}": device
                              for device, description in ports}
        self.serial_box.config(values=list(self._serial_ports))
        if ports and not self.serial_port_var.get():
            self.serial_port_var.set(next(iter(self._serial_ports)))
        if not quiet:
            if ports:
                self.log("info", f"{len(ports)} serial port(s): "
                                 + ", ".join(device for device, _ in ports))
            else:
                self.log("warn", "no serial ports found - plug in the adapter and press "
                                 "Refresh " + compat.serial_permission_note())

    def selected_serial_port(self) -> str:
        raw = self.serial_port_var.get().strip()
        return self._serial_ports.get(raw, raw.split(" - ")[0] if " - " in raw else raw)

    # =============================================================== settings
    def _apply_protocol(self) -> None:
        """Push the protocol settings into whatever is running."""
        if self._syncing or self.store is None:
            return
        profile = self.store.profile

        try:
            unit = int(self.unit_var.get())
        except ValueError:
            unit = None
        if unit is not None and 0 <= unit <= 255 and unit != profile.unit_id:
            profile.unit_id = unit
            if self.master_link is not None:
                self.master_link.unit = unit
            self.log("info", f"Modbus address (unit id) is now {unit}")

        if self.any_unit_var.get() != profile.accept_any_unit_id:
            profile.accept_any_unit_id = self.any_unit_var.get()
            self.log("info", "answering any unit id" if profile.accept_any_unit_id
                     else f"answering unit id {profile.unit_id} only")

        word = WORD_ORDERS.get(self.word_order_var.get(), "big")
        byte = BYTE_ORDERS.get(self.byte_order_var.get(), "big")
        if (word, byte) != (profile.word_order, profile.byte_order):
            self.store.set_framing(word, byte)
            self.log("info", f"word order {word}, byte order {byte} - "
                             "all registers re-encoded")

        gap = GAP_POLICIES.get(self.gap_var.get(), "zero")
        if gap != profile.gap_policy:
            profile.gap_policy = gap
            self.log("info", "undefined addresses now "
                             f"{'return zeros' if gap == 'zero' else 'raise exception 0x02'}")

        if self.server is not None:
            try:
                delay = max(0.0, float(self.delay_var.get()) / 1000.0)
            except ValueError:
                delay = self.server.response_delay
            if delay != self.server.response_delay:
                self.server.response_delay = delay
                self.log("info", f"response delay {delay * 1000:.0f} ms")
            try:
                limit = max(0, int(self.max_clients_var.get()))
            except ValueError:
                limit = self.server.max_clients
            if limit != self.server.max_clients:
                self.server.max_clients = limit
                self.log("info", f"client limit {limit or 'unlimited'}")

        if self.poller is not None:
            try:
                interval = max(0.05, float(self.interval_var.get()))
            except ValueError:
                interval = self.poller.interval
            if interval != self.poller.interval:
                self.poller.interval = interval
                self.log("info", f"poll interval {interval:g}s")

    def _apply_log_options(self) -> None:
        if self.server is not None:
            self.server.log_requests = self.log_requests_var.get()
            self.server.log_frames = self.log_frames_var.get()
        if self.poller is not None:
            self.poller.log_requests = self.log_requests_var.get()

    # ========================================================= start and stop
    def toggle(self) -> None:
        if self.busy:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.store is None:
            messagebox.showwarning("No device", "Load a device profile first.", parent=self)
            return
        try:
            if self.is_server:
                self._start_server()
            else:
                self._start_client()
        except (TransportError, OSError) as exc:
            self._fail_to_start(exc)
            return
        self.toggle_button.config(text="Stop server" if self.is_server else "Stop polling")
        self._set_connection_state("disabled")

    def _fail_to_start(self, exc: Exception) -> None:
        self.server = None
        self.poller = None
        self.master_link = None
        message = str(exc)
        if self.is_rtu:
            extra = compat.serial_permission_note()
        else:
            try:
                extra = compat.privileged_port_note(int(self.port_var.get()))
            except ValueError:
                extra = ""
        self.log("error", f"cannot start: {message}")
        messagebox.showerror("Cannot start", f"{message}\n\n{extra}".strip(), parent=self)
        self.state_label.config(text="stopped", style="Stop.TLabel")

    def _serial_settings(self) -> SerialSettings:
        port = self.selected_serial_port()
        if not port:
            raise TransportError("choose a serial port first (press Refresh to list them)")
        return SerialSettings(
            port=port,
            baudrate=int(self.baud_var.get()),
            parity=self.parity_var.get(),
            bytesize=int(self.databits_var.get()),
            stopbits=float(self.stopbits_var.get()),
        )

    def _int_field(self, var: tk.StringVar, name: str, low: int, high: int) -> int:
        try:
            value = int(var.get())
        except ValueError:
            raise TransportError(f"{name} must be a whole number") from None
        if not low <= value <= high:
            raise TransportError(f"{name} must be between {low} and {high}")
        return value

    def _start_server(self) -> None:
        unit = self._int_field(self.unit_var, "Modbus address (unit id)", 0, 255)
        self.store.profile.unit_id = unit
        self.store.profile.accept_any_unit_id = self.any_unit_var.get()
        try:
            delay = max(0.0, float(self.delay_var.get()) / 1000.0)
        except ValueError:
            raise TransportError("Response delay must be a number of milliseconds") from None

        if self.is_rtu:
            settings = self._serial_settings()
            self.server = RtuServer(
                self.store, settings, on_event=self._queue_event,
                log_requests=self.log_requests_var.get(),
                log_frames=self.log_frames_var.get(), response_delay=delay,
            )
            self.server.start()
            self.state_label.config(text=f"answering on {settings.describe()}",
                                    style="Run.TLabel")
            self.reach_label.config(text=f"as unit id {unit}")
        else:
            port = self._int_field(self.port_var, "TCP port", 1, 65535)
            limit = self._int_field(self.max_clients_var, "Max clients", 0, 4096)
            host = self.host_var.get().strip() or "0.0.0.0"
            self.server = TcpServer(
                self.store, host=host, port=port, on_event=self._queue_event,
                log_requests=self.log_requests_var.get(),
                log_frames=self.log_frames_var.get(), response_delay=delay,
                max_clients=limit,
            )
            self.server.start()
            self.state_label.config(text=f"listening on {host}:{port}", style="Run.TLabel")
            if host in ("0.0.0.0", ""):
                others = [a for a in local_addresses() if a != "0.0.0.0"]
                self.reach_label.config(text="reachable at " + ", ".join(others))
            else:
                self.reach_label.config(text="")

    def _make_master(self) -> tuple:
        """Build the client link from the fields on screen, not yet opened.

        Reads Tk variables, so it must run on the UI thread. Opening is left
        to the caller: a one-off request opens it on a worker instead, so a
        device that is not there cannot freeze the window for the timeout.
        """
        unit = self._int_field(self.unit_var, "Modbus address (unit id)", 0, 255)
        retries = self._int_field(self.retries_var, "Retries", 0, 10)
        try:
            timeout = max(0.05, float(self.timeout_var.get()) / 1000.0)
        except ValueError:
            raise TransportError("Timeout must be a number") from None

        def on_frame(direction: str, data: bytes) -> None:
            if self.log_frames_var.get():
                self._queue_event("frame", f"{direction.upper()} {data.hex(' ')}")

        if self.is_rtu:
            settings = self._serial_settings()
            transport = RtuMasterTransport(settings, timeout=timeout, on_frame=on_frame)
            target = settings.describe()
        else:
            port = self._int_field(self.port_var, "TCP port", 1, 65535)
            host = self.host_var.get().strip()
            if not host or host == "0.0.0.0":
                raise TransportError("enter the IP address or host name of the device to poll")
            transport = TcpMasterTransport(host, port, timeout=timeout, on_frame=on_frame)
            target = f"{host}:{port}"
        master = ModbusMaster(transport, unit=unit, retries=retries,
                              on_event=self._queue_event)
        return master, target

    def _client_target(self) -> str:
        """Where a one-off request would go, worded for a confirmation prompt."""
        if self.master_link is not None:
            return self.master_link.describe()
        if self.is_rtu:
            return self.selected_serial_port() or "the serial port"
        host = self.host_var.get().strip()
        return f"{host}:{self.port_var.get().strip()}" if host else "the device"

    def _start_client(self) -> None:
        try:
            interval = max(0.05, float(self.interval_var.get()))
        except ValueError:
            raise TransportError("Poll interval must be a number") from None
        master, target = self._make_master()

        self.store.blank()
        self.master_link = master
        master.transport.open()                # fail now, not silently in the thread
        self.poller = Poller(master, self.store, interval=interval,
                             on_event=self._queue_event)
        self.poller.log_requests = self.log_requests_var.get()
        self.poller.start()
        self.state_label.config(text=f"polling {target}", style="Run.TLabel")
        self.reach_label.config(text=f"unit id {master.unit}, every {interval:g}s, "
                                     f"{len(self.poller.blocks)} request(s) per cycle")

    def _one_shot(self, what: str, action, on_error=None, alert: bool = False) -> None:
        """Run one request against the device, off the UI thread.

        When the poller is running its link is borrowed: a second connection
        to the same device is wrong on TCP and impossible on a serial bus, and
        ModbusMaster's lock keeps the two threads from overlapping on it.
        When nothing is polling, a link is opened for this one request and
        closed again - which is what lets a single read or write work without
        pressing Start at all.
        """
        if self.is_server or self.store is None:
            return
        borrowed = self.master_link
        if borrowed is not None:
            master = borrowed
        else:
            try:
                master, _target = self._make_master()
            except (TransportError, ValueError) as exc:
                messagebox.showerror("Cannot reach the device", str(exc), parent=self)
                return

        def work() -> None:
            peer = master.describe()
            try:
                if borrowed is None:
                    master.transport.open()
                action(master, peer)
            except (TransportError, ModbusError, IndexError, ValueError,
                    struct.error, OSError) as exc:
                reason = exc.describe() if isinstance(exc, ModbusError) else str(exc)
                self._queue_event("error", f"{what} failed: {reason}")
                if on_error is not None:
                    on_error(reason, peer)
                if alert:
                    self._alert_later(f"Could not {what}", reason)
            finally:
                if borrowed is None:
                    try:
                        master.close()
                    except Exception:          # nothing useful to do while tidying up
                        pass

        threading.Thread(target=work, name="modbus-one-shot", daemon=True).start()

    def _alert_later(self, title: str, message: str) -> None:
        """Raise a dialog from a worker thread, on the UI thread, if still open."""
        def show() -> None:
            if not self._closing:
                messagebox.showerror(title, message, parent=self)
        if not self._closing:
            self.after(0, show)

    def stop(self) -> None:
        if self.poller is not None:
            self.poller.stop()
            self.poller = None
        if self.master_link is not None:
            self.master_link.close()
            self.master_link = None
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.toggle_button.config(text="Start server" if self.is_server else "Start polling")
        self.state_label.config(text="stopped", style="Stop.TLabel")
        self.reach_label.config(text="")
        self._set_connection_state("normal")
        for item in self.client_tree.get_children():
            self.client_tree.delete(item)

    def _set_connection_state(self, state: str) -> None:
        """Lock the endpoint settings that cannot change under a live link."""
        enabled = state != "disabled"

        def walk(parent: tk.Misc) -> None:
            for child in parent.winfo_children():
                if isinstance(child, ttk.Combobox):
                    child.config(state="readonly" if enabled else "disabled")
                elif isinstance(child, (ttk.Entry, ttk.Button)):
                    child.config(state="normal" if enabled else "disabled")
                walk(child)

        walk(self.tcp_frame)
        walk(self.rtu_frame)
        if enabled:
            # these two accept anything typed, they are not pick-one lists
            self.host_box.config(state="normal")
            self.serial_box.config(state="normal")

    # ================================================================ register
    def _format_address(self, spec) -> str:
        """The Address cell, in whichever numbering the user asked for."""
        if self.conventional_var.get():
            return str(spec.conventional_address)
        return f"{spec.address} / 0x{spec.address:04X}"

    def on_address_style_change(self) -> None:
        """Redraw the table and relabel the column after the toggle."""
        conventional = self.conventional_var.get()
        self.tree.heading("address", text="4xxxx / 3xxxx" if conventional else "Address")
        self.tree.column("address", width=110 if conventional else 120)
        self._rebuild_register_rows()

    def _rebuild_register_rows(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows.clear()
        if self.store is None:
            return
        for reg in self.store.snapshot():
            self.tree.insert("", "end", iid=reg.name, values=self._row_values(reg))

    def _row_values(self, reg) -> tuple:
        spec = reg.spec
        now = time.monotonic()
        if not self.is_server:
            mode, every, due = "polled", "-", "-"
        elif spec.mode == "fixed" or reg.pinned:
            mode, every, due = spec.mode, "-", "-"
        else:
            mode = spec.mode
            every = f"{spec.interval:g}s"
            due = f"{reg.due_in(now):.0f}s"
        return (
            spec.table,
            self._format_address(spec),
            spec.label + ("  [held]" if reg.pinned and self.is_server else ""),
            spec.type,
            reg.format_value(),
            spec.unit,
            mode,
            every,
            due,
            reg.updates,
            reg.reads or "",
            time.strftime("%H:%M:%S", time.localtime(reg.last_read)) if reg.last_read else "",
        )

    def _row_tags(self, reg, now: float) -> tuple:
        if reg.error:
            return ("failed",)
        if reg.last_read and now - reg.last_read < RECENTLY_READ_SECONDS:
            return ("hot",)
        if reg.pinned and self.is_server:
            return ("pinned",)
        return ()

    def _refresh(self) -> None:
        try:
            self._flush_traffic()
            now = time.time()
            if self.store is not None:
                for reg in self.store.snapshot():
                    values = self._row_values(reg)
                    tags = self._row_tags(reg, now)
                    if self._rows.get(reg.name) != (values, tags):
                        self._rows[reg.name] = (values, tags)
                        self.tree.item(reg.name, values=values, tags=tags)
            self._refresh_peers()
            self._refresh_status()
        finally:
            if not self._closing:
                self._refresh_job = self.after(500, self._refresh)

    def _refresh_peers(self) -> None:
        rows: list[tuple] = []
        if self.server is not None and self.server.running:
            for info in self.server.clients():
                doing = info.last_operation or "-"
                if info.last_result:
                    doing = f"{doing}  ->  {info.last_result}"
                rows.append((
                    info.peer,
                    time.strftime("%H:%M:%S", time.localtime(info.connected_at)),
                    info.requests, info.exceptions,
                    time.strftime("%H:%M:%S", time.localtime(info.last_seen))
                    if info.requests else "-",
                    doing,
                ))
            hint = f"{len(rows)} client(s) connected" if rows else "no clients connected"
        elif self.poller is not None and self.poller.running:
            stats = self.master_link.stats.snapshot()
            rows.append((
                self.master_link.describe(),
                time.strftime("%H:%M:%S", time.localtime(stats["started_at"])),
                stats["requests"], stats["exceptions"] + stats["timeouts"],
                time.strftime("%H:%M:%S", time.localtime(stats["last_request"]))
                if stats["last_request"] else "-",
                self.poller.last_error or
                f"cycle {self.poller.cycles}, {self.poller.last_cycle_seconds * 1000:.0f} ms",
            ))
            hint = (f"polling unit {self.master_link.unit}, "
                    f"{len(self.poller.blocks)} request(s) per cycle")
        else:
            hint = "not running"

        existing = set(self.client_tree.get_children())
        seen = set()
        for row in rows:
            iid = str(row[0])
            seen.add(iid)
            if iid in existing:
                self.client_tree.item(iid, values=row)
            else:
                self.client_tree.insert("", "end", iid=iid, values=row)
        for item in existing - seen:
            self.client_tree.delete(item)
        self.client_hint.config(text=hint)

    def _refresh_status(self) -> None:
        device = self.profile.title if self.profile else "no device"
        if self.server is not None and self.server.running:
            stats = self.server.stats.snapshot()
            elapsed = time.monotonic() - self._last_rate_time
            if elapsed >= 1.0:
                self._rate = (stats["requests"] - self._last_request_count) / elapsed
                self._last_request_count = stats["requests"]
                self._last_rate_time = time.monotonic()
            last = (time.strftime("%H:%M:%S", time.localtime(stats["last_request"]))
                    if stats["last_request"] else "never")
            extra = ""
            if self.server.transport == "rtu":
                rtu = self.server.rtu_stats
                extra = f"  |  frames in/out {rtu.frames_in}/{rtu.frames_out}  |  " \
                        f"CRC errors {rtu.crc_errors}"
            self.status_var.set(
                f"SERVER {self.server.transport.upper()} {self.server.endpoint()}  |  "
                f"unit id {self.store.profile.unit_id}  |  clients {stats['active']} "
                f"(total {stats['connections']})  |  requests {stats['requests']} "
                f"({self._rate:.1f}/s)  |  writes {stats['writes']}  |  "
                f"errors {stats['exceptions']}  |  last {last}{extra}"
            )
        elif self.poller is not None and self.poller.running:
            stats = self.master_link.stats.snapshot()
            last = (time.strftime("%H:%M:%S", time.localtime(stats["last_request"]))
                    if stats["last_request"] else "never")
            self.status_var.set(
                f"CLIENT {self.link_var.get().upper()} {self.master_link.describe()}  |  "
                f"unit id {self.master_link.unit}  |  cycles {self.poller.cycles}  |  "
                f"requests {stats['requests']}  |  replies {stats['replies']}  |  "
                f"timeouts {stats['timeouts']}  |  exceptions {stats['exceptions']}  |  "
                f"cycle {self.poller.last_cycle_seconds * 1000:.0f} ms  |  last {last}"
            )
        else:
            role = "server" if self.is_server else "client"
            self.status_var.set(f"stopped  |  {role} / {self.link_var.get().upper()}  |  {device}")

    # ------------------------------------------------------- register actions
    # ---------------------------------------------------------------- context
    def _build_menubar(self) -> None:
        """The menu bar: profiles, the address style, and help.

        The buttons on the panels stay where they are - this is the second way
        to reach the same things, plus the two that have nowhere else to live:
        the profile editor and the help window.
        """
        root = self.winfo_toplevel()
        try:
            bar = tk.Menu(root, tearoff=0)
        except tk.TclError:                  # pragma: no cover - no window manager
            return

        profile_menu = tk.Menu(bar, tearoff=0)
        profile_menu.add_command(label="New profile...", command=self.new_profile)
        profile_menu.add_command(label="Edit profile...", command=self.edit_profile)
        profile_menu.add_separator()
        profile_menu.add_command(label="Rescan folder", command=self.rescan_devices)
        profile_menu.add_command(label="Reload this profile", command=self.reload_device)
        profile_menu.add_command(label="Open devices folder", command=self.open_devices_folder)
        bar.add_cascade(label="Profile", menu=profile_menu)

        view_menu = tk.Menu(bar, tearoff=0)
        view_menu.add_checkbutton(label="4xxxx / 3xxxx addresses",
                                  variable=self.conventional_var,
                                  command=self.on_address_style_change)
        bar.add_cascade(label="View", menu=view_menu)

        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label=f"{branding.APP_NAME} help", accelerator="F1",
                              command=lambda: show_help(self.winfo_toplevel()))
        help_menu.add_separator()
        help_menu.add_command(label=f"About {branding.APP_NAME}",
                              command=lambda: branding.show_about(self.winfo_toplevel()))
        bar.add_cascade(label="Help", menu=help_menu)

        root.configure(menu=bar)
        self.menubar = bar
        root.bind("<F1>", lambda _e: show_help(self.winfo_toplevel()))

    # ------------------------------------------------------------- profiles
    def edit_profile(self) -> None:
        """Open the editor on the profile that is loaded."""
        if self.profile is None:
            messagebox.showinfo("No profile", "Load a device profile first.", parent=self)
            return
        ProfileEditor(self.winfo_toplevel(), self.profile, self.devices_dir,
                      on_saved=self._profile_saved)

    def new_profile(self) -> None:
        """Open the editor on a blank device with one register to start from."""
        blank = parse_profile({
            "device": {"name": "New device", "unit_id": 1},
            "registers": [{"name": "first_register", "table": "holding", "address": "0x0000",
                           "type": "float32", "mode": "fixed", "start": 0}],
        }, "<new>")
        ProfileEditor(self.winfo_toplevel(), blank, self.devices_dir,
                      on_saved=self._profile_saved)

    def _profile_saved(self, path: str) -> None:
        """A profile was written: pick it up, and load it if it is the current one."""
        self.rescan_devices()
        current = self.profile.source_path if self.profile else ""
        if os.path.abspath(path) == os.path.abspath(current or ""):
            self.reload_device()
        else:
            self.log("info", f"saved profile {os.path.basename(path)}")

    def _build_register_menu(self) -> None:
        """The right click menu over the register table.

        Its entries are relabelled per role when it opens rather than rebuilt,
        so the menu object - and the accelerators on it - stay stable.
        """
        self.register_menu = tk.Menu(self, tearoff=0)
        # Entries are remembered by name. They used to be reconfigured by
        # position, which is a silent trap: get one number wrong and "Read
        # now" runs the write.
        self.menu_index: dict[str, int] = {}
        entries = (
            ("read", "Read now", self.read_selected),
            ("write", "Write to device...", self.edit_selected),
            (None, None, None),
            ("release", "Release", self.release_selected),
            (None, None, None),
            ("copy_address", "Copy address", lambda: self._copy_register("address")),
            ("copy_conventional", "Copy 4xxxx address",
             lambda: self._copy_register("conventional")),
            ("copy_value", "Copy value", lambda: self._copy_register("value")),
            ("copy_row", "Copy row", lambda: self._copy_register("row")),
        )
        position = 0
        for key, label, command in entries:
            if key is None:
                self.register_menu.add_separator()
            else:
                self.menu_index[key] = position
                self.register_menu.add_command(label=label, command=command)
            position += 1

    def _popup_register_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        reg = self.selected_register()
        if reg is None:
            return

        writable = reg.spec.table in ("holding", "coil")
        entry = self.register_menu.entryconfigure
        if self.is_server:
            # A server owns the values, so there is nothing to fetch: holding
            # one is the useful action, and releasing it undoes that.
            entry(self.menu_index["read"], label="Read now", state="disabled")
            entry(self.menu_index["write"], label="Hold at value...", state="normal")
            entry(self.menu_index["release"], label="Release",
                  state="normal" if reg.pinned else "disabled")
        else:
            # Both connect on their own when nothing is polling, so neither
            # depends on the poller any more. Only the table can rule one out.
            entry(self.menu_index["read"], label="Read now", state="normal")
            entry(self.menu_index["write"], label="Write to device...",
                  state="normal" if writable else "disabled")
            entry(self.menu_index["release"], label="Release", state="disabled")
        self.register_menu.tk_popup(event.x_root, event.y_root)
        self.register_menu.grab_release()

    def activate_selected(self) -> None:
        """What a double click does: the first thing the menu would offer.

        It used to always mean "write", so double clicking an input register
        on a client answered "Modbus does not allow a client to write" - a
        refusal to do the one thing that register can do. A server still owns
        its values, so there holding one is the useful action.
        """
        reg = self.selected_register()
        if reg is None:
            return
        if self.is_server or reg.spec.table in ("holding", "coil"):
            self.edit_selected()
        else:
            self.read_selected()

    def selected_register(self):
        """The Register object under the selection, or None."""
        selection = self.tree.selection()
        if not selection or self.store is None:
            return None
        return next((r for r in self.store.snapshot() if r.name == selection[0]), None)

    def _copy_register(self, what: str) -> None:
        reg = self.selected_register()
        if reg is None:
            return
        spec = reg.spec
        text = {
            "address": str(spec.address),
            "conventional": str(spec.conventional_address),
            "value": reg.format_value(),
            "row": f"{spec.label}\t{spec.table}\t{spec.address}\t"
                   f"{spec.conventional_address}\t{spec.type}\t"
                   f"{reg.format_value()}\t{spec.unit}",
        }[what]
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log("info", f"copied: {text}")

    def read_selected(self) -> None:
        """Read one register now, whether or not anything is being polled.

        Needing to press Start before you can look at a single register made
        no sense for the commonest job there is - check one value on a meter -
        so this connects on its own if nothing is connected already.
        """
        reg = self.selected_register()
        if reg is None or self.is_server or self.store is None:
            return
        spec, store = reg.spec, self.store
        block = Block(spec.table, spec.address,
                      1 if spec.table in BIT_TABLES else spec.word_count, [spec])

        def action(master, peer) -> None:
            data = master.read_block(block)
            if spec.table in BIT_TABLES:
                value = data[0]
            else:
                value = decode_words(spec, data[:spec.word_count],
                                     store.profile.word_order, store.profile.byte_order)
            store.apply_poll(spec.name, value, peer)
            self._queue_event("request", f"{peer} read {spec.label} -> {value}")

        def failed(reason: str, peer: str) -> None:
            store.apply_poll(spec.name, None, peer, error=reason)

        self._one_shot(f"read {spec.label}", action, failed)

    def edit_selected(self) -> None:
        selection = self.tree.selection()
        if not selection or self.store is None:
            return
        name = selection[0]
        reg = next((r for r in self.store.snapshot() if r.name == name), None)
        if reg is None:
            return
        unit = f", {reg.spec.unit}" if reg.spec.unit else ""
        if self.is_server:
            prompt = (f"{reg.spec.label} ({reg.spec.type}{unit})\n"
                      "The value stays fixed until you press Release.")
        else:
            if reg.spec.table not in ("holding", "coil"):
                messagebox.showinfo(
                    "Read only",
                    f"{reg.spec.label} is in the '{reg.spec.table}' table, which Modbus "
                    "does not allow a client to write.", parent=self)
                return
            prompt = (f"{reg.spec.label} ({reg.spec.type}{unit})\n"
                      f"This writes to the real device at {self._client_target()}.")

        answer = _ask_string(self, "Write register" if not self.is_server else "Hold value",
                             prompt, reg.format_value())
        if answer is None:
            return
        try:
            if reg.spec.table in BIT_TABLES:
                value = answer.strip().lower() in ("1", "true", "on", "yes")
            elif reg.spec.type == "string":
                value = answer
            else:
                value = float(answer)
        except ValueError:
            messagebox.showerror("Value", f"{answer!r} is not a number.", parent=self)
            return

        if self.is_server:
            self.store.set_value(name, value, pin=True)
            self.log("write", f"{reg.spec.label} is now held at {answer}")
            return
        spec, profile = reg.spec, self.store.profile

        def action(master, _peer) -> None:
            master.write_register_value(profile, spec, value)
            self._queue_event("write", f"wrote {answer} to {spec.label} "
                                       f"@ {spec.address} (0x{spec.address:04X})")

        # alert: a write that silently did not happen is worse than a noisy one
        self._one_shot(f"write {spec.label}", action, alert=True)

    def release_selected(self) -> None:
        selection = self.tree.selection()
        if selection and self.store is not None:
            self.store.set_pinned(selection[0], False)
            self.log("info", f"released '{selection[0]}' back to the simulation")

    def release_all(self) -> None:
        if self.store is None:
            return
        for reg in self.store.snapshot():
            if reg.pinned:
                self.store.set_pinned(reg.name, False)
        self.log("info", "released every held register")

    # ---------------------------------------------------------------- traffic
    def _queue_traffic(self, stamp: float, details: dict) -> None:
        if self.traffic_paused.get():
            return
        if len(self._traffic_pending) > MAX_TRAFFIC_ROWS:
            self._traffic_dropped += 1
            return
        address = details.get("address")
        count = details.get("count")
        function = details.get("function", 0)
        self._traffic_pending.append((
            f"{time.strftime('%H:%M:%S', time.localtime(stamp))}"
            f".{int(stamp * 1000) % 1000:03d}",
            details.get("peer", ""),
            f"FC{function:02X} {details.get('name', '')}",
            f"{address} / 0x{address:04X}" if address is not None else "-",
            count if count is not None else "",
            details.get("result", ""),
            "failed" if details.get("failed") else ("write" if function in (5, 6, 15, 16) else ""),
        ))

    def _flush_traffic(self) -> None:
        pending, self._traffic_pending = self._traffic_pending, []
        if not pending:
            return
        for row in pending[-MAX_TRAFFIC_INSERTS_PER_REFRESH:]:
            self._traffic_seq += 1
            self.traffic.insert("", 0, iid=f"t{self._traffic_seq}", values=row[:6],
                                tags=(row[6],) if row[6] else ())
        self._traffic_dropped += max(0, len(pending) - MAX_TRAFFIC_INSERTS_PER_REFRESH)
        children = self.traffic.get_children()
        if len(children) > MAX_TRAFFIC_ROWS:
            for item in children[MAX_TRAFFIC_ROWS:]:
                self.traffic.delete(item)
        self.traffic_hint.config(
            text="newest first" if not self._traffic_dropped else
            f"newest first - {self._traffic_dropped} row(s) dropped, "
            "the traffic is faster than the table")

    def clear_traffic(self) -> None:
        for item in self.traffic.get_children():
            self.traffic.delete(item)
        self._traffic_dropped = 0
        self.traffic_hint.config(text="newest first")

    # ---------------------------------------------------------------- logging
    def _queue_event(self, kind: str, message: str, **details) -> None:
        """Called from the server, poller and simulation threads.

        Never blocks: a full queue means the UI cannot keep up, and stalling a
        protocol thread to draw a log line would be the wrong trade. The oldest
        event is dropped instead, and the count is reported once the UI catches
        up so the gap is visible rather than silent.
        """
        try:
            self.events.put_nowait((kind, message, time.time(), details))
        except queue.Full:
            try:
                self.events.get_nowait()            # drop the oldest
                self.events.put_nowait((kind, message, time.time(), details))
            except (queue.Empty, queue.Full):       # drained by the UI meanwhile
                pass
            with self._events_lock:
                self._events_dropped += 1

    def _drain_events(self) -> None:
        try:
            for _ in range(MAX_EVENTS_PER_DRAIN):
                kind, message, stamp, details = self.events.get_nowait()
                if "function" in details:
                    self._queue_traffic(stamp, details)
                if not details.get("quiet"):
                    self._write_log(kind, message, stamp)
        except queue.Empty:
            pass
        finally:
            with self._events_lock:
                dropped, self._events_dropped = self._events_dropped, 0
            if dropped:
                self._write_log("warn", f"{dropped} log event(s) dropped: "
                                        "the link is busier than the window can draw",
                                time.time())
            if not self._closing:
                self._drain_job = self.after(120, self._drain_events)

    def log(self, kind: str, message: str) -> None:
        self._write_log(kind, message, time.time())

    def _write_log(self, kind: str, message: str, stamp: float) -> None:
        wanted = self.filter_var.get().strip().lower()
        if wanted and wanted not in message.lower():
            return
        line = (f"{time.strftime('%H:%M:%S', time.localtime(stamp))}"
                f".{int(stamp * 1000) % 1000:03d}  {message}\n")
        self.log_text.config(state="normal")
        self.log_text.insert("end", line, kind if kind in self.palette.log else "info")
        excess = int(self.log_text.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log_text.delete("1.0", f"{excess + 1}.0")
        self.log_text.config(state="disabled")
        if self.autoscroll_var.get():
            self.log_text.see("end")

    def clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            initialfile=f"modbus-{time.strftime('%Y%m%d-%H%M%S')}.log",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.log_text.get("1.0", "end"))
        self.log("info", f"log written to {path}")

    # ----------------------------------------------------------------- closing
    def on_close(self) -> None:
        # Cancel the repeating timers first. Destroying the window with one
        # still pending lets it fire against dead widgets, which surfaces as a
        # TclError traceback on an otherwise clean exit.
        self._closing = True
        for job in (self._drain_job, self._refresh_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._drain_job = self._refresh_job = None
        self.stop()
        if self.simulation is not None:
            self.simulation.stop()
        self.master.destroy()


def _ask_string(parent: tk.Misc, title: str, prompt: str, initial: str) -> str | None:
    """A small modal entry dialog (simpledialog draws badly on some themes)."""
    window = tk.Toplevel(parent)
    window.title(title)
    window.transient(parent.winfo_toplevel())
    window.resizable(False, False)
    result: dict[str, str | None] = {"value": None}

    ttk.Label(window, text=prompt, justify="left").pack(padx=12, pady=(12, 6), anchor="w")
    entry_var = tk.StringVar(value=initial)
    entry = ttk.Entry(window, textvariable=entry_var, width=36)
    entry.pack(padx=12, fill="x")
    entry.select_range(0, "end")
    entry.focus_set()

    def confirm(_event=None) -> None:
        result["value"] = entry_var.get()
        window.destroy()

    buttons = ttk.Frame(window)
    buttons.pack(padx=12, pady=12, anchor="e")
    ttk.Button(buttons, text="OK", width=10, command=confirm).pack(side="left")
    ttk.Button(buttons, text="Cancel", width=10, command=window.destroy).pack(side="left", padx=6)
    entry.bind("<Return>", confirm)
    window.bind("<Escape>", lambda _e: window.destroy())
    window.grab_set()
    parent.wait_window(window)
    return result["value"]


def tk_patchlevel(root: tk.Misc) -> str:
    """The Tcl/Tk this interpreter actually loaded, e.g. "8.6.13"."""
    try:
        return str(root.tk.call("info", "patchlevel"))
    except tk.TclError:
        return ""


def _refuse_old_tk(root: tk.Tk, problem: str) -> None:
    """Say why the window will not appear, instead of showing an empty one.

    Old Tk still opens a window and still sizes it - it just never draws
    anything into it, which reads as a hung program. A message the user can
    act on is worth more than a black rectangle, so the window is withdrawn
    before it is ever shown.
    """
    root.withdraw()
    print(problem, file=sys.stderr, flush=True)
    try:
        messagebox.showerror(f"{branding.APP_NAME} cannot open its window", problem)
    except tk.TclError:
        pass                                # a Tk this old may not manage even that
    try:
        root.destroy()
    except tk.TclError:
        pass


def run_ui(devices_dir: str, profile_path: str = "", host: str = "0.0.0.0", port: int = 502,
           role: str = "server", link: str = "tcp", serial_port: str = "",
           autostart: bool = False) -> int:
    root = tk.Tk()
    problem = compat.tk_version_problem(tk_patchlevel(root))
    if problem and not compat.ignore_tk_version():
        _refuse_old_tk(root, problem)
        return 3
    root.title(branding.window_title())
    branding.apply_window_icon(root)
    root.geometry("1260x820")
    root.minsize(1000, 640)
    ModbusToolApp(root, devices_dir, profile_path, host, port,
                 role=role, link=link, serial_port=serial_port, autostart=autostart)
    root.mainloop()
    return 0
