# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Tiny dependency-free Modbus TCP client, handy for checking the server.

    python tools/read_meter.py                       # poll every register of a profile
    python tools/read_meter.py --host 127.0.0.1 --port 5020 --watch 2
    python tools/read_meter.py --raw input 0 20      # dump a raw register range
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool.datastore import decode_words
from creabus_tool.profile import discover_profiles, load_profile

FUNCTION_FOR_TABLE = {"input": 4, "holding": 3, "coil": 1, "discrete": 2}


class ModbusClient:
    def __init__(self, host: str, port: int, unit: int, timeout: float = 3.0):
        self.unit = unit
        self.transaction = 0
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        self.sock.close()

    def _recv(self, size: int) -> bytes:
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("server closed the connection")
            data += chunk
        return data

    def request(self, pdu: bytes) -> bytes:
        self.transaction = (self.transaction + 1) & 0xFFFF
        frame = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, self.unit) + pdu
        self.sock.sendall(frame)
        header = self._recv(7)
        _, _, length = struct.unpack(">HHH", header[:6])
        body = self._recv(length - 1)
        if body[0] & 0x80:
            raise RuntimeError(f"modbus exception 0x{body[1]:02X} "
                               f"for function 0x{body[0] & 0x7F:02X}")
        return body

    def read(self, table: str, address: int, count: int) -> list[int]:
        function = FUNCTION_FOR_TABLE[table]
        body = self.request(struct.pack(">BHH", function, address, count))
        payload = body[2:]
        if function in (3, 4):
            return [int.from_bytes(payload[i:i + 2], "big") for i in range(0, len(payload), 2)]
        return [(payload[i // 8] >> (i % 8)) & 1 for i in range(count)]

    def write_registers(self, address: int, words: list[int]) -> None:
        data = b"".join(struct.pack(">H", w) for w in words)
        self.request(struct.pack(">BHHB", 0x10, address, len(words), len(data)) + data)

    def report_server_id(self) -> str:
        body = self.request(bytes([0x11]))
        return body[4:].decode("ascii", "replace")


def poll_profile(client: ModbusClient, profile) -> None:
    print(f"\n{time.strftime('%H:%M:%S')}  {profile.title}")
    print(f"{'register':<34}{'address':>18}{'value':>16}  unit")
    print("-" * 78)
    for spec in profile.registers:
        try:
            if spec.table in ("coil", "discrete"):
                bits = client.read(spec.table, spec.address, 1)
                value = "ON" if bits[0] else "OFF"
            else:
                words = client.read(spec.table, spec.address, spec.word_count)
                raw = decode_words(spec, words, profile.word_order, profile.byte_order)
                decimals = (spec.decimals if spec.decimals is not None
                            else (3 if spec.is_float else 0))
                value = f"{raw:.{decimals}f}" if isinstance(raw, float) else str(raw)
        except RuntimeError as exc:
            value = f"! {exc}"
        print(f"{spec.label:<34}{spec.describe_address():>18}{value:>16}  {spec.unit}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=None)
    parser.add_argument("--device", default="", help="profile used to decode the registers")
    parser.add_argument("--watch", type=float, default=0.0,
                        help="repeat every N seconds instead of polling once")
    parser.add_argument("--raw", nargs=3, metavar=("TABLE", "ADDRESS", "COUNT"),
                        help="dump a raw register range, e.g. --raw input 0 20")
    args = parser.parse_args()

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = args.device
    if not path:
        candidates = discover_profiles(os.path.join(here, "devices"))
        path = next((c for c in candidates if "sdm120" in os.path.basename(c).lower()),
                    candidates[0] if candidates else "")
    profile = load_profile(path)
    unit = args.unit if args.unit is not None else profile.unit_id

    client = ModbusClient(args.host, args.port, unit)
    print(f"connected to {args.host}:{args.port} as unit {unit}")
    try:
        print(f"server id: {client.report_server_id()}")
    except RuntimeError as exc:
        print(f"server id: unavailable ({exc})")

    try:
        if args.raw:
            table, address, count = args.raw[0], int(args.raw[1], 0), int(args.raw[2], 0)
            words = client.read(table, address, count)
            for offset, word in enumerate(words):
                print(f"  {address + offset:>5} (0x{address + offset:04X})  "
                      f"0x{word:04X}  {word}")
            return 0
        while True:
            poll_profile(client, profile)
            if args.watch <= 0:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
