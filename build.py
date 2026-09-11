# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Build standalone executables for whichever platform you run this on.

    python build.py                 # everything this platform needs
    python build.py --clean         # throw away previous output first
    python build.py --gui-only      # just the double click app
    python build.py --cli-only      # just the command line binary
    python build.py --dir           # a folder instead of one file (starts faster)

What you get:

    Windows   creabus-tool.exe      the window, no console flash on double click
              creabus-tool-cli.exe  console build for --headless and scripting,
                                   where Ctrl+C and output redirection work
    macOS     creabus-tool.app      the double click bundle
              creabus-tool          the same thing as a terminal binary
    Linux     creabus-tool          one binary, window or --headless

    everywhere  dist/HOW-TO-RUN.txt  which of those files to open, in words,
                                     to travel inside the release archive

PyInstaller cannot cross compile: run this on Windows for a .exe, on macOS for
a .app, on Linux for an ELF binary. .github/workflows/build.yml does all three
on every push if you would rather let CI do it.

The device profiles are baked into the executable and copied out next to it the
first time it runs, so they stay editable.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from creabus_tool import branding                                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "creabus-tool"
BUNDLE_ID = "com.creavisiontechnology.modbustool"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        pass
    print("PyInstaller is not installed.")
    if not sys.stdin.isatty():
        print("run 'pip install pyinstaller' (or 'pip install -r requirements-dev.txt')",
              file=sys.stderr)
        return False
    answer = input("install it now with pip? [Y/n] ").strip().lower()
    if answer not in ("", "y", "yes"):
        print("run 'pip install pyinstaller' and try again")
        return False
    return subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"],
                          check=False).returncode == 0


def check_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        print("error: this Python has no tkinter, so the built app would have no UI.",
              file=sys.stderr)
        if sys.platform.startswith("linux"):
            print("       Debian/Ubuntu: sudo apt install python3-tk", file=sys.stderr)
            print("       Fedora:        sudo dnf install python3-tkinter", file=sys.stderr)
        elif IS_MACOS:
            print("       macOS: brew install python-tk", file=sys.stderr)
        return False


def check_pyserial() -> None:
    try:
        import serial  # noqa: F401
    except ImportError:
        print("warning: pyserial is not installed, so the build would have no Modbus RTU.")
        print("         run 'pip install -r requirements.txt' first.")


def default_icon() -> str:
    """The icon PyInstaller wants on this platform, if it is in the checkout.

    Windows needs .ico and macOS needs .icns. Linux window managers take the
    icon from the running app instead, which the UI sets from the PNGs.
    """
    name = "creabus-tool.ico" if IS_WINDOWS else "creabus-tool.icns" if IS_MACOS else ""
    if not name:
        return ""
    path = os.path.join(HERE, "assets", name)
    return path if os.path.exists(path) else ""


def pyinstaller_command(name: str, windowed: bool, one_dir: bool, icon: str) -> list[str]:
    separator = ";" if IS_WINDOWS else ":"
    command = [
        sys.executable, "-m", "PyInstaller",
        os.path.join(HERE, "main.py"),
        "--name", name,
        "--onedir" if one_dir else "--onefile",
        "--windowed" if windowed else "--console",
        "--add-data", f"{os.path.join(HERE, 'devices')}{separator}devices",
        "--add-data", f"{os.path.join(HERE, 'assets')}{separator}assets",
        # pyserial picks its backend at import time, by platform
        "--hidden-import", "serial.tools.list_ports",
        "--hidden-import", "serial.serialutil",
        "--collect-submodules", "serial",
        # nothing here needs these, and they are big
        "--exclude-module", "numpy",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PIL",
        "--exclude-module", "pandas",
        "--exclude-module", "pytest",
        "--noconfirm",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build", "pyinstaller", name),
        "--specpath", os.path.join(HERE, "build"),
    ]
    if icon:
        command += ["--icon", icon]
    if IS_MACOS and windowed:
        command += ["--osx-bundle-identifier", BUNDLE_ID]
    return command


def _entry(label: str, body: list[str], width: int = 24) -> list[str]:
    """One "file - what it is" block, with the prose lined up under itself."""
    pad = " " * (width + 2)
    return ([f"  {label.ljust(width)}{body[0]}".rstrip()]
            + [(pad + line).rstrip() for line in body[1:]])


