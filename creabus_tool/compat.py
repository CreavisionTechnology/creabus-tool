# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Small platform differences, kept in one place.

CreaBus Tool runs the same on Windows, Linux and macOS; these are the handful
of places where the three genuinely differ.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_FROZEN = getattr(sys, "frozen", False)


def resource_dir() -> str:
    """Where the files shipped with the app live.

    In a PyInstaller build that is the temporary unpack directory; running from
    a checkout it is the project root.
    """
    if IS_FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_dir() -> str:
    """The folder the user sees: next to the executable, or the project root."""
    if IS_FROZEN:
        executable = os.path.abspath(sys.executable)
        if IS_MACOS and ".app/Contents/MacOS/" in executable.replace("\\", "/"):
            # a .app bundle: put the editable files beside the bundle itself
            return os.path.dirname(executable.split(".app/Contents/MacOS/")[0] + ".app")
        return os.path.dirname(executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_devices_dir() -> str:
    """The devices folder, seeded from the bundled copy on first run.

    A frozen build carries the example profiles inside it, but the whole point
    of the profiles is that you can edit them, so they are copied out next to
    the executable the first time the app starts.
    """
    target = os.path.join(app_dir(), "devices")
    if os.path.isdir(target) and any(
        name.lower().endswith((".yaml", ".yml", ".json")) for name in os.listdir(target)
    ):
        return target
    bundled = os.path.join(resource_dir(), "devices")
    if os.path.isdir(bundled) and os.path.abspath(bundled) != os.path.abspath(target):
        os.makedirs(target, exist_ok=True)
        for name in os.listdir(bundled):
            source = os.path.join(bundled, name)
            if os.path.isfile(source) and not os.path.exists(os.path.join(target, name)):
                shutil.copy2(source, os.path.join(target, name))
    os.makedirs(target, exist_ok=True)
    return target


class _NullStream:
    """Stand-in for stdout/stderr when a windowed build has neither."""

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def enable_console_output(force: bool = False) -> None:
    """Make print() work in a windowed Windows build.

    PyInstaller's windowed mode leaves the process without a console, and
    sys.stdout as None, so anything that prints would crash. Started from a
    terminal we can attach to that terminal; started by double click there is
    nothing to attach to, and a console is only allocated when the user
    actually asked for console output (any command line argument).
    """
    if not IS_WINDOWS or not IS_FROZEN:
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    attached = False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        attached = bool(kernel32.AttachConsole(-1))     # -1 = the parent's console
        if not attached and force:
            attached = bool(kernel32.AllocConsole())
    except Exception:
        attached = False

    if attached:
        try:
            sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8",
                              errors="replace")
            sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8",
                              errors="replace")
            return
        except OSError:
            pass
    sys.stdout = sys.stdout or _NullStream()
    sys.stderr = sys.stderr or _NullStream()


# Tk 8.6 is from 2012 and is what every current Python ships with. The one
# place you still meet 8.5 is macOS: Apple's system Tcl/Tk framework is stuck
# at 8.5.9, and /usr/bin/python3 links against it. On macOS 10.15 and later it
# no longer draws ttk widgets - the window opens, sizes itself correctly, and
# stays empty - so the app looks hung rather than unsupported.
MIN_TK_VERSION = (8, 6)


def parse_tk_version(patchlevel: str) -> tuple:
    """(8, 5, 9) from "8.5.9", or () if the string is not a version at all."""
    parts: list[int] = []
    for piece in str(patchlevel).split("."):
        digits = ""
        for character in piece:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def tk_version_problem(patchlevel: str) -> str:
    """Why this Tcl/Tk cannot draw the window, or "" if it is usable.

    Returned as the whole message, ready to print and to put in a dialog: by
    the time anyone sees it they need the fix, not the diagnosis.
    """
    version = parse_tk_version(patchlevel)
    if not version or version >= MIN_TK_VERSION:
        return ""
    wanted = ".".join(str(number) for number in MIN_TK_VERSION)
    # Naming the interpreter matters more than it looks: the fix is to run a
    # different one, and "python3" on a Mac rarely means what its owner thinks.
    lines = [f"This Python is using Tcl/Tk {patchlevel}, and the window needs "
             f"Tk {wanted} or newer.",
             f"  interpreter: {sys.executable}"]
    if IS_MACOS:
        lines += [
            "",
            "Tk 8.5.9 is the framework Apple ships with macOS. It stopped "
            "drawing this kind of window at macOS 10.15, which is why you get "
            "an empty one - the app is running, you just cannot see it.",
            "",
            "Apple's Python cannot be repaired: no Tk you install changes which "
            "one it links against. You have to run a DIFFERENT Python, by name.",
            "",
            "  brew install python@3.13 python-tk@3.13",
            "  python3.13 main.py            <- the version, not plain 'python3'",
            "",
            "or install one from https://www.python.org/downloads/macos/ and run "
            "it as python3.13 (or whichever version you chose).",
            "",
            "Check before you start - this must not say 8.5:",
            "  python3.13 -c 'import tkinter; print(tkinter.TkVersion)'",
            "",
            "Or skip Python altogether and open creabus-tool.app from the "
            "release, which carries a working Tk inside it.",
        ]
    elif IS_LINUX:
        lines += ["", "Install a current Tk for this Python:",
                  "  sudo apt install python3-tk        Debian, Ubuntu",
                  "  sudo dnf install python3-tkinter   Fedora"]
    else:
        lines += ["", "Install a current Python from https://www.python.org/downloads/"]
    lines += ["",
              "Everything except the window works on old Tk: run with --headless, "
              "or set MODBUS_TOOL_IGNORE_TK_VERSION=1 to open it anyway."]
    return "\n".join(lines)


