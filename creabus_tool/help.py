# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""The Help window.

Kept as text in one place rather than scattered through tooltips, so it can be
read start to finish by someone meeting Modbus for the first time, and skimmed
by someone who just wants to know what a button does.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import branding, compat, theme

SECTIONS: list[tuple[str, str]] = [
    ("What this is", """
CreaBus Tool is two programs in one window.

As a SERVER it pretends to be a Modbus device, so you can develop and test a
client - a PLC, a SCADA package, your own code - with no hardware on the desk.
It answers on Modbus TCP or on Modbus RTU over a serial port.

As a CLIENT it polls a real device and shows you what is in it, decoded into
named values rather than raw registers.

Whichever role you pick, the device it imitates or decodes is described by a
profile file. Change the profile and the same program becomes a different
device. Nothing about any particular meter is built into the program.

What changed between versions is in CHANGELOG.md, next to the README.
"""),
    ("Getting started", """
1. Pick a device profile in the dropdown at the top. Seven devices ship with
   the tool, plus a commented template - see "Device profiles". The list
   re-reads the folder every time you open it, so a file you have just copied
   in is already there.
2. Choose the role: "Simulate a device" or "Poll a device".
3. Choose the link: Modbus TCP for a network, Modbus RTU for a serial cable.
4. Fill in the address. For a TCP server, 0.0.0.0 listens on every interface;
   port 502 is the standard, and 5020 is the usual alternative because 502
   needs administrator rights on Linux and macOS.
5. Press Start.

If you are polling a real device, the settings that matter most are the unit
id and, for RTU, the baud rate and parity. They must match the device exactly.

You do not have to press Start to look at one register. Right click it and
choose Read now, and the tool connects for that one request and disconnects
again. Start is for polling the whole map on a timer, which it does every 10
seconds unless you change it.
"""),
    ("The four views", """
REGISTERS shows every register in the profile with its live value, its address
and how often it has been read. A row turns green while it is being read, so
you can see which part of the map a client actually touches. Amber means the
value is being held; red means the last read failed.

TRAFFIC is one line per request: who asked, which function, which address, how
many registers, and what went back. Writes are amber, exceptions red.

DEBUG LOG is the same events as running text, with the registers decoded. Tick
"Log raw frames (hex)" to see the actual bytes, including the CRC on RTU.

CONNECTIONS shows who is connected and what they last asked for. On a serial
bus there is only ever one peer, shown as the bus master.

All four follow your desktop's light or dark setting, so the colours here may
not match a screenshot taken on the other one.
"""),
    ("Working with one register", """
Right click any row in the Registers table for the actions that apply to it.

Polling a device:
  Read now            fetch just this register, now
  Write to device...  write a value to it, if the table allows writing

Neither needs polling to be running. If nothing is connected, the tool opens
a connection for that one request and closes it again, so checking a single
value on a meter takes a right click and nothing else. If polling IS running
the same link is reused, because a second connection to one device is wrong
on TCP and impossible on a serial bus.

Simulating a device:
  Hold at value...    freeze it at a value you choose, ignoring the simulation
  Release             hand it back to the simulation

Either role:
  Copy address, copy the 4xxxx address, copy the value, or copy the whole row
  as a line you can paste into a spreadsheet.

Double clicking a row reads it, or - on a register a client may write, and on
anything at all when simulating - offers to set it.
"""),
    ("Addresses: 0 or 40001?", """
Modbus carries a zero based address on the wire. Device manuals almost always
print a different number for the same register: one based, with a digit in
front saying which table it lives in.

    coils             1, 2, 3 ...        protocol address 0, 1, 2 ...
    discrete inputs   10001, 10002 ...   protocol address 0, 1 ...
    input registers   30001, 30002 ...   protocol address 0, 1 ...
    holding registers 40001, 40002 ...   protocol address 0, 1 ...

So "40001" in a manual and "holding register 0" on the wire are the same
register. This is the single most common reason a value comes back from the
wrong place - being one out, or looking in the wrong table.

The five digit form only has four digits for the address, so it stops at
49999, which is protocol address 9998. Devices that put registers higher than
that - the SDM120 keeps its serial number at 0xFC00 - use the six digit form
instead, and this tool switches to it at the same point:

    holding 9998    ->  49999      the last five digit address
    holding 9999    ->  410000     six digits from here on
    holding 0xFC00  ->  464513     the SDM120 serial number

Tick "4xxxx addresses" under the Registers table to switch the Address column
between the two. Nothing else changes: it is a display choice.
"""),
    ("Data types and word order", """
A Modbus register is 16 bits. Anything bigger spans several of them, and there
is no agreement about which half comes first.

  float32, int32, uint32   two registers
  float64, int64, uint64   four registers
  int16, uint16            one register
  string                   one register per two characters

"32-bit word order" decides how those halves are put together. High word first
(ABCD) is what Eastron and most PLCs do. Low word first (CDAB) is common on
inverters. Get it wrong and floats come out as nonsense - often huge or tiny
numbers. Flip the setting and everything is re-decoded immediately, with no
restart, so trying both takes seconds.

"Byte order" does the same one level down, inside each register. It is rarely
anything other than normal.

"scale" in a profile handles devices that store, say, tenths of a volt in an
integer register: the raw value is multiplied by the scale.
"""),
    ("Device profiles", """
A profile is a YAML file in the devices folder that lists every register: its
name, table, address, type, unit, and - for a simulated device - how its value
should move over time.

What ships with the tool:

  sdm120, sdm630        Eastron energy meters, from their manuals
  sunspec-inverter      three phase PV inverter, SunSpec models 1 + 103
  sunspec-meter         three phase revenue meter, SunSpec models 1 + 203
  plc-io                PLC / remote I/O, exercises all four tables
  vfd-drive             motor drive: control word, reference, status
  climate-sensor        small RS-485 temperature / humidity / CO2 sensor

The SunSpec ones follow an open published specification rather than any one
vendor's map, so they are a fair first guess at an unknown inverter - check
the base address first, since a manual saying "40001" means protocol address
40000 on the wire. The last three are generic and say so in the file: no two
vendors number a drive the same way. None of them replaces your device's
manual. Copy the closest one and fix the addresses.

The dropdown lists every profile found, and re-reads the folder each time you
open it - so a file you have just copied in is there, and one you have edited
outside the window is reloaded, without pressing anything. Profile > Open
devices folder shows you where to copy files to.

Edit... opens the built-in editor on the loaded profile: add, duplicate,
reorder and delete registers, set every field, then Check to validate and
Save. Nothing is saved that would fail to load, so a profile that saves is a
profile that opens. New... starts from a blank device.

Editing the file in a text editor is still the fastest route for a big change.
devices/README.md documents every option, and devices/_template.yaml uses each
one at least once with comments.
"""),
    ("The profile editor", """
Edit... opens the loaded profile in an editor and New... starts from a blank
device. Both are buttons beside the dropdown, and both are in the Profile menu.

The device box at the top carries the settings that apply to the whole device:
name, unit id, default interval, word and byte order, and what an undefined
address does.

Below it, the register list is on the left and every field of the selected
register on the right.

  Add        append a register
  Duplicate  copy the selected one, with a fresh name and a free address
  Delete     remove it
  Up / Down  reorder the list. This is display order only - the address is
             what a client asks for, not the position in the file

  Check      validate without writing anything, and say what is wrong
  Save       write it back where it came from
  Save as... write a copy, and switch to it
  Close      discard, after asking if there is anything to discard

Nothing is saved that would fail to load: the check the loader performs runs
in memory first, so a profile that saves is a profile that opens. Overlapping
addresses, a duplicate name, a walk mode with no min or max - all refused here
rather than at load time.

The editor works on a copy. A server that is running keeps serving the profile
it started with until you save and it reloads, so you cannot break a running
simulation halfway through editing it.

For a large change a text editor is still faster. devices/README.md documents
every option and devices/_template.yaml uses each one at least once.
"""),
    ("Simulation modes", """
These only matter when simulating a device; a client just reads what is there.

  walk         random walk between min and max, moving at most step each time
  random       a fresh random value between min and max
  fixed        never changes on its own
  expression   computed from other registers, e.g. voltage * current
  accumulator  integrates a rate, for energy counters that only climb

An expression is checked against an allowlist of syntax before it runs - no
attribute access, no indexing, no imports, only arithmetic and the functions
above - so a profile from somebody else cannot run code on your machine. A
profile that steps outside it is refused when it loads.

Expressions are what make the dummy data believable rather than merely random.
In the SDM120 profile only voltage, current, power factor and frequency are
generated; the powers and energies are derived from them, so a client reading
the meter sees a physically consistent device.
"""),
    ("Troubleshooting", """
Nothing comes back, and it times out.
  Check the unit id first - it is the most common mismatch. On RTU also check
  baud rate and parity. Tick "Log raw frames (hex)" in the Debug log: if you
  see frames going out and nothing coming back, the device is not hearing you
  or is answering on a different address.

Floats look like nonsense.
  Wrong word order. Flip "32-bit word order" between ABCD and CDAB.

Every value is 10 or 100 times out.
  The device stores a scaled integer and the profile does not say so. A
  register holding tenths of a volt reads 2364 where you expect 236.4. Set
  "scale" on that register - 0.1 for tenths, 0.01 for hundredths. The
  climate-sensor profile is built entirely this way if you want to see it
  working. SunSpec devices do the same but keep the exponent in a separate
  _SF register nearby: read that first, and the real value is the raw one
  times ten to that power.

A negative reading comes back as about 65500.
  The register is signed and the profile says uint16. -42 stored in 16 bits
  is 65494 read as unsigned. Change the type to int16.

Exception 0x02, illegal data address.
  The device does not have that register. Check whether the manual's number is
  the 4xxxx style and yours is the protocol address, or the other way round.

Exception 0x0B, gateway target failed.
  You asked for a unit id this device does not answer on.

"Permission denied" opening a serial port on Linux.
  The port belongs to the dialout group: run
  sudo usermod -a -G dialout $USER, then log out and back in.

Binding port 502 is refused.
  Ports below 1024 need root on Linux and macOS. Use 5020 instead.

The serial port is missing from the list.
  Press Refresh. On Windows a port can only be open once, so close any other
  terminal program using it. On macOS prefer the /dev/cu.* name.

The window opens completely empty, on macOS, run from source.
  macOS still ships Tcl/Tk 8.5.9 as a system framework, and Apple's
  /usr/bin/python3 links against it. It opens the window and then draws
  nothing into it. Apple's Python cannot be repaired - installing a newer Tk
  does not change which one it links against - so you have to run a different
  Python by name:

    brew install python@3.13 python-tk@3.13
    python3.13 main.py

  Plain "python3" will still be Apple's. Or open creabus-tool.app from the
  release, which carries a working Tk inside it. "--version" prints the
  interpreter and the Tk in use and says if the Tk is too old.
"""),
    ("Command line", """
Everything the window does can be done without it, which is what you want on a
server or in a script:

  creabus-tool --headless --port 5020
  creabus-tool --role client --host 192.168.1.50 --port 502
  creabus-tool --link rtu --serial-port /dev/ttyUSB0 --baud 9600 --headless
  creabus-tool --list       device profiles
  creabus-tool --ports      serial ports on this machine
  creabus-tool --help       every option

On Windows use creabus-tool-cli.exe for these. Windows makes a program choose
between having a console and having a window when it is built, so the same
program ships twice: creabus-tool.exe is the window, creabus-tool-cli.exe is the
one that can print, be piped and take Ctrl+C. Run the -cli one with no
arguments and it prints its help; add --ui to get the window from it.

On macOS the release carries creabus-tool.app to double click and a plain
creabus-tool binary to type in a Terminal. They are the same program.

  creabus-tool --version    version, and the Python and Tk it is running on
"""),
]


