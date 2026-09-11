# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Protocol self test: starts the server in-process and exercises it."""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import textwrap
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool import branding, compat, theme
from creabus_tool.datastore import DataStore, decode_words
from creabus_tool.expressions import ExpressionError, check_expression
from creabus_tool.profile import (
    ProfileError,
    RegisterSpec,
    discover_profiles,
    load_profile,
)
from creabus_tool.server import SimulationRunner, TcpServer
from tools.read_meter import ModbusClient

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 15020
passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def test_runs_without_tk() -> None:
    """The headless modes must work on a machine with no Tk installed.

    Importing the package pulls in the branding module, which is also what the
    window uses - so it is easy to accidentally make `import creabus_tool`
    require tkinter and break every server without a desktop on it. This runs
    a subprocess with tkinter blocked at import time to prove it does not.
    """
    script = textwrap.dedent(
        """
        import sys

        class Blocked:
            def find_module(self, name, path=None):
                return self if name == "tkinter" or name.startswith("tkinter.") else None
            def load_module(self, name):
                raise ImportError("tkinter is blocked for this test")
            def find_spec(self, name, path=None, target=None):
                if name == "tkinter" or name.startswith("tkinter."):
                    raise ImportError("tkinter is blocked for this test")
                return None

        sys.meta_path.insert(0, Blocked())
        for stale in [m for m in sys.modules if m.startswith("tkinter")]:
            del sys.modules[stale]

        import creabus_tool
        import main
        from creabus_tool.datastore import DataStore
        from creabus_tool.profile import load_profile
        from creabus_tool.server import TcpServer

        assert creabus_tool.APP_NAME, "the product name is not importable"
        assert main.parse_args(["--headless"]).headless
        print("OK", creabus_tool.__version__)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=HERE,
                            capture_output=True, text=True, check=False)
    output = (result.stderr or result.stdout).strip().splitlines()
    check("the package imports with no tkinter available",
          result.returncode == 0 and result.stdout.startswith("OK"),
          output[-1] if output else "")


def test_tk_version_gate() -> None:
    """The window refuses a Tk too old to draw it, and only then.

    macOS ships Tcl/Tk 8.5.9, which opens a window and never paints it. The
    check that catches that is pure, so it can be exercised anywhere - the
    machine running this does not have to have the broken Tk.
    """
    check("a version string parses into numbers",
          compat.parse_tk_version("8.6.13") == (8, 6, 13))
    check("a distribution suffix does not confuse it",
          compat.parse_tk_version("8.6.13+deb") == (8, 6, 13))
    check("nonsense parses to nothing and is not treated as old",
          compat.parse_tk_version("junk") == () and not compat.tk_version_problem("junk"))

    for good in ("8.6", "8.6.13", "9.0.0"):
        check(f"Tk {good} is accepted", not compat.tk_version_problem(good), good)

    problem = compat.tk_version_problem("8.5.9")
    check("Tk 8.5.9 is refused", bool(problem))
    check("the refusal names the version found and the one needed",
          "8.5.9" in problem and "8.6" in problem, problem[:80])
    check("the refusal says what still works",
          "--headless" in problem, problem[-160:])
    check("the refusal names the interpreter that has to change",
          sys.executable in problem, problem[:200])

    check("a source checkout is never mistaken for the console build",
          not compat.is_console_build())


def test_conventional_addresses() -> None:
    """The number a meter manual prints, including past the five digit range.

    The classic 4xxxx form has four digits for the address, so it stops at
    9998. A holding register at 0xFC00 came out as "104513", which is not a
    number any manual prints and reads like a coil; the six digit form covers
    the whole 16 bit range and gives 464513.
    """
    def numbered(table: str, address: int) -> int:
        return RegisterSpec(name="x", address=address, table=table).conventional_address

    check("holding 0 is 40001", numbered("holding", 0) == 40001)
    check("input 0 is 30001", numbered("input", 0) == 30001)
    check("discrete 0 is 10001", numbered("discrete", 0) == 10001)
    check("coil 0 is 1", numbered("coil", 0) == 1)

    check("the last five digit holding address is 49999",
          numbered("holding", 9998) == 49999, str(numbered("holding", 9998)))
    check("one past it moves to the six digit form",
          numbered("holding", 9999) == 410000, str(numbered("holding", 9999)))
    check("the SDM120 serial number reads as 464513, not 104513",
          numbered("holding", 0xFC00) == 464513, str(numbered("holding", 0xFC00)))
    check("the top of the address range still fits",
          numbered("holding", 0xFFFF) == 465536, str(numbered("holding", 0xFFFF)))
    check("input registers get the same treatment",
          numbered("input", 0xFC00) == 364513, str(numbered("input", 0xFC00)))

    profile = load_profile(os.path.join(HERE, "devices", "sdm120.yaml"))
    check("and the shipped profile agrees",
          profile.by_name("serial_number").conventional_address == 464513)


def test_theme_luminance() -> None:
    """Which palette a desktop gets comes down to this one number."""
    white = theme.luminance((65535, 65535, 65535))
    black = theme.luminance((0, 0, 0))
    # the weights sum to 1.0 in decimal but not in binary, so allow the slack
    check("white is 1.0 and black is 0.0", abs(white - 1.0) < 1e-9 and black == 0.0,
          f"{white} {black}")
    check("a channel outside the 16 bit range is clamped, not extrapolated",
          theme.luminance((99999, 99999, 99999)) <= 1.0 + 1e-9
          and theme.luminance((-5, -5, -5)) == 0.0)
    check("a macOS dark window background counts as dark",
          theme.luminance((0x1e1e, 0x1e1e, 0x1e1e)) < theme.DARK_THRESHOLD)
    check("a clam grey counts as light",
          theme.luminance((0xdcdc, 0xdada, 0xd5d5)) > theme.DARK_THRESHOLD)
    check("green weighs more than blue, so navy is not mistaken for light",
          theme.luminance((0, 0, 65535)) < theme.luminance((0, 65535, 0)))
    check("the two palettes disagree about every surface",
          theme.LIGHT.surface != theme.DARK.surface
          and theme.LIGHT.ink != theme.DARK.ink
          and theme.LIGHT.hot != theme.DARK.hot)
    check("every log kind has a colour in both",
          set(theme.LIGHT.log) == set(theme.DARK.log), str(set(theme.LIGHT.log)))


def test_every_profile_runs() -> None:
    """Every shipped profile must load AND actually produce values.

    Loading proves the file parses; it does not prove the simulation works.
    An expression naming a register that does not exist only fails when it is
    evaluated - which is how three phase currents in the SunSpec inverter
    profile referred to registers that live in the meter profile.
    """
    for path in discover_profiles(os.path.join(HERE, "devices")):
        name = os.path.basename(path)
        profile = load_profile(path)
        store = DataStore(profile)
        simulation = SimulationRunner(store, tick=0.05)
        simulation.start()
        time.sleep(0.5)
        simulation.stop()
        snapshot = store.snapshot()
        broken = [f"{r.name}: {r.error}" for r in snapshot if r.error]
        check(f"{name} simulates every register without error", not broken, str(broken[:2]))
        missing = [r.name for r in snapshot if r.value is None]
        check(f"{name} gives every register a value", not missing, str(missing[:4]))


def test_sunspec_chain() -> None:
    """Walk the SunSpec profiles over Modbus, the way a real client does.

    A SunSpec client finds its way by reading "SunS", then hopping model to
    model using each one's declared length until the end marker. Get one id
    or length a register out and the walk lands mid-model - so this is the
    check that the addresses in those two files are actually self consistent.
    """
    base, port = 40000, 15310
    for name, expected in (("sunspec-inverter.yaml", [1, 103]),
                           ("sunspec-meter.yaml", [1, 203])):
        profile = load_profile(os.path.join(HERE, "devices", name))
        store = DataStore(profile)
        server = TcpServer(store, "127.0.0.1", port, log_requests=False)
        server.start()
        time.sleep(0.3)
        client = ModbusClient("127.0.0.1", port, profile.unit_id)
        try:
            marker = struct.pack(">HH", *client.read("holding", base, 2))
            check(f"{name} announces itself with the SunSpec marker",
                  marker == b"SunS", repr(marker))

            # A wrong length sends the walk into the middle of a model, or off
            # the end of the map entirely - which the server answers with
            # exception 0x02. Caught here so that reads as a failed check
            # rather than a traceback that abandons the rest of the run.
            walked, at, ended, blew_up = [], base + 2, False, ""
            try:
                for _hop in range(8):
                    model_id, length = client.read("holding", at, 2)
                    if model_id == 0xFFFF:
                        ended = length == 0
                        break
                    walked.append(model_id)
                    # every register the model claims must actually answer
                    remaining, offset = length, at + 2
                    while remaining:
                        chunk = min(100, remaining)
                        client.read("holding", offset, chunk)
                        offset += chunk
                        remaining -= chunk
                    at += length + 2
            except (RuntimeError, OSError, struct.error) as exc:
                blew_up = f"the walk ran off the map at {at}: {exc}"

            check(f"{name} chains the models it should",
                  walked == expected and not blew_up, blew_up or str(walked))
            check(f"{name} ends the chain with id 65535 length 0", ended and not blew_up,
                  blew_up)
        finally:
            client.close()
            server.stop()
        port += 1


def test_expressions_are_sandboxed() -> None:
    """A device profile is code, and profiles get shared. This is the boundary.

    `eval` with builtins removed stops a typo, not an attacker: every object
    in Python carries its type hierarchy, so ().__class__.__base__ walks out
    to every loaded class and from there to the ones that run commands. The
    expression language is checked against an allowlist of syntax instead,
    and attribute access is not on it.
    """
    names = {"voltage", "current"}

    escapes = [
        ("the classic subclass walk", "().__class__.__base__.__subclasses__()"),
        ("attribute access on a register", "voltage.__class__"),
        ("a comprehension", "[c for c in ().__class__.__mro__]"),
        ("a lambda", "(lambda: 1)()"),
        ("the import builtin", "__import__('os')"),
        ("open", "open('/etc/passwd')"),
        ("globals", "globals()"),
        ("indexing", "voltage[0]"),
        ("an f-string", "f'{voltage}'"),
        ("the walrus", "(x := 5)"),
        ("an unknown name", "secret_backdoor"),
        ("a power tower", "10**10**8"),
        ("a huge literal exponent", "2**2000"),
    ]
    for label, source in escapes:
        try:
            check_expression(source, names)
            check(f"{label} is refused", False, f"ALLOWED: {source}")
        except ExpressionError:
            check(f"{label} is refused", True)

    allowed = [
        "voltage * current",
        "sqrt(max(voltage * current, 0.0))",
        "voltage if current > 1.0 else 0.0",
        "choice(1, 2, 3) + uniform(0, 1)",
        "voltage ** 2",
        "voltage ** current",
        "-voltage + abs(current) * 2",
        "t + now",
    ]
    for source in allowed:
        try:
            check_expression(source, names)
            check(f"real maths still works: {source[:34]}", True)
        except ExpressionError as exc:
            check(f"real maths still works: {source[:34]}", False, str(exc))

    # and the refusal has to happen at load time, not when the value is wanted
    broken = os.path.join(HERE, "devices", "_sandbox_probe.yaml")
    with open(broken, "w", encoding="utf-8") as handle:
        handle.write(
            "device:\n  name: probe\nregisters:\n"
            "  - {name: a, table: input, address: 0, type: float32,\n"
            "     mode: fixed, value: 1.0}\n"
            "  - {name: b, table: input, address: 2, type: float32,\n"
            "     mode: expression, expression: \"().__class__\"}\n")
    try:
        load_profile(broken)
        check("a profile with a hostile expression is refused at load", False)
    except ProfileError as exc:
        check("a profile with a hostile expression is refused at load",
              "not allowed" in str(exc), str(exc))
    finally:
        os.remove(broken)


def test_changelog_covers_this_version() -> None:
    """The changelog must have an entry for whatever version this is.

    A version bump with no changelog entry is the way a changelog quietly
    stops being worth reading, so it is a test rather than a good intention.
    """
    path = os.path.join(HERE, "CHANGELOG.md")
    check("CHANGELOG.md exists", os.path.isfile(path))
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    check(f"the changelog has a section for {branding.VERSION}",
          f"## {branding.VERSION}" in text, branding.VERSION)
    check("and one for every version that has been tagged",
          "## 0.0.1" in text)


def test_console_build_prints_help() -> None:
    """The command line binary must not open a window when given nothing.

    Windows is the only platform where a program has to pick a console or a
    window at build time, so it is the only one that ships two executables.
    The console one used to open the window when it was double clicked, which
    made it look like a second, identical copy of the app. Run in a subprocess
    that claims to be that binary, since the check is on sys.executable.
    """
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, %r)
        sys.executable = %r

        from creabus_tool import compat
        compat.IS_FROZEN = True                 # pretend to be the frozen -cli build

        import main
        assert compat.is_console_build(), "the -cli name was not recognised"
        code = main.main([])
        assert code == 0, code
        """
    ) % (HERE, os.path.join(HERE, "creabus-tool-cli"))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=60)
    check("the command line build exits cleanly with no arguments",
          result.returncode == 0, result.stderr[-400:])
    check("it prints the usage rather than opening a window",
          "--headless" in result.stdout and "usage:" in result.stdout,
          result.stdout[:200])
    check("it says why there are two executables",
          "command line build" in result.stdout, result.stdout[-300:])
    check("the examples name the binary rather than main.py",
          "main.py" not in result.stdout and "creabus-tool-cli --list" in result.stdout,
          result.stdout[:200])
    check("the bare invocation in the examples carries --ui, since it prints help",
          "creabus-tool-cli --ui " in result.stdout, result.stdout[:200])
    check("it did not create a devices folder beside the fake executable",
          not os.path.isdir(os.path.join(HERE, "creabus-tool-cli")))


