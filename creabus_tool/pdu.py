# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Protocol level pieces shared by every transport and by both roles.

A Modbus PDU (function code + data) is the same whether it travels inside a
TCP MBAP header or between two CRC bytes on an RS-485 pair. Everything in here
is transport independent; :mod:`creabus_tool.rtu` adds the serial framing and
:mod:`creabus_tool.server` / :mod:`creabus_tool.client` add the two roles.
"""

from __future__ import annotations

import struct

# --- function codes ----------------------------------------------------------
READ_COILS = 0x01
READ_DISCRETE_INPUTS = 0x02
READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_COIL = 0x05
WRITE_SINGLE_REGISTER = 0x06
WRITE_MULTIPLE_COILS = 0x0F
WRITE_MULTIPLE_REGISTERS = 0x10
REPORT_SERVER_ID = 0x11

FUNCTION_NAMES = {
    READ_COILS: "read coils",
    READ_DISCRETE_INPUTS: "read discrete inputs",
    READ_HOLDING_REGISTERS: "read holding registers",
    READ_INPUT_REGISTERS: "read input registers",
    WRITE_SINGLE_COIL: "write single coil",
    WRITE_SINGLE_REGISTER: "write single register",
    WRITE_MULTIPLE_COILS: "write multiple coils",
    WRITE_MULTIPLE_REGISTERS: "write multiple registers",
    REPORT_SERVER_ID: "report server id",
}

FUNCTION_FOR_TABLE = {
    "coil": READ_COILS,
    "discrete": READ_DISCRETE_INPUTS,
    "holding": READ_HOLDING_REGISTERS,
    "input": READ_INPUT_REGISTERS,
}

WRITE_FUNCTIONS = (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER,
                   WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS)

# --- exception codes ---------------------------------------------------------
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03
SERVER_FAILURE = 0x04
ACKNOWLEDGE = 0x05
SERVER_BUSY = 0x06
GATEWAY_PATH_UNAVAILABLE = 0x0A
GATEWAY_TARGET_FAILED = 0x0B

EXCEPTION_NAMES = {
    ILLEGAL_FUNCTION: "illegal function",
    ILLEGAL_DATA_ADDRESS: "illegal data address",
    ILLEGAL_DATA_VALUE: "illegal data value",
    SERVER_FAILURE: "server device failure",
    ACKNOWLEDGE: "acknowledge",
    SERVER_BUSY: "server device busy",
    GATEWAY_PATH_UNAVAILABLE: "gateway path unavailable",
    GATEWAY_TARGET_FAILED: "gateway target device failed to respond",
}

MAX_PDU_SIZE = 253          # Modbus application protocol limit
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123
MAX_READ_BITS = 2000
MAX_WRITE_BITS = 1968


class ModbusError(Exception):
    """A Modbus exception response (not a Python level failure)."""

    def __init__(self, code: int, reason: str = ""):
        super().__init__(reason or f"modbus exception 0x{code:02X}")
        self.code = code
        self.reason = reason

    def describe(self) -> str:
        name = EXCEPTION_NAMES.get(self.code, "unknown exception")
        return f"0x{self.code:02X} {name}" + (f": {self.reason}" if self.reason else "")


class TransportError(Exception):
    """The link failed: no reply, bad CRC, port gone, socket closed."""


# --- CRC-16/MODBUS -----------------------------------------------------------

def _build_crc_table() -> list[int]:
    table = []
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
        table.append(value)
    return table


_CRC_TABLE = _build_crc_table()


def crc16(data: bytes) -> int:
    """CRC-16/MODBUS, as it goes on the wire (low byte first)."""
    crc = 0xFFFF
    for byte in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


def append_crc(frame: bytes) -> bytes:
    return frame + struct.pack("<H", crc16(frame))


def check_crc(frame: bytes) -> bool:
    """True when the last two bytes are a valid CRC over the rest."""
    return len(frame) >= 3 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


# --- how long is this frame going to be? -------------------------------------
#  RTU has no length field, so a receiver either waits for 3.5 characters of
#  silence or works the length out from the function code. Doing both means a
#  fast answer for the codes we know and a safe fall back for the ones we do not.

def expected_request_length(buffer: bytes) -> int | None:
    """Total RTU request length (address..CRC), or None if unknown so far."""
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function in (READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS,
                    READ_INPUT_REGISTERS, WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER):
        return 8                                   # addr fc addr(2) qty(2) crc(2)
    if function in (WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS):
        if len(buffer) < 7:
            return None
        return 9 + buffer[6]                       # ... byte count + payload + crc
    if function == REPORT_SERVER_ID:
        return 4                                   # addr fc crc(2)
    return None                                    # unknown: fall back to silence


def expected_response_length(buffer: bytes) -> int | None:
    """Total RTU response length (address..CRC), or None if unknown so far."""
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function & 0x80:
        return 5                                   # addr fc|80 code crc(2)
    if function in (READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS,
                    READ_INPUT_REGISTERS, REPORT_SERVER_ID):
        if len(buffer) < 3:
            return None
        return 5 + buffer[2]                       # addr fc count payload crc(2)
    if function in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER,
                    WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS):
        return 8
    return None


# --- building requests (client side) -----------------------------------------

def read_request(function: int, address: int, count: int) -> bytes:
    return struct.pack(">BHH", function, address, count)


def write_single_register(address: int, value: int) -> bytes:
    return struct.pack(">BHH", WRITE_SINGLE_REGISTER, address, value & 0xFFFF)


def write_single_coil(address: int, on: bool) -> bytes:
    return struct.pack(">BHH", WRITE_SINGLE_COIL, address, 0xFF00 if on else 0x0000)


def write_multiple_registers(address: int, words: list[int]) -> bytes:
    payload = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
    return struct.pack(">BHHB", WRITE_MULTIPLE_REGISTERS, address,
                       len(words), len(payload)) + payload


def write_multiple_coils(address: int, bits: list[bool]) -> bytes:
    data = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            data[index // 8] |= 1 << (index % 8)
    return struct.pack(">BHHB", WRITE_MULTIPLE_COILS, address,
                       len(bits), len(data)) + bytes(data)


# --- reading responses (client side) -----------------------------------------

def raise_for_exception(pdu: bytes) -> None:
    if pdu and pdu[0] & 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        raise ModbusError(code, f"in reply to function 0x{pdu[0] & 0x7F:02X}")


def decode_register_response(pdu: bytes) -> list[int]:
    raise_for_exception(pdu)
    if len(pdu) < 2 or len(pdu) < 2 + pdu[1]:
        raise TransportError("truncated register response")
    byte_count = pdu[1]
    if byte_count % 2:
        # Registers are 16 bit, so an odd byte count cannot be right. Left
        # unchecked the trailing half word would decode as a plausible value.
        raise TransportError(f"register response has an odd byte count ({byte_count})")
    payload = pdu[2:2 + byte_count]
    return [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]


def decode_bit_response(pdu: bytes, count: int) -> list[bool]:
    raise_for_exception(pdu)
    if len(pdu) < 2 or len(pdu) < 2 + pdu[1]:
        raise TransportError("truncated bit response")
    payload = pdu[2:2 + pdu[1]]
    # The byte count has to cover every bit that was asked for; a device that
    # sends fewer would otherwise run this off the end of the payload.
    if len(payload) * 8 < count:
        raise TransportError(
            f"bit response carries {len(payload)} byte(s), too few for {count} bit(s)")
    return [(payload[i // 8] >> (i % 8)) & 1 == 1 for i in range(count)]


def pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (index % 8)
    return bytes(out)


def describe_request(function: int, pdu: bytes) -> str:
    """One line describing a request, for the log and the traffic table."""
    name = FUNCTION_NAMES.get(function, "unknown function")
    if function in (READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS,
                    READ_INPUT_REGISTERS, WRITE_MULTIPLE_COILS,
                    WRITE_MULTIPLE_REGISTERS) and len(pdu) >= 5:
        address, count = struct.unpack(">HH", pdu[1:5])
        return f"FC{function:02X} {name} @ {address} (0x{address:04X}) x{count}"
    if function in (WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER) and len(pdu) >= 5:
        address, value = struct.unpack(">HH", pdu[1:5])
        return f"FC{function:02X} {name} @ {address} (0x{address:04X}) = 0x{value:04X}"
    return f"FC{function:02X} {name}"


def request_target(function: int, pdu: bytes) -> tuple[int | None, int | None]:
    """The address and quantity a request refers to, if it has one."""
    if function in (READ_COILS, READ_DISCRETE_INPUTS, READ_HOLDING_REGISTERS,
                    READ_INPUT_REGISTERS, WRITE_SINGLE_COIL, WRITE_SINGLE_REGISTER,
                    WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS) and len(pdu) >= 5:
        return struct.unpack(">HH", pdu[1:5])
    return None, None