def blocks(body: str) -> list[tuple[str, bool]]:
    """Split a section into paragraphs, each flagged preformatted or prose.

    The text here is wrapped to 78 columns so it reads well in the source. Tk
    wraps on word boundaries too, and the two together give a ragged mess - so
    prose is joined back into single lines and handed over whole to be wrapped
    once. Blocks whose lines are indented are tables or command listings where
    the line breaks and the column positions are the point: those are passed
    through untouched, and the flag tells the window to set them in a
    monospaced font, without which the columns do not line up at all.
    """
    out: list[tuple[str, bool]] = []
    for block in body.strip("\n").split("\n\n"):
        lines = block.split("\n")
        if any(line.startswith(("  ", "\t")) for line in lines if line.strip()):
            out.append((block, True))
        else:
            out.append((" ".join(line.strip() for line in lines if line.strip()), False))
    return out


def reflow(body: str) -> str:
    """The whole section as one string, laid out blocks left as written."""
    return "\n\n".join(text for text, _preformatted in blocks(body))


def show_help(parent) -> None:
    """Open the Help window, or raise it if it is already open."""
    existing = getattr(parent, "_help_window", None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_set()
        return

    window = tk.Toplevel(parent)
    parent._help_window = window            # noqa: SLF001 - one per parent window
    window.title(f"{branding.APP_NAME} help")
    window.geometry("820x620")
    window.minsize(560, 400)
    branding.apply_window_icon(window)

    outer = ttk.Frame(window, padding=8)
    outer.pack(fill="both", expand=True)

    panes = ttk.PanedWindow(outer, orient="horizontal")
    panes.pack(fill="both", expand=True)

    # Listbox and Text are classic Tk widgets: they stay light whatever the
    # desktop is doing unless they are told otherwise.
    palette = theme.palette_for(window)
    index = tk.Listbox(panes, exportselection=False, activestyle="none", width=26,
                       background=palette.surface, foreground=palette.ink,
                       selectbackground=palette.select, selectforeground=palette.ink,
                       highlightthickness=0, borderwidth=0)
    panes.add(index, weight=0)

    right = ttk.Frame(panes)
    panes.add(right, weight=1)
    text = tk.Text(right, wrap="word", padx=12, pady=10, relief="flat",
                   font=("TkDefaultFont", 10),
                   background=palette.surface, foreground=palette.ink,
                   insertbackground=palette.cursor, selectbackground=palette.select,
                   highlightthickness=0)
    scroll = ttk.Scrollbar(right, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    _body_font, mono_font, _mono_bold = compat.ui_fonts()
    text.tag_configure("title", font=("TkDefaultFont", 13, "bold"),
                       spacing1=4, spacing3=10)
    text.tag_configure("body", spacing3=4)
    # Tables and command listings are aligned with spaces, which a proportional
    # font throws away. This is the whole reason blocks() flags them.
    text.tag_configure("pre", font=mono_font, spacing3=4, lmargin1=8, lmargin2=8)

    for title, _ in SECTIONS:
        index.insert("end", title)

    def show(position: int) -> None:
        title, body = SECTIONS[position]
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("end", title + "\n", "title")
        for chunk, preformatted in blocks(body):
            text.insert("end", chunk + "\n\n", "pre" if preformatted else "body")
        text.configure(state="disabled")
        text.yview_moveto(0)

    def on_pick(_event=None) -> None:
        selection = index.curselection()
        if selection:
            show(selection[0])

    index.bind("<<ListboxSelect>>", on_pick)
    index.selection_set(0)
    show(0)

    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(8, 0))
    ttk.Label(bar, text=f"{branding.APP_NAME} {branding.VERSION}  -  {branding.VENDOR}",
              foreground=palette.muted).pack(side="left")
    ttk.Button(bar, text="Close", command=window.destroy).pack(side="right")

    window.bind("<Escape>", lambda _e: window.destroy())
    window.focus_set()
