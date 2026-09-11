# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""CreaBus Tool - entry point.

Server, pretending to be a device:
    python main.py                                   # UI, SDM120 over TCP
    python main.py --headless --port 5020            # no UI
    python main.py --link rtu --serial-port COM3 --baud 9600 --headless

Client, polling a real device:
    python main.py --role client --host 192.168.1.50 --port 502
    python main.py --role client --link rtu --serial-port /dev/ttyUSB0 --headless

Other:
    python main.py --list                            # available device profiles
    python main.py --ports                           # available serial ports
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import signal
import sys
import time

from creabus_tool import branding, compat
from creabus_tool.client import ModbusMaster, Poller, TcpMasterTransport, plan_blocks
from creabus_tool.datastore import DataStore
from creabus_tool.pdu import TransportError
from creabus_tool.profile import ProfileError, discover_profiles, load_profile
from creabus_tool.rtu import RtuMasterTransport, SerialSettings, list_serial_ports
from creabus_tool.server import RtuServer, SimulationRunner, TcpServer, local_addresses


def _usage_examples() -> str:
    """The examples at the top of this file, addressed to however you started it.

    "python main.py --list" is the right thing to print from a checkout and the
    wrong thing to print from a downloaded binary, where there is no main.py.
    """
    if not compat.IS_FROZEN:
        return __doc__ or ""
    invocation = os.path.basename(sys.executable)
    if not compat.IS_WINDOWS:
        invocation = f"./{invocation}"
    text = (__doc__ or "").replace("python main.py", invocation)
    if compat.is_console_build():
        # this build prints its help when it is given nothing to do, so the
        # bare invocation is not the one that opens the window
        text = re.sub(rf"^(\s*){re.escape(invocation)}(\s+#)",
                      rf"\g<1>{invocation} --ui\g<2>", text, count=1, flags=re.M)
    return _align_comments(text)


def _align_comments(text: str) -> str:
    """Put the trailing # comments back in one column after a substitution.

    The examples are written flush in the docstring; swapping "python main.py"
    for a longer executable name pushes some of them out and leaves the block
    looking untended.
    """
    lines = text.split("\n")
    commented = [index for index, line in enumerate(lines) if "  #" in line]
    if not commented:
        return text
    width = max(len(lines[index].split("#", 1)[0].rstrip()) for index in commented) + 2
    for index in commented:
        head, comment = lines[index].split("#", 1)
        lines[index] = f"{head.rstrip().ljust(width)}#{comment}"
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Simulate a Modbus device, or poll a real one, over TCP or RTU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"{_usage_examples()}\n"
               f"{branding.APP_NAME} {branding.VERSION} - "
               f"{branding.LICENCE} licensed, free for any use.\n"
               f"Made by {branding.VENDOR}: {branding.VENDOR_URL}\n",
    )
    role = parser.add_argument_group("role and link")
    role.add_argument("-r", "--role", choices=("server", "client"), default="server",
                      help="server = pretend to be a device, client = poll one "
                           "(default: server)")
    role.add_argument("-l", "--link", choices=("tcp", "rtu"), default="tcp",
                      help="tcp = Modbus TCP, rtu = Modbus RTU on a serial port "
                           "(default: tcp)")

    net = parser.add_argument_group("Modbus TCP")
    net.add_argument("-H", "--host", default="",
                     help="server: address to bind (default 0.0.0.0, every interface). "
                          "client: the device to poll")
    net.add_argument("-p", "--port", type=int, default=502, help="TCP port (default: 502)")
    net.add_argument("--max-clients", type=int, default=16,
                     help="server: refuse connections beyond this many (0 = unlimited)")

    ser = parser.add_argument_group("Modbus RTU")
    ser.add_argument("-s", "--serial-port", default="",
                     help=f"serial port, e.g. {compat.default_serial_port_hint()}")
    ser.add_argument("-b", "--baud", type=int, default=9600, help="baud rate (default: 9600)")
    ser.add_argument("--parity", choices=("none", "even", "odd"), default="none")
    ser.add_argument("--databits", type=int, choices=(7, 8), default=8)
    ser.add_argument("--stopbits", type=float, choices=(1, 2), default=1)

    mod = parser.add_argument_group("Modbus settings")
    mod.add_argument("-u", "--unit", type=int, default=None,
                     help="Modbus address (unit id), overrides the profile")
    mod.add_argument("--any-unit", action="store_true",
                     help="server: answer whatever unit id the client asks for")
    mod.add_argument("--word-order", choices=("big", "little"), default=None,
                     help="32 bit word order: big = high word first (ABCD), "
                          "little = low word first (CDAB)")
    mod.add_argument("--byte-order", choices=("big", "little"), default=None,
                     help="byte order inside each 16 bit word")
    mod.add_argument("--gap-policy", choices=("zero", "exception"), default=None,
                     help="server: what undefined addresses do")
    mod.add_argument("--delay", type=float, default=0.0,
                     help="server: response delay in ms, to simulate a slow device")
    mod.add_argument("--poll-interval", type=float, default=10.0,
                     help="client: seconds between poll cycles (default: 10)")
    mod.add_argument("--timeout", type=float, default=1000.0,
                     help="client: reply timeout in ms (default: 1000)")
    mod.add_argument("--retries", type=int, default=1,
                     help="client: retries per request (default: 1)")

    other = parser.add_argument_group("device profile and output")
    other.add_argument("-d", "--device", default="",
                       help="device profile to load (default: devices/sdm120.yaml)")
    other.add_argument("--devices-dir", default="",
                       help="folder scanned for device profiles")
    other.add_argument("--headless", action="store_true",
                       help="run without the UI and log to stdout")
    other.add_argument("--ui", action="store_true",
                       help="open the window (what the command line build needs "
                            "to be told, since it defaults to printing this help)")
    other.add_argument("--start", action="store_true",
                       help="UI mode: start straight away")
    other.add_argument("--log-frames", action="store_true",
                       help="log every raw Modbus frame as hex")
    other.add_argument("--quiet", action="store_true",
                       help="do not log individual requests")
    other.add_argument("--list", action="store_true",
                       help="list the available device profiles and exit")
    other.add_argument("--ports", action="store_true",
                       help="list the serial ports on this machine and exit")
    other.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def resolve_profile(devices_dir: str, wanted: str) -> str:
    if wanted:
        return wanted
    found = discover_profiles(devices_dir)
    for path in found:
        if "sdm120" in os.path.basename(path).lower():
            return path
    if not found:
        raise ProfileError(f"no device profiles found in {devices_dir}")
    return found[0]


