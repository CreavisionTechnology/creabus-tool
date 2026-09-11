# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Register storage, value encoding and the dummy-data simulation engine."""

from __future__ import annotations

import random
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .pdu import (
    ILLEGAL_DATA_ADDRESS,
    ILLEGAL_DATA_VALUE,
    ILLEGAL_FUNCTION,
    MAX_READ_BITS,
    MAX_READ_REGISTERS,
    SERVER_FAILURE,
    ModbusError,
)
from .expressions import (
    SAFE_FUNCTIONS,
    ExpressionError,
    compile_expression,
    evaluation_globals,
)
from .profile import BIT_TABLES, NUMERIC_TYPES, DeviceProfile, RegisterSpec

__all__ = [
    "ILLEGAL_DATA_ADDRESS",
    "ILLEGAL_DATA_VALUE",
    "ILLEGAL_FUNCTION",
    "SERVER_FAILURE",
    "DataStore",
    "ModbusError",
    "Register",
    "decode_words",
    "encode_value",
]


# --- value <-> register words ------------------------------------------------

def _int_limits(fmt: str) -> tuple[int, int]:
    bits = {"h": 16, "H": 16, "i": 32, "I": 32, "q": 64, "Q": 64}[fmt]
    if fmt.islower():
        return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    return 0, 2**bits - 1


def _bytes_to_words(data: bytes, word_order: str, byte_order: str) -> list[int]:
    words = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    if byte_order == "little":
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]
    if word_order == "little":
        words.reverse()
    return words


def _words_to_bytes(words: list[int], word_order: str, byte_order: str) -> bytes:
    words = list(words)
    if word_order == "little":
        words.reverse()
    if byte_order == "little":
        words = [((w & 0xFF) << 8) | (w >> 8) for w in words]
    return b"".join(int(w & 0xFFFF).to_bytes(2, "big") for w in words)


def encode_value(spec: RegisterSpec, value: Any, word_order: str, byte_order: str) -> list[int]:
    """Engineering value -> list of 16 bit register words."""
    if spec.type == "string":
        text = "" if value is None else str(value)
        data = text.encode("ascii", "replace")[: spec.length]
        data = data.ljust(spec.word_count * 2, b"\x00")
        return _bytes_to_words(data, word_order, byte_order)

    _words, fmt, is_float = NUMERIC_TYPES[spec.type]
    raw = (float(value) if value is not None else 0.0) / spec.scale
    if is_float:
        packed = struct.pack(">" + fmt, raw)
    else:
        low, high = _int_limits(fmt)
        packed = struct.pack(">" + fmt, max(low, min(high, round(raw))))
    return _bytes_to_words(packed, word_order, byte_order)


def decode_words(spec: RegisterSpec, words: list[int], word_order: str, byte_order: str) -> Any:
    """List of 16 bit register words -> engineering value."""
    data = _words_to_bytes(words, word_order, byte_order)
    if spec.type == "string":
        return data.split(b"\x00")[0].decode("ascii", "replace")
    _, fmt, _ = NUMERIC_TYPES[spec.type]
    (raw,) = struct.unpack(">" + fmt, data[: struct.calcsize(">" + fmt)])
    return raw * spec.scale


# --- expression sandbox ------------------------------------------------------


# --- runtime register --------------------------------------------------------

@dataclass
class Register:
    spec: RegisterSpec
    value: Any = 0.0
    last_update: float = 0.0          # time.monotonic() of last change
    updated_wall: float = 0.0         # time.time() of last change
    updates: int = 0
    pinned: bool = False              # value held: simulation skips it
    error: str = ""
    reads: int = 0                    # times a client has read this register
    writes: int = 0                   # times a client has written it
    last_read: float = 0.0            # time.time() of the last client read
    last_write: float = 0.0           # time.time() of the last client write
    last_client: str = ""             # who touched it last

    @property
    def name(self) -> str:
        return self.spec.name

    def format_value(self) -> str:
        spec = self.spec
        if self.value is None:
            return "-"
        if spec.table in BIT_TABLES:
            return "ON" if self.value else "OFF"
        if spec.type == "string":
            return str(self.value)
        if spec.is_float or spec.scale != 1.0:
            decimals = spec.decimals if spec.decimals is not None else 3
            return f"{float(self.value):.{decimals}f}"
        return str(int(self.value))

    def due_in(self, now: float) -> float:
        return max(0.0, self.spec.interval - (now - self.last_update))


