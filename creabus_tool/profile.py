# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Loading and validation of device profiles.

A profile is a YAML (or JSON) file that fully describes a simulated Modbus
device: its identity, framing conventions and every register it exposes.
Nothing about the SDM120 is hard coded in the Python - swap the profile and
the server becomes a different device.
"""

from __future__ import annotations


import json
import os
import re

from .expressions import ExpressionError, check_expression
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - dependency check
    yaml = None


class ProfileError(Exception):
    """Raised when a device profile is missing or malformed."""


# --- register types ---------------------------------------------------------
#   name -> (number of 16 bit registers, struct format, is_float)
NUMERIC_TYPES: dict[str, tuple[int, str, bool]] = {
    "int16": (1, "h", False),
    "uint16": (1, "H", False),
    "int32": (2, "i", False),
    "uint32": (2, "I", False),
    "int64": (4, "q", False),
    "uint64": (4, "Q", False),
    "float32": (2, "f", True),
    "float64": (4, "d", True),
}

BIT_TABLES = ("coil", "discrete")
WORD_TABLES = ("input", "holding")
TABLES = WORD_TABLES + BIT_TABLES

MODES = ("walk", "random", "fixed", "expression", "accumulator")

IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


# Zero based protocol address -> the number printed in a device manual. Coils
# start at 1, discrete inputs at 10001, input registers at 30001 and holding
# registers at 40001.
# The Modicon numbering meter manuals print. The classic five digit form
# leaves four digits for the address, so it only reaches address 9998; past
# that the six digit form is used, which has room for the whole 16 bit range.
CONVENTIONAL_BASE = {"coil": 1, "discrete": 10001, "input": 30001, "holding": 40001}
CONVENTIONAL_WIDE_BASE = {"coil": 1, "discrete": 100001, "input": 300001, "holding": 400001}
MAX_FIVE_DIGIT_ADDRESS = 9998


@dataclass
class RegisterSpec:
    """One register (or one contiguous block of registers) of the device."""

    name: str
    address: int
    table: str = "input"
    type: str = "float32"
    label: str = ""
    unit: str = ""
    mode: str = "random"
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    start: float | None = None
    interval: float = 30.0
    decimals: int | None = None
    scale: float = 1.0
    expression: str = ""
    rate: str = ""
    wrap: bool = False
    length: int = 0          # string types: number of characters
    writable: bool = False
    comment: str = ""
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_bit(self) -> bool:
        return self.table in BIT_TABLES

    @property
    def word_count(self) -> int:
        if self.is_bit:
            return 0
        if self.type == "string":
            return max(1, (self.length + 1) // 2)
        return NUMERIC_TYPES[self.type][0]

    @property
    def is_float(self) -> bool:
        return self.type in NUMERIC_TYPES and NUMERIC_TYPES[self.type][2]

    @property
    def addresses(self) -> range:
        return range(self.address, self.address + max(1, self.word_count))

    @property
    def conventional_address(self) -> int:
        """The 3x/4x number meter manuals print, e.g. holding 0 is 40001.

        The protocol carries a zero based address; the documentation for most
        devices numbers the same register from one, with a digit in front
        saying which table it lives in. Both refer to the same register, and
        which one a manual means is a common source of off-by-one wiring.

        Above address 9998 the five digit form has run out of room - holding
        0xFC00 would read as "104513", which looks like a coil and is not a
        number any manual prints - so the six digit form takes over and the
        same register reads as 464513.
        """
        if self.address > MAX_FIVE_DIGIT_ADDRESS:
            return CONVENTIONAL_WIDE_BASE[self.table] + self.address
        return CONVENTIONAL_BASE[self.table] + self.address

    def describe_address(self) -> str:
        """Address in the 3x/4x documentation style used by meter manuals."""
        return f"{self.address} (0x{self.address:04X} / {self.conventional_address})"

    def to_dict(self) -> dict:
        """The register as it is written in a profile file.

        Only what matters is emitted: anything still at its default is left
        out, so a saved file stays as readable as a hand written one.
        """
        out: dict[str, Any] = {"name": self.name, "table": self.table,
                               "address": f"0x{self.address:04X}"}
        if self.label and self.label != self.name.replace("_", " ").capitalize():
            out["label"] = self.label
        if not self.is_bit:
            out["type"] = self.type
        if self.unit:
            out["unit"] = self.unit
        out["mode"] = self.mode
        for key, value in (("min", self.minimum), ("max", self.maximum),
                           ("step", self.step), ("start", self.start),
                           ("decimals", self.decimals)):
            if value is not None:
                out[key] = value
        if self.scale != 1.0:
            out["scale"] = self.scale
        if self.expression:
            out["expression"] = self.expression
        if self.rate:
            out["rate"] = self.rate
        if self.wrap:
            out["wrap"] = True
        if self.length:
            out["length"] = self.length
        if self.comment:
            out["comment"] = self.comment
        out["interval"] = self.interval
        return out


@dataclass
class DeviceProfile:
    name: str = "Unnamed device"
    model: str = ""
    vendor: str = ""
    description: str = ""
    unit_id: int = 1
    default_interval: float = 30.0
    word_order: str = "big"
    byte_order: str = "big"
    gap_policy: str = "zero"
    accept_any_unit_id: bool = False
    registers: list[RegisterSpec] = field(default_factory=list)
    source_path: str = ""

    def by_name(self, name: str) -> RegisterSpec | None:
        for reg in self.registers:
            if reg.name == name:
                return reg
        return None

    @property
    def title(self) -> str:
        return f"{self.name} ({self.model})" if self.model else self.name

    def to_dict(self) -> dict:
        """The whole profile as it is written to a file."""
        device: dict[str, Any] = {"name": self.name}
        for key, value, default in (("model", self.model, ""),
                                    ("vendor", self.vendor, ""),
                                    ("description", self.description, "")):
            if value != default:
                device[key] = value
        device["unit_id"] = self.unit_id
        device["default_interval"] = self.default_interval
        device["word_order"] = self.word_order
        device["byte_order"] = self.byte_order
        device["gap_policy"] = self.gap_policy
        if self.accept_any_unit_id:
            device["accept_any_unit_id"] = True
        return {"device": device, "registers": [r.to_dict() for r in self.registers]}


def _as_number(value: Any, ctx: str, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{ctx}: '{key}' must be a number, got {value!r}")
    return float(value)


def _as_address(value: Any, ctx: str) -> int:
    """Accept 12, 0x000C or "0x000C"."""
    if isinstance(value, bool):
        raise ProfileError(f"{ctx}: 'address' must be a number")
    if isinstance(value, int):
        address = value
    elif isinstance(value, str):
        try:
            address = int(value, 0)
        except ValueError:
            raise ProfileError(f"{ctx}: cannot parse address {value!r}") from None
    else:
        raise ProfileError(f"{ctx}: 'address' must be a number, got {value!r}")
    if not 0 <= address <= 0xFFFF:
        raise ProfileError(f"{ctx}: address {address} outside 0..65535")
    return address


def _parse_register(raw: Any, index: int, defaults: DeviceProfile) -> RegisterSpec:
    if not isinstance(raw, dict):
        raise ProfileError(f"registers[{index}]: expected a mapping, got {type(raw).__name__}")

    name = str(raw.get("name") or "").strip()
    ctx = f"register '{name}'" if name else f"registers[{index}]"
    if not name:
        raise ProfileError(f"{ctx}: 'name' is required")
    if not IDENTIFIER_RE.fullmatch(name):
        raise ProfileError(
            f"{ctx}: 'name' must be a plain identifier (letters, digits, underscore) "
            "so it can be used inside expressions"
        )
    if "address" not in raw:
        raise ProfileError(f"{ctx}: 'address' is required")

    table = str(raw.get("table", "input")).lower()
    if table not in TABLES:
        raise ProfileError(f"{ctx}: unknown table {table!r}, expected one of {', '.join(TABLES)}")

    reg_type = str(raw.get("type", "bool" if table in BIT_TABLES else "float32")).lower()
    if table in BIT_TABLES:
        reg_type = "bool"
    elif reg_type not in NUMERIC_TYPES and reg_type != "string":
        raise ProfileError(
            f"{ctx}: unknown type {reg_type!r}, expected one of "
            f"{', '.join(sorted(NUMERIC_TYPES))}, string"
        )

    mode = str(raw.get("mode", "random")).lower()
    if mode not in MODES:
        raise ProfileError(f"{ctx}: unknown mode {mode!r}, expected one of {', '.join(MODES)}")

    spec = RegisterSpec(
        name=name,
        address=_as_address(raw["address"], ctx),
        table=table,
        type=reg_type,
        label=str(raw.get("label") or name.replace("_", " ").capitalize()),
        unit=str(raw.get("unit", "")),
        mode=mode,
        interval=float(raw.get("interval", defaults.default_interval)),
        scale=float(raw.get("scale", 1.0)),
        expression=str(raw.get("expression", "")).strip(),
        rate=str(raw.get("rate", "")).strip(),
        wrap=bool(raw.get("wrap", False)),
        length=int(raw.get("length", 0)),
        comment=str(raw.get("comment", "")),
        writable=bool(raw.get("writable", table in ("holding", "coil"))),
    )

    if "min" in raw:
        spec.minimum = _as_number(raw["min"], ctx, "min")
    if "max" in raw:
        spec.maximum = _as_number(raw["max"], ctx, "max")
    if "step" in raw:
        spec.step = _as_number(raw["step"], ctx, "step")
    if "decimals" in raw:
        spec.decimals = int(raw["decimals"])

    start = raw.get("start", raw.get("value", raw.get("default")))
    if start is not None:
        if spec.type == "string":
            spec.start = start
        elif isinstance(start, bool):
            spec.start = float(start)
        else:
            spec.start = _as_number(start, ctx, "start")

    if spec.interval <= 0:
        raise ProfileError(f"{ctx}: 'interval' must be greater than 0")
    if spec.scale == 0:
        raise ProfileError(f"{ctx}: 'scale' must not be 0")
    if spec.type == "string" and spec.length <= 0:
        raise ProfileError(f"{ctx}: string registers need a 'length' (characters)")
    if spec.minimum is not None and spec.maximum is not None and spec.minimum > spec.maximum:
        raise ProfileError(f"{ctx}: 'min' ({spec.minimum}) is greater than 'max' ({spec.maximum})")

    if mode in ("walk", "random") and spec.type != "string" and table not in BIT_TABLES:
        if spec.minimum is None or spec.maximum is None:
            raise ProfileError(f"{ctx}: mode '{mode}' needs both 'min' and 'max'")
    if mode == "expression" and not spec.expression:
        raise ProfileError(f"{ctx}: mode 'expression' needs an 'expression'")
    if mode == "accumulator" and not spec.rate:
        raise ProfileError(f"{ctx}: mode 'accumulator' needs a 'rate' (units per second)")

    if spec.step is None and spec.minimum is not None and spec.maximum is not None:
        spec.step = (spec.maximum - spec.minimum) / 20.0 or 1.0

    return spec


def _resolve_dependencies(profile: DeviceProfile) -> None:
    """Vet every expression, and record which registers each one reads.

    The vetting happens here, at load time, so a profile carrying an
    expression that is not allowed is refused whole - it never reaches the
    simulation, and the editor's Check button reports it like any other
    mistake. A profile is code, and profiles get shared.
    """
    names = {reg.name for reg in profile.registers}
    for reg in profile.registers:
        for label, source in (("expression", reg.expression), ("rate", reg.rate)):
            if not str(source).strip():
                continue
            try:
                check_expression(str(source), names)
            except ExpressionError as exc:
                raise ProfileError(
                    f"register '{reg.name}': {label} {source!r} is not allowed: {exc}"
                ) from None

        source = f"{reg.expression} {reg.rate}"
        if not source.strip():
            continue
        found = {token for token in IDENTIFIER_RE.findall(source) if token in names}
        found.discard(reg.name)  # self reference (peak hold) is not a dependency
        reg.depends_on = tuple(sorted(found))


def _check_overlaps(profile: DeviceProfile) -> None:
    seen: dict[tuple[str, int], str] = {}
    for reg in profile.registers:
        if reg.is_bit:
            slots = [reg.address]
        else:
            slots = list(reg.addresses)
        for address in slots:
            key = (reg.table, address)
            if key in seen:
                raise ProfileError(
                    f"register '{reg.name}' overlaps '{seen[key]}' at {reg.table} "
                    f"address {address} (0x{address:04X})"
                )
            seen[key] = reg.name


def save_profile(profile: DeviceProfile, path: str) -> None:
    """Write a profile to .yaml or .json, chosen by the file extension.

    YAML needs PyYAML, which is optional, so a .json profile is offered as the
    way out when it is missing - load_profile reads either.
    """
    data = profile.to_dict()
    if path.lower().endswith(".json"):
        text = json.dumps(data, indent=2) + "\n"
    else:
        if yaml is None:
            raise ProfileError(
                "PyYAML is not installed, so a .yaml profile cannot be written - "
                "save it as .json instead, which needs nothing extra"
            )
        header = (f"# {profile.title}\n"
                  "# Written by CreaBus Tool. See devices/README.md for the full schema.\n")
        text = header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                                       default_flow_style=False)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def load_profile(path: str) -> DeviceProfile:
    """Read a device profile from a .yaml / .yml / .json file."""
    if not os.path.isfile(path):
        raise ProfileError(f"device profile not found: {path}")

    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    if path.lower().endswith(".json"):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProfileError(f"{path}: invalid JSON - {exc}") from None
    else:
        if yaml is None:
            raise ProfileError(
                "PyYAML is not installed - run 'pip install -r requirements.txt' "
                "or use a .json profile instead"
            )
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ProfileError(f"{path}: invalid YAML - {exc}") from None

    return parse_profile(raw, path)


def parse_profile(raw: Any, path: str = "<profile>") -> DeviceProfile:
    """Validate an already parsed profile mapping.

    Split out from load_profile so the editor can check what it is about to
    write without going near the disk, and get exactly the same errors a file
    would have produced.
    """
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: top level must be a mapping with 'device' and 'registers'")

    device = raw.get("device") or {}
    if not isinstance(device, dict):
        raise ProfileError(f"{path}: 'device' must be a mapping")

    profile = DeviceProfile(
        name=str(device.get("name", os.path.splitext(os.path.basename(path))[0])),
        model=str(device.get("model", "")),
        vendor=str(device.get("vendor", "")),
        description=str(device.get("description", "")),
        unit_id=int(device.get("unit_id", 1)),
        default_interval=float(device.get("default_interval", 30.0)),
        word_order=str(device.get("word_order", "big")).lower(),
        byte_order=str(device.get("byte_order", "big")).lower(),
        gap_policy=str(device.get("gap_policy", "zero")).lower(),
        accept_any_unit_id=bool(device.get("accept_any_unit_id", False)),
        source_path=os.path.abspath(path) if os.path.isfile(path) else "",
    )

    if profile.word_order not in ("big", "little"):
        raise ProfileError(f"{path}: 'word_order' must be 'big' or 'little'")
    if profile.byte_order not in ("big", "little"):
        raise ProfileError(f"{path}: 'byte_order' must be 'big' or 'little'")
    if profile.gap_policy not in ("zero", "exception"):
        raise ProfileError(f"{path}: 'gap_policy' must be 'zero' or 'exception'")
    if not 0 <= profile.unit_id <= 255:
        raise ProfileError(f"{path}: 'unit_id' must be between 0 and 255")
    if profile.default_interval <= 0:
        raise ProfileError(f"{path}: 'default_interval' must be greater than 0")

    registers = raw.get("registers")
    if not isinstance(registers, list) or not registers:
        raise ProfileError(f"{path}: 'registers' must be a non-empty list")

    names: set[str] = set()
    for index, entry in enumerate(registers):
        spec = _parse_register(entry, index, profile)
        if spec.name in names:
            raise ProfileError(f"{path}: duplicate register name '{spec.name}'")
        names.add(spec.name)
        profile.registers.append(spec)

    _check_overlaps(profile)
    _resolve_dependencies(profile)
    return profile


def discover_profiles(directory: str) -> list[str]:
    """All profile files in a directory, sorted by file name."""
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, entry)
        for entry in sorted(os.listdir(directory))
        if entry.lower().endswith((".yaml", ".yml", ".json"))
    ]