def apply_overrides(profile, args: argparse.Namespace) -> None:
    if args.unit is not None:
        profile.unit_id = args.unit
    if args.any_unit:
        profile.accept_any_unit_id = True
    if args.word_order:
        profile.word_order = args.word_order
    if args.byte_order:
        profile.byte_order = args.byte_order
    if args.gap_policy:
        profile.gap_policy = args.gap_policy


def serial_settings(args: argparse.Namespace) -> SerialSettings:
    if not args.serial_port:
        ports = list_serial_ports()
        listing = ", ".join(device for device, _ in ports) or "none found"
        raise TransportError(
            f"--serial-port is required for an RTU link (available: {listing})")
    return SerialSettings(port=args.serial_port, baudrate=args.baud, parity=args.parity,
                          bytesize=args.databits, stopbits=args.stopbits)


def make_logger(args: argparse.Namespace):
    def on_event(kind: str, message: str, **details) -> None:
        if details.get("quiet"):
            return
        print(f"{time.strftime('%H:%M:%S')}  {kind:<8} {message}", flush=True)
    return on_event


def wait_for_interrupt() -> None:
    stop = {"now": False}

    def shutdown(_signum, _frame) -> None:
        stop["now"] = True

    signal.signal(signal.SIGINT, shutdown)
    # SIGTERM everywhere, SIGBREAK (Ctrl+Break) additionally on Windows
    for name in ("SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, shutdown)
            except (OSError, ValueError):
                pass
    print("Ctrl+C to stop.", flush=True)
    try:
        while not stop["now"]:
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass


def run_server_headless(args: argparse.Namespace, profile_path: str) -> int:
    profile = load_profile(profile_path)
    apply_overrides(profile, args)
    on_event = make_logger(args)

    store = DataStore(profile, on_event=on_event)
    simulation = SimulationRunner(store)
    simulation.start()

    print("role     : server (simulating a device)")
    print(f"device   : {profile.title}")
    print(f"profile  : {profile.source_path}")
    print(f"registers: {len(profile.registers)}  unit id: {profile.unit_id}"
          f"{' (answers any)' if profile.accept_any_unit_id else ''}")
    print(f"framing  : {profile.word_order} word order, {profile.byte_order} byte order, "
          f"undefined addresses {profile.gap_policy}")

    try:
        if args.link == "rtu":
            settings = serial_settings(args)
            server = RtuServer(store, settings, on_event=on_event,
                               log_requests=not args.quiet, log_frames=args.log_frames,
                               response_delay=max(0.0, args.delay) / 1000.0)
            print(f"link     : Modbus RTU on {settings.describe()}")
        else:
            host = args.host or "0.0.0.0"
            server = TcpServer(store, host=host, port=args.port, on_event=on_event,
                               log_requests=not args.quiet, log_frames=args.log_frames,
                               response_delay=max(0.0, args.delay) / 1000.0,
                               max_clients=max(0, args.max_clients))
            print(f"link     : Modbus TCP on {host}:{args.port}")
            if host in ("0.0.0.0", ""):
                print("reachable: "
                      + ", ".join(a for a in local_addresses() if a != "0.0.0.0")
                      + f" on port {args.port}")
        server.start()
    except (TransportError, OSError) as exc:
        print(f"error    : {exc}", file=sys.stderr)
        if args.link == "rtu":
            note = compat.serial_permission_note()
        else:
            note = compat.privileged_port_note(args.port)
        if note:
            print(f"           {note}", file=sys.stderr)
        simulation.stop()
        return 1

    try:
        wait_for_interrupt()
    finally:
        server.stop()
        simulation.stop()
    return 0


def run_client_headless(args: argparse.Namespace, profile_path: str) -> int:
    profile = load_profile(profile_path)
    apply_overrides(profile, args)
    on_event = make_logger(args)

    store = DataStore(profile)
    store.blank()
    timeout = max(0.05, args.timeout / 1000.0)

    def on_frame(direction: str, data: bytes) -> None:
        if args.log_frames:
            print(f"{time.strftime('%H:%M:%S')}  frame    "
                  f"{direction.upper()} {data.hex(' ')}", flush=True)

    try:
        if args.link == "rtu":
            settings = serial_settings(args)
            transport = RtuMasterTransport(settings, timeout=timeout, on_frame=on_frame)
            target = settings.describe()
        else:
            host = args.host or "127.0.0.1"
            transport = TcpMasterTransport(host, args.port, timeout=timeout, on_frame=on_frame)
            target = f"{host}:{args.port}"
    except TransportError as exc:
        print(f"error    : {exc}", file=sys.stderr)
        return 1

    blocks = plan_blocks(profile)
    print("role     : client (polling a real device)")
    print(f"target   : {target}  unit id {profile.unit_id}")
    print(f"profile  : {profile.source_path}")
    print(f"plan     : {len(profile.registers)} registers in {len(blocks)} request(s) "
          f"every {args.poll_interval:g}s")

    master = ModbusMaster(transport, unit=profile.unit_id,
                          retries=max(0, args.retries), on_event=on_event)
    try:
        transport.open()
    except TransportError as exc:
        print(f"error    : {exc}", file=sys.stderr)
        note = (compat.serial_permission_note() if args.link == "rtu" else "")
        if note:
            print(f"           {note}", file=sys.stderr)
        return 1

    poller = Poller(master, store, interval=args.poll_interval, on_event=on_event)
    poller.log_requests = not args.quiet
    poller.start()
    try:
        wait_for_interrupt()
    finally:
        poller.stop()

    print("\nlast values read:")
    for reg in store.snapshot():
        flag = f"  ! {reg.error}" if reg.error else ""
        print(f"  {reg.spec.label:<34}{reg.format_value():>16} {reg.spec.unit}{flag}")
    return 0


def tk_report() -> str:
    """"Tk 8.6" for the version banner, or why there is no window at all.

    Read from the compiled-in version rather than by opening a window: this
    has to answer on a build server and over ssh, where there is no display
    to open one on.
    """
    try:
        import tkinter
    except ImportError:
        return "no Tk (the window needs tkinter; --headless does not)"
    version = str(tkinter.TkVersion)
    if compat.tk_version_problem(version):
        return f"Tk {version}  - TOO OLD, the window would not draw. Run --help."
    return f"Tk {version}"


def console_build_banner() -> str:
    """What the command line build says when it is started with nothing to do.

    Named from the running executable rather than hard coded, so it cannot end
    up telling someone to run a file that is not the one sitting next to them.
    """
    cli = os.path.basename(sys.executable)
    gui = cli.replace("-cli", "", 1)
    rows = [
        (gui, "the window - that is the one to double click"),
        (f"{cli} --headless", "a server with no window"),
        (f"{cli} --list", "the device profiles"),
        (f"{cli} --ui", "the window, from here"),
    ]
    width = max(len(left) for left, _ in rows) + 4
    why = ("Windows makes a program choose between having a console and having a "
           "window\nwhen it is built, so the same program ships twice. Every option "
           "is the same\nin both."
           if compat.IS_WINDOWS else
           "This build was made as a console program, so it prints instead of "
           "opening\na window. Every option is the same in both.")
    listing = "\n".join(f"  {left.ljust(width)}{right}" for left, right in rows)
    return (
        f"\nThis is {branding.APP_NAME}'s command line build. It has a console but no\n"
        "window, which is why it printed the help above instead of starting.\n"
        "\n"
        f"{listing}\n"
        "\n"
        f"{why}"
    )


def main(argv: list[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
    # a windowed build has no stdout until we ask for one; any argument at all
    # means the user is on a command line and expects to see output
    compat.enable_console_output(force=bool(given))
    parser = build_parser()
    args = parser.parse_args(given)

    if args.version:
        print(f"{branding.APP_NAME} {branding.VERSION}")
        print(f"{branding.VENDOR} - {branding.VENDOR_URL}")
        print(f"{branding.LICENCE_NAME} - {branding.PROJECT_URL}")
        print("This program comes with ABSOLUTELY NO WARRANTY. It is free")
        print("software, and you are welcome to redistribute it under the")
        print("terms of the GPL; see the LICENSE file for the conditions.")
        print(f"Python {platform.python_version()} on {platform.platform()}")
        print(tk_report())
        return 0

    # The console binary exists so that --headless, --list and piping work on
    # Windows. Started with nothing to do it used to open the window, which
    # made it look like a second copy of the app rather than the command line
    # half of it. It now does what a command line program should.
    if not given and compat.is_console_build():
        parser.print_help()
        print(console_build_banner())
        return 0

    # Only now: this creates the devices folder next to the executable on the
    # first run, and neither --version nor the help above has any business
    # doing that.
    devices_dir = args.devices_dir or compat.ensure_devices_dir()

    if args.ports:
        ports = list_serial_ports()
        if not ports:
            print("no serial ports found")
            note = compat.serial_permission_note()
            if note:
                print(note)
            return 1
        print("serial ports:")
        for device, description in ports:
            print(f"  {device:<24} {description}")
        return 0

    if args.list:
        found = discover_profiles(devices_dir)
        if not found:
            print(f"no device profiles in {devices_dir}")
            return 1
        print(f"device profiles in {devices_dir}:")
        for path in found:
            try:
                profile = load_profile(path)
                print(f"  {os.path.basename(path):<28} {profile.title} "
                      f"({len(profile.registers)} registers, unit id {profile.unit_id})")
            except ProfileError as exc:
                print(f"  {os.path.basename(path):<28} INVALID: {exc}")
        return 0

    try:
        profile_path = resolve_profile(devices_dir, args.device)
        load_profile(profile_path)              # fail fast on a broken profile
    except ProfileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.headless:
        if args.ui:
            print("error: --headless and --ui ask for opposite things", file=sys.stderr)
            return 2
        if args.role == "client":
            return run_client_headless(args, profile_path)
        return run_server_headless(args, profile_path)

    try:
        from creabus_tool.ui import run_ui
    except ImportError as exc:                  # tkinter missing on this python
        print(f"error: the UI needs tkinter ({exc}).", file=sys.stderr)
        print("       Install it (Debian/Ubuntu: sudo apt install python3-tk, "
              "macOS: brew install python-tk) or use --headless.", file=sys.stderr)
        return 2

    default_host = args.host or ("0.0.0.0" if args.role == "server" else "192.168.1.50")
    return run_ui(devices_dir, profile_path, default_host, args.port,
                  role=args.role, link=args.link, serial_port=args.serial_port,
                  autostart=args.start)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Someone piped us into `head` and stopped reading. Point what is left
        # of stdout at the void so the interpreter does not report the same
        # broken pipe again while flushing on the way out.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