class DataStore:
    """Holds every register of one device and keeps the dummy data moving."""

    def __init__(self, profile: DeviceProfile, on_event: Callable[..., None] | None = None):
        self.profile = profile
        self.lock = threading.RLock()
        self._on_event = on_event or (lambda *a, **k: None)
        self._code_cache: dict[str, Any] = {}
        self.started = time.monotonic()

        self.registers: list[Register] = [Register(spec) for spec in profile.registers]
        self._by_name: dict[str, Register] = {r.name: r for r in self.registers}

        # address -> register, per table
        self._words: dict[str, dict[int, int]] = {"input": {}, "holding": {}}
        self._bits: dict[str, dict[int, bool]] = {"coil": {}, "discrete": {}}
        self._owner: dict[str, dict[int, Register]] = {t: {} for t in
                                                       ("input", "holding", "coil", "discrete")}
        for reg in self.registers:
            if reg.spec.is_bit:
                self._owner[reg.spec.table][reg.spec.address] = reg
                self._bits[reg.spec.table][reg.spec.address] = False
            else:
                for address in reg.spec.addresses:
                    self._owner[reg.spec.table][address] = reg
                    self._words[reg.spec.table][address] = 0

        self._expression_order = self._sort_expressions()
        self.initialise()

    # -- setup ---------------------------------------------------------------
    def _sort_expressions(self) -> list[Register]:
        """Expression registers in dependency order (cycles keep file order)."""
        pending = [r for r in self.registers if r.spec.mode == "expression"]
        names = {r.name for r in pending}
        ordered: list[Register] = []
        done: set[str] = set()
        remaining = list(pending)
        while remaining:
            progressed = False
            for reg in list(remaining):
                waiting = [d for d in reg.spec.depends_on if d in names and d not in done]
                if not waiting:
                    ordered.append(reg)
                    done.add(reg.name)
                    remaining.remove(reg)
                    progressed = True
            if not progressed:                      # circular reference
                ordered.extend(remaining)
                break
        return ordered

    def initialise(self) -> None:
        """Seed every register so nothing reads back as zero before the first tick."""
        with self.lock:
            now = time.monotonic()
            for reg in self.registers:
                spec = reg.spec
                if spec.start is not None:
                    reg.value = spec.start if spec.type == "string" else float(spec.start)
                elif spec.table in BIT_TABLES:
                    reg.value = False
                elif spec.type == "string":
                    reg.value = ""
                elif spec.minimum is not None and spec.maximum is not None:
                    reg.value = (spec.minimum + spec.maximum) / 2.0
                else:
                    reg.value = 0.0
                if spec.table in BIT_TABLES:
                    reg.value = bool(reg.value)
                reg.last_update = now
                reg.updated_wall = time.time()
                self._store(reg)
            # random / walk registers get a first proper sample, then everything
            # derived from them is evaluated
            for reg in self.registers:
                if reg.spec.mode in ("walk", "random"):
                    self._simulate(reg, now, 0.0)
            for reg in self.registers:
                if reg.spec.mode == "accumulator":
                    self._store(reg)
            for reg in self._expression_order:
                self._evaluate_expression(reg)
                self._store(reg)

    # -- simulation ----------------------------------------------------------
    def tick(self) -> list[Register]:
        """Advance the simulation. Returns the registers that changed."""
        changed: list[Register] = []
        with self.lock:
            now = time.monotonic()

            for reg in self.registers:
                if reg.pinned or reg.spec.mode in ("expression", "accumulator", "fixed"):
                    continue
                if now - reg.last_update >= reg.spec.interval:
                    self._simulate(reg, now, now - reg.last_update)
                    changed.append(reg)

            for reg in self.registers:
                if reg.pinned or reg.spec.mode != "accumulator":
                    continue
                elapsed = now - reg.last_update
                if elapsed >= reg.spec.interval:
                    self._accumulate(reg, now, elapsed)
                    changed.append(reg)

            recompute = {r.name for r in changed}
            for reg in self._expression_order:
                if reg.pinned:
                    continue
                due = now - reg.last_update >= reg.spec.interval
                follows = any(dep in recompute for dep in reg.spec.depends_on)
                if due or follows:
                    if self._evaluate_expression(reg):
                        reg.last_update = now
                        reg.updated_wall = time.time()
                        reg.updates += 1
                        self._store(reg)
                        changed.append(reg)
                        recompute.add(reg.name)
        return changed

    def _simulate(self, reg: Register, now: float, elapsed: float) -> None:
        spec = reg.spec
        if spec.table in BIT_TABLES:
            reg.value = random.random() < 0.5 if spec.mode != "fixed" else bool(reg.value)
        elif spec.type == "string":
            pass
        elif spec.mode == "random":
            reg.value = random.uniform(spec.minimum, spec.maximum)
        elif spec.mode == "walk":
            step = spec.step or (spec.maximum - spec.minimum) / 20.0
            candidate = float(reg.value) + random.uniform(-step, step)
            reg.value = max(spec.minimum, min(spec.maximum, candidate))
        reg.value = self._round(reg)
        reg.last_update = now
        reg.updated_wall = time.time()
        reg.updates += 1
        self._store(reg)

    def _accumulate(self, reg: Register, now: float, elapsed: float) -> None:
        spec = reg.spec
        try:
            rate = float(self._eval(spec.rate, reg))
            reg.error = ""
        except ExpressionError as exc:
            reg.error = str(exc)
            rate = 0.0
        value = float(reg.value) + rate * elapsed
        if spec.maximum is not None and value > spec.maximum:
            value = spec.minimum or 0.0 if spec.wrap else spec.maximum
        if spec.minimum is not None and value < spec.minimum:
            value = spec.minimum
        reg.value = value
        reg.value = self._round(reg)
        reg.last_update = now
        reg.updated_wall = time.time()
        reg.updates += 1
        self._store(reg)

    def _evaluate_expression(self, reg: Register) -> bool:
        spec = reg.spec
        try:
            value = self._eval(spec.expression, reg)
            reg.error = ""
        except ExpressionError as exc:
            if reg.error != str(exc):
                self._on_event("error", f"register '{reg.name}': {exc}")
            reg.error = str(exc)
            return False
        if spec.table in BIT_TABLES:
            reg.value = bool(value)
        elif spec.type == "string":
            reg.value = str(value)
        else:
            value = float(value)
            if spec.minimum is not None:
                value = max(spec.minimum, value)
            if spec.maximum is not None:
                value = min(spec.maximum, value)
            reg.value = value
        reg.value = self._round(reg)
        return True

    def _round(self, reg: Register) -> Any:
        spec = reg.spec
        if spec.decimals is None or spec.table in BIT_TABLES or spec.type == "string":
            return reg.value
        return round(float(reg.value), spec.decimals)

    def _eval(self, source: str, reg: Register) -> Any:
        code = self._code_cache.get(source)
        if code is None:
            # Vetted again here even though load_profile already did it: this
            # is the only door into eval(), so it is the one that should be
            # locked, rather than a caller trusted to have checked.
            code = compile_expression(source, {r.name for r in self.registers})
            self._code_cache[source] = code
        namespace = dict(SAFE_FUNCTIONS)
        namespace["t"] = time.monotonic() - self.started
        namespace["now"] = time.time()
        for other in self.registers:
            namespace[other.name] = other.value
        try:
            return eval(code, evaluation_globals(), namespace)
        except Exception as exc:
            raise ExpressionError(f"{type(exc).__name__}: {exc}") from None

    # -- storage -------------------------------------------------------------
    def _store(self, reg: Register) -> None:
        spec = reg.spec
        if spec.is_bit:
            self._bits[spec.table][spec.address] = bool(reg.value)
            return
        words = encode_value(spec, reg.value, self.profile.word_order, self.profile.byte_order)
        for offset, word in enumerate(words):
            self._words[spec.table][spec.address + offset] = word & 0xFFFF

    def _reload_from_words(self, reg: Register) -> None:
        spec = reg.spec
        words = [self._words[spec.table].get(a, 0) for a in spec.addresses]
        reg.value = decode_words(spec, words, self.profile.word_order, self.profile.byte_order)
        reg.value = self._round(reg)
        reg.last_update = time.monotonic()
        reg.updated_wall = time.time()

    # -- Modbus level access -------------------------------------------------
    def read_words(self, table: str, address: int, count: int) -> list[int]:
        if not 1 <= count <= MAX_READ_REGISTERS:
            raise ModbusError(ILLEGAL_DATA_VALUE,
                              f"quantity {count} out of range 1..{MAX_READ_REGISTERS}")
        if address + count > 0x10000:
            raise ModbusError(ILLEGAL_DATA_ADDRESS, "range extends past 0xFFFF")
        with self.lock:
            table_words = self._words[table]
            if self.profile.gap_policy == "exception":
                missing = [a for a in range(address, address + count) if a not in table_words]
                if missing:
                    raise ModbusError(
                        ILLEGAL_DATA_ADDRESS,
                        f"{table} address 0x{missing[0]:04X} is not defined by this device",
                    )
            return [table_words.get(a, 0) for a in range(address, address + count)]

    def read_bits(self, table: str, address: int, count: int) -> list[bool]:
        if not 1 <= count <= MAX_READ_BITS:
            raise ModbusError(ILLEGAL_DATA_VALUE,
                              f"quantity {count} out of range 1..{MAX_READ_BITS}")
        if address + count > 0x10000:
            raise ModbusError(ILLEGAL_DATA_ADDRESS, "range extends past 0xFFFF")
        with self.lock:
            table_bits = self._bits[table]
            if self.profile.gap_policy == "exception":
                missing = [a for a in range(address, address + count) if a not in table_bits]
                if missing:
                    raise ModbusError(
                        ILLEGAL_DATA_ADDRESS,
                        f"{table} address 0x{missing[0]:04X} is not defined by this device",
                    )
            return [bool(table_bits.get(a, False)) for a in range(address, address + count)]

    def write_words(self, address: int, words: list[int]) -> list[Register]:
        """Client write to holding registers. Returns the registers it touched."""
        with self.lock:
            owners = self._owner["holding"]
            # a Modbus write is all or nothing: check the whole range first
            for offset in range(len(words)):
                reg = owners.get(address + offset)
                if reg is None:
                    if self.profile.gap_policy == "exception":
                        raise ModbusError(
                            ILLEGAL_DATA_ADDRESS,
                            f"holding address 0x{address + offset:04X} is not defined "
                            "by this device",
                        )
                elif not reg.spec.writable:
                    raise ModbusError(ILLEGAL_DATA_ADDRESS, f"register '{reg.name}' is read only")

            touched: dict[str, Register] = {}
            for offset, word in enumerate(words):
                target = address + offset
                self._words["holding"][target] = word & 0xFFFF
                reg = owners.get(target)
                if reg is not None:
                    touched[reg.name] = reg
            for reg in touched.values():
                self._reload_from_words(reg)
                if reg.spec.mode != "fixed":
                    reg.pinned = True
            return list(touched.values())

    def write_bits(self, address: int, values: list[bool]) -> list[Register]:
        with self.lock:
            owners = self._owner["coil"]
            for offset in range(len(values)):
                reg = owners.get(address + offset)
                if reg is None:
                    if self.profile.gap_policy == "exception":
                        raise ModbusError(
                            ILLEGAL_DATA_ADDRESS,
                            f"coil address 0x{address + offset:04X} is not defined by this device",
                        )
                elif not reg.spec.writable:
                    raise ModbusError(ILLEGAL_DATA_ADDRESS, f"coil '{reg.name}' is read only")

            touched: dict[str, Register] = {}
            for offset, value in enumerate(values):
                target = address + offset
                self._bits["coil"][target] = bool(value)
                reg = owners.get(target)
                if reg is None:
                    continue
                reg.value = bool(value)
                reg.last_update = time.monotonic()
                reg.updated_wall = time.time()
                reg.pinned = reg.spec.mode != "fixed"
                touched[reg.name] = reg
            return list(touched.values())

    def note_access(self, table: str, address: int, count: int, peer: str,
                    writing: bool = False) -> list[Register]:
        """Record that a client touched this range. Drives the Registers tab."""
        stamp = time.time()
        touched = self.registers_in(table, address, count)
        with self.lock:
            for reg in touched:
                if writing:
                    reg.writes += 1
                    reg.last_write = stamp
                else:
                    reg.reads += 1
                    reg.last_read = stamp
                reg.last_client = peer
        return touched

    def set_framing(self, word_order: str, byte_order: str) -> None:
        """Change the 32 bit word / byte order and re-encode everything."""
        with self.lock:
            self.profile.word_order = word_order
            self.profile.byte_order = byte_order
            for reg in self.registers:
                self._store(reg)

    # -- helpers for the UI / logging ---------------------------------------
    def registers_in(self, table: str, address: int, count: int) -> list[Register]:
        with self.lock:
            owners = self._owner[table]
            seen: dict[str, Register] = {}
            for target in range(address, address + count):
                reg = owners.get(target)
                if reg is not None:
                    seen[reg.name] = reg
            return list(seen.values())

    def describe_range(self, table: str, address: int, count: int, limit: int = 4) -> str:
        found = self.registers_in(table, address, count)
        if not found:
            return "no registers defined in this range"
        names = [f"{r.spec.label}={r.format_value()}{(' ' + r.spec.unit) if r.spec.unit else ''}"
                 for r in found[:limit]]
        if len(found) > limit:
            names.append(f"... +{len(found) - limit} more")
        return ", ".join(names)

    def set_value(self, name: str, value: Any, pin: bool = True) -> None:
        with self.lock:
            reg = self._by_name[name]
            if reg.spec.table in BIT_TABLES:
                reg.value = bool(value)
            elif reg.spec.type == "string":
                reg.value = str(value)
            else:
                reg.value = float(value)
            reg.value = self._round(reg)
            reg.pinned = pin
            reg.last_update = time.monotonic()
            reg.updated_wall = time.time()
            reg.updates += 1
            self._store(reg)

    def blank(self) -> None:
        """Client mode: forget the simulated values, nothing has been read yet."""
        with self.lock:
            for reg in self.registers:
                reg.value = None
                reg.updates = 0
                reg.reads = 0
                reg.writes = 0
                reg.last_read = 0.0
                reg.last_write = 0.0
                reg.pinned = False
                reg.error = ""
                self._store(reg)

    def apply_poll(self, name: str, value: Any, peer: str = "", error: str = "") -> None:
        """Client mode: record a value read back from a real device."""
        with self.lock:
            reg = self._by_name[name]
            reg.error = error
            if not error:
                if reg.spec.table in BIT_TABLES:
                    reg.value = bool(value)
                elif reg.spec.type == "string":
                    reg.value = str(value)
                else:
                    reg.value = float(value)
                reg.value = self._round(reg)
                self._store(reg)
                reg.updates += 1
            reg.reads += 1
            reg.last_read = time.time()
            reg.last_client = peer

    def set_pinned(self, name: str, pinned: bool) -> None:
        with self.lock:
            self._by_name[name].pinned = pinned

    def snapshot(self) -> list[Register]:
        with self.lock:
            return list(self.registers)