def main() -> int:
    test_runs_without_tk()
    test_tk_version_gate()
    test_conventional_addresses()
    test_theme_luminance()
    test_expressions_are_sandboxed()
    test_every_profile_runs()
    test_sunspec_chain()
    test_changelog_covers_this_version()
    test_console_build_prints_help()
    profile = load_profile(os.path.join(HERE, "devices", "sdm120.yaml"))
    store = DataStore(profile)
    simulation = SimulationRunner(store, tick=0.1)
    simulation.start()
    server = TcpServer(store, "127.0.0.1", PORT, log_requests=False)
    server.start()
    time.sleep(0.3)

    try:
        client = ModbusClient("127.0.0.1", PORT, profile.unit_id)

        # --- block read across defined registers and gaps -------------------
        words = client.read("input", 0, 32)
        check("block read of 32 input registers returns 32 words", len(words) == 32)
        voltage = decode_words(profile.by_name("voltage"), words[0:2], "big", "big")
        check("voltage inside a block read is plausible", 220.0 < voltage < 250.0, f"{voltage}")
        check("undefined gap reads back as zero", words[2:6] == [0, 0, 0, 0], str(words[2:6]))

        power = decode_words(profile.by_name("active_power"), words[12:14], "big", "big")
        current = decode_words(profile.by_name("current"), words[6:8], "big", "big")
        pf = decode_words(profile.by_name("power_factor"), words[30:32], "big", "big")
        check("P == V * I * PF", abs(power - voltage * current * pf) < 0.6,
              f"{power} vs {voltage * current * pf}")

        # --- maximum read size ---------------------------------------------
        check("125 register read is accepted", len(client.read("input", 0, 125)) == 125)
        try:
            client.read("input", 0, 126)
            check("126 register read is rejected", False)
        except RuntimeError as exc:
            check("126 register read is rejected", "0x03" in str(exc), str(exc))

        # --- writes ----------------------------------------------------------
        client.write_registers(0x0000, list(struct.unpack(">HH", struct.pack(">f", 200.0))))
        back = decode_words(profile.by_name("relay_pulse_width"),
                            client.read("holding", 0x0000, 2), "big", "big")
        check("write then read back holding register", abs(back - 200.0) < 0.01, f"{back}")

        try:
            client.write_registers(0x0100, [1, 2])
            check("write to an undefined holding address is tolerated (gap_policy zero)", True)
        except RuntimeError as exc:
            check("write to an undefined holding address is tolerated", False, str(exc))

        # --- exceptions --------------------------------------------------------
        try:
            client.request(bytes([0x63, 0x00, 0x00]))
            check("unsupported function -> exception 0x01", False)
        except RuntimeError as exc:
            check("unsupported function -> exception 0x01", "0x01" in str(exc), str(exc))

        wrong = ModbusClient("127.0.0.1", PORT, 9)
        try:
            wrong.read("input", 0, 2)
            check("wrong unit id -> exception 0x0B", False)
        except RuntimeError as exc:
            check("wrong unit id -> exception 0x0B", "0x0B" in str(exc), str(exc))
        wrong.close()

        # --- concurrent clients ----------------------------------------------
        errors: list[str] = []

        def hammer() -> None:
            try:
                other = ModbusClient("127.0.0.1", PORT, profile.unit_id)
                for _ in range(25):
                    other.read("input", 0, 40)
                other.close()
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        check("6 concurrent clients, 150 reads, no errors", not errors, "; ".join(errors[:2]))

        # --- values move over time ---------------------------------------------
        before = client.read("input", 0, 2)
        deadline = time.time() + 8
        while time.time() < deadline and client.read("input", 0, 2) == before:
            time.sleep(0.5)
        check("voltage changes within its 5s interval", client.read("input", 0, 2) != before)

        energy_before = decode_words(profile.by_name("import_active_energy"),
                                     client.read("input", 0x0048, 2), "big", "big")
        time.sleep(11)
        energy_after = decode_words(profile.by_name("import_active_energy"),
                                    client.read("input", 0x0048, 2), "big", "big")
        check("import energy only counts up", energy_after > energy_before,
              f"{energy_before} -> {energy_after}")

        stats = server.stats.snapshot()
        check("server counted the requests", stats["requests"] > 150, str(stats["requests"]))

        # --- per register read accounting ------------------------------------
        voltage_reg = next(r for r in store.snapshot() if r.name == "voltage")
        check("reads are counted per register", voltage_reg.reads > 150, str(voltage_reg.reads))
        check("the register remembers who read it",
              voltage_reg.last_client.startswith("127.0.0.1"), voltage_reg.last_client)
        untouched = next(r for r in store.snapshot() if r.name == "serial_number")
        check("a register nobody read stays at zero reads", untouched.reads == 0,
              str(untouched.reads))

        # --- settings changed while the server is running --------------------
        big = client.read("input", 0, 2)
        store.set_framing("little", "big")
        little = client.read("input", 0, 2)
        check("word order change swaps the two registers", little == [big[1], big[0]],
              f"{big} -> {little}")
        store.set_framing("big", "big")
        check("word order change is reversible", client.read("input", 0, 2) == big)

        store.profile.unit_id = 7
        moved = ModbusClient("127.0.0.1", PORT, 7)
        check("Modbus address can be changed while running",
              len(moved.read("input", 0, 2)) == 2)
        moved.close()
        try:
            client.read("input", 0, 2)
            check("the old unit id stops answering", False)
        except RuntimeError as exc:
            check("the old unit id stops answering", "0x0B" in str(exc), str(exc))
        store.profile.accept_any_unit_id = True
        check("answer any unit id lets it through", len(client.read("input", 0, 2)) == 2)
        store.profile.accept_any_unit_id = False
        store.profile.unit_id = 1

        # --- response delay ----------------------------------------------------
        server.response_delay = 0.25
        started = time.time()
        client.read("input", 0, 2)
        check("response delay is honoured", time.time() - started >= 0.24,
              f"{time.time() - started:.3f}s")
        server.response_delay = 0.0

        # --- client limit -------------------------------------------------------
        server.max_clients = 1
        try:
            spare = ModbusClient("127.0.0.1", PORT, 1)
            spare.read("input", 0, 2)
            check("client limit refuses the extra connection", False)
        except (RuntimeError, ConnectionError, OSError):
            check("client limit refuses the extra connection", True)
        server.max_clients = 16

        client.close()
    finally:
        server.stop()
        simulation.stop()

    # ---------------- second device: bits, strings and scaled integers -------
    profile = load_profile(os.path.join(HERE, "devices", "_template.yaml"))
    store = DataStore(profile)
    simulation = SimulationRunner(store, tick=0.1)
    simulation.start()
    server = TcpServer(store, "127.0.0.1", PORT + 1, log_requests=False)
    server.start()
    time.sleep(0.3)
    try:
        client = ModbusClient("127.0.0.1", PORT + 1, profile.unit_id)

        tag = decode_words(profile.by_name("device_tag"),
                           client.read("holding", 0x0010, 8), "big", "big")
        check("string register reads back", tag == "TEMPLATE-01", repr(tag))

        volts = decode_words(profile.by_name("voltage_x10"),
                             client.read("input", 0x0004, 1), "big", "big")
        check("scaled uint16 decodes to volts", 219.0 < volts < 246.0, f"{volts}")

        negative = decode_words(profile.by_name("power_signed"),
                                client.read("input", 0x0005, 2), "big", "big")
        check("signed int32 is in range", -3500.0 <= negative <= 4500.0, f"{negative}")

        check("coil reads back on", client.read("coil", 0, 1) == [1])
        client.request(struct.pack(">BHH", 0x05, 0, 0x0000))
        check("coil write turns it off", client.read("coil", 0, 1) == [0])
        check("discrete inputs are readable", len(client.read("discrete", 0, 2)) == 2)

        before = client.read("holding", 0x0000, 1)
        try:
            client.write_registers(0x0000, [999])
            check("write to a read only register is refused", False)
        except RuntimeError as exc:
            check("write to a read only register is refused", "0x02" in str(exc), str(exc))
        check("refused write left the value untouched",
              client.read("holding", 0x0000, 1) == before)

        try:
            client.request(struct.pack(">BHH", 0x05, 0, 0x1234))
            check("invalid coil value is refused", False)
        except RuntimeError as exc:
            check("invalid coil value is refused", "0x03" in str(exc), str(exc))

        client.close()
    finally:
        server.stop()
        simulation.stop()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