def ignore_tk_version() -> bool:
    """True if the user has asked to open the window on an old Tk regardless."""
    return os.environ.get("MODBUS_TOOL_IGNORE_TK_VERSION", "").strip().lower() in (
        "1", "true", "yes", "on")


def is_console_build() -> bool:
    """True in the frozen console binary, the one built as creabus-tool-cli.

    Windows is the only platform where a program has to choose between a
    window and a console when it is linked, so it is the only one that gets
    two executables. The console one is meant for a command prompt and prints
    its help rather than opening the window when it is given nothing to do.
    """
    if not IS_FROZEN:
        return False
    name = os.path.splitext(os.path.basename(sys.executable))[0]
    return name.endswith("-cli")


def open_folder(path: str) -> None:
    """Show a folder in Explorer / Finder / the desktop file manager."""
    if IS_WINDOWS:
        os.startfile(path)
    elif IS_MACOS:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def default_serial_port_hint() -> str:
    """A plausible example port name for this platform, used in help text."""
    if IS_WINDOWS:
        return "COM3"
    if IS_MACOS:
        return "/dev/tty.usbserial-0001"
    return "/dev/ttyUSB0"


def privileged_port_note(port: int) -> str:
    """Extra advice when binding a low port is likely to be refused."""
    if port < 1024 and not IS_WINDOWS:
        return ("Ports below 1024 need root on this platform - run with sudo, "
                "or use 5020 instead.")
    return "Port 502 is often already taken; try 5020."


def serial_permission_note() -> str:
    if IS_LINUX:
        return ("On Linux the serial port usually belongs to the 'dialout' group - "
                "run 'sudo usermod -a -G dialout $USER' and log back in.")
    if IS_MACOS:
        return "On macOS use the /dev/cu.* name rather than /dev/tty.* for a client."
    return ""


def mono_font(size: int = 9) -> tuple:
    """A genuinely fixed width font at the size asked for.

    ("TkFixedFont", 9) looks like it asks for the fixed font and does not.
    Tk reads the first element of a tuple as a family name, finds no family
    called "TkFixedFont" - it is the name of a font, not of a family - and
    falls back to the proportional default without complaining. The hex dumps
    in the log did not line up on Linux for exactly this reason.

    The bare name "TkFixedFont" does work, but then the size cannot be set
    without mutating the named font for the whole application. So ask the
    named font which family it actually is, and use that.
    """
    try:
        from tkinter import font as tkfont

        family = tkfont.nametofont("TkFixedFont").actual("family")
        if family:
            return (family, size)
    except Exception:                         # no Tk, or no root window yet
        pass
    return ("Courier", size)                  # X11 and Windows both have this


def ui_fonts() -> tuple[tuple, tuple, tuple]:
    """(body, mono, mono-bold) font tuples that exist on this platform."""
    if IS_WINDOWS:
        return ("Segoe UI", 9), ("Consolas", 9), ("Consolas", 9, "bold")
    if IS_MACOS:
        return (".AppleSystemUIFont", 12), ("Menlo", 11), ("Menlo", 11, "bold")
    mono = mono_font(9)
    return ("TkDefaultFont", 9), mono, (*mono, "bold")


def pick_theme(available: list[str]) -> str | None:
    """The best looking ttk theme for this platform, or None to keep the default."""
    for name in (["vista", "winnative"] if IS_WINDOWS else
                 ["aqua"] if IS_MACOS else
                 ["clam", "alt"]):
        if name in available:
            return name
    return None


def asset_path(name: str) -> str:
    """Full path to a file in ``assets/``, bundled or straight from a checkout."""
    return os.path.join(resource_dir(), "assets", name)