def how_to_run(name: str) -> str:
    """The "which file do I open?" note that ships inside every archive.

    Every platform hands you more than one thing - a .app and a binary, or two
    .exe files - and none of them says which is which. Downloading a zip and
    guessing is where people give up, so the answer travels with the files.
    """
    lines = [f"{branding.APP_NAME} {branding.VERSION} - which file do I open?", ""]
    if IS_WINDOWS:
        lines += _entry(f"{name}.exe", [
            "Double click this. It is the window."])
        lines += [""]
        lines += _entry(f"{name}-cli.exe", [
            "The same program, for a command prompt. Windows makes a",
            "program choose between having a console and having a",
            "window when it is built, so it ships twice; this is the",
            "copy that can print, be piped and take Ctrl+C.",
            "",
            f"  {name}-cli.exe --help",
            f"  {name}-cli.exe --list",
            f"  {name}-cli.exe --headless --port 5020",
            "",
            "With no arguments it prints its help. Add --ui if you",
            "want the window from this one."])
    elif IS_MACOS:
        lines += _entry(f"{name}.app", [
            "Double click this. It is the window."])
        lines += [""]
        lines += _entry(name, [
            "The same program, for a Terminal. macOS needs a bundle",
            "to double click and a plain binary to type, so both are",
            "here; every option is the same in either.",
            "",
            f"  ./{name} --help",
            f"  ./{name} --list",
            f"  ./{name} --headless --port 5020"])
    else:
        lines += _entry(f"./{name}", [
            "The whole program. With no arguments it opens the",
            "window; --headless runs it without one.",
            "",
            f"  ./{name} --help",
            f"  ./{name} --list",
            f"  ./{name} --headless --port 5020"])
    lines += [""]
    lines += _entry("devices/", [
        "The device profiles, in plain YAML. Edit them, add your",
        "own, or use Profile > Edit in the window."])
    if IS_MACOS:
        lines += ["",
                  "macOS quarantines anything unsigned that arrived over the internet.",
                  "Clear it once, in the folder you unzipped:",
                  "",
                  "  xattr -dr com.apple.quarantine ."]
    lines += [
        "",
        "The window needs Tk 8.6 or newer, which these builds carry inside them.",
        "Running from source on a Python with an older Tk - macOS system python3",
        "is the one that still has 8.5 - opens a window that never draws.",
        "",
        f"{branding.APP_NAME} {branding.VERSION} - {branding.LICENCE} licensed, "
        "free for any use.",
        f"Made by {branding.VENDOR}: {branding.VENDOR_URL}",
        f"Documentation and source: {branding.PROJECT_URL}",
    ]
    return "\n".join(lines) + "\n"


def describe(path: str) -> str:
    if os.path.isdir(path):
        total = sum(os.path.getsize(os.path.join(root, entry))
                    for root, _dirs, files in os.walk(path) for entry in files)
    else:
        total = os.path.getsize(path)
    return f"{path}  ({total / 1024 / 1024:.1f} MB)"


def find_artifacts(name: str, one_dir: bool) -> list[str]:
    dist = os.path.join(HERE, "dist")
    candidates = []
    if IS_MACOS:
        candidates.append(os.path.join(dist, f"{name}.app"))
    suffix = ".exe" if IS_WINDOWS else ""
    candidates.append(os.path.join(dist, name, f"{name}{suffix}") if one_dir
                      else os.path.join(dist, f"{name}{suffix}"))
    return [path for path in candidates if os.path.exists(path)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gui-only", action="store_true", help="skip the console binary")
    parser.add_argument("--cli-only", action="store_true", help="skip the windowed app")
    parser.add_argument("--dir", dest="one_dir", action="store_true",
                        help="build a folder instead of a single file")
    parser.add_argument("--clean", action="store_true",
                        help="delete build/ and dist/ before building")
    parser.add_argument("--name", default=APP_NAME, help=f"base name (default {APP_NAME})")
    parser.add_argument("--icon", default="",
                        help="path to an .ico (Windows) or .icns (macOS) icon "
                             "(default: the one in assets/)")
    args = parser.parse_args()

    if sys.version_info < (3, 9):
        return fail(f"Python 3.9 or newer is needed, this is {platform.python_version()}")
    if not args.cli_only and not check_tkinter():
        return 1
    check_pyserial()
    if not ensure_pyinstaller():
        return 1

    if args.clean:
        for folder in ("build", "dist"):
            path = os.path.join(HERE, folder)
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"removed {path}")

    # Windows is the only platform where the console and the window are
    # different subsystems, so it is the only one that needs two binaries.
    targets: list[tuple[str, bool]] = []
    if not args.cli_only:
        targets.append((args.name, True))
    if IS_WINDOWS and not args.gui_only:
        targets.append((f"{args.name}-cli", False))
    if args.cli_only and not IS_WINDOWS:
        targets = [(args.name, False)]
    if not targets:
        return fail("--gui-only and --cli-only cannot both be given")

    print(f"building for {platform.system()} {platform.machine()} "
          f"with Python {platform.python_version()}")

    built: list[str] = []
    for name, windowed in targets:
        print(f"\n--- {name} ({'windowed' if windowed else 'console'}) "
              + "-" * 30)
        command = pyinstaller_command(name, windowed, args.one_dir,
                                      args.icon or default_icon())
        result = subprocess.run(command, cwd=HERE, check=False)
        if result.returncode != 0:
            return fail(f"PyInstaller exited with {result.returncode} while building {name}")
        found = find_artifacts(name, args.one_dir)
        if not found:
            return fail(f"PyInstaller reported success but no {name} artifact is in dist/")
        built.extend(found)

    note = os.path.join(HERE, "dist", "HOW-TO-RUN.txt")
    with open(note, "w", encoding="utf-8") as handle:
        handle.write(how_to_run(args.name))

    print("\nbuilt:")
    for path in built:
        print("  " + describe(path))
    print("  " + describe(note))

    print("\nThe device profiles are inside the executable and are copied to a "
          "'devices' folder\nnext to it the first time it runs, so they stay editable.")
    print(f"Ship {os.path.basename(note)} alongside them: it says which file to open.")
    if IS_WINDOWS:
        print(f"\nUse {args.name}.exe for the window and {args.name}-cli.exe for "
              "--headless:\na windowed Windows build has no console, so Ctrl+C and "
              "output redirection\nonly work with the -cli binary. Run it with no "
              "arguments and it prints its help.")
    if IS_MACOS:
        print("\nmacOS Gatekeeper quarantines an unsigned app that arrived over the "
              "internet.\nWhoever opens it can clear that once with:")
        print(f"  xattr -dr com.apple.quarantine /path/to/{args.name}.app")
    if sys.platform.startswith("linux"):
        print("\nThe binary is glibc dependent: build on the oldest distribution you "
              "intend to support.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
