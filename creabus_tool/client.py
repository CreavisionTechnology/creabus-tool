# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Client (master) side: poll a real Modbus device over TCP or RTU.

The same device profile that describes a simulated device also says how to
decode a real one, so pointing the client at hardware fills the Registers tab
with named, scaled, correctly typed values instead of raw words.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .datastore import DataStore, decode_words, encode_value
from .pdu import (
    FUNCTION_FOR_TABLE,
    FUNCTION_NAMES,
    MAX_READ_BITS,
    MAX_READ_REGISTERS,
    ModbusError,
    TransportError,
    decode_bit_response,
    decode_register_response,
    describe_request,
    raise_for_exception,
    read_request,
    write_multiple_registers,
    write_single_coil,
    write_single_register,
)
from .profile import BIT_TABLES, DeviceProfile
from .rtu import RtuMasterTransport, SerialSettings

# reading one register at a time is slow, so neighbouring registers are merged
# into one request as long as the hole between them is smaller than this
MAX_BLOCK_GAP = 8

# Late replies to earlier requests are skipped, but a device that never sends a
# matching transaction id must not keep us reading for ever.
MAX_STALE_REPLIES = 8


class TcpMasterTransport:
    """Modbus TCP master: MBAP framing over one socket."""

    def __init__(self, host: str, port: int = 502, timeout: float = 2.0,
                 on_frame: Callable[[str, bytes], None] | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._on_frame = on_frame
        self.sock: socket.socket | None = None
        self.transaction = 0
        self.lock = threading.Lock()

    def describe(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def connected(self) -> bool:
        return self.sock is not None

    def open(self) -> None:
        if self.sock is not None:
            return
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self.sock = None
            raise TransportError(f"cannot connect to {self.describe()}: {exc}") from None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv_exactly(self, size: int) -> bytes:
        # close() can run on another thread (the UI stopping a poller that is
        # mid request), so work from one reference rather than re-reading
        # self.sock and finding None halfway through.
        sock = self.sock
        if sock is None:
            raise TransportError("the connection was closed")
        data = b""
        while len(data) < size:
            try:
                chunk = sock.recv(size - len(data))
            except socket.timeout:
                raise TransportError(f"no reply within {self.timeout:.2f}s") from None
            except OSError as exc:
                raise TransportError(f"connection lost: {exc}") from None
            if not chunk:
                raise TransportError("the device closed the connection")
            data += chunk
        return data

    def transact(self, pdu: bytes, unit: int) -> bytes:
        with self.lock:
            if self.sock is None:
                self.open()
            self.transaction = (self.transaction + 1) & 0xFFFF
            frame = struct.pack(">HHHB", self.transaction, 0, len(pdu) + 1, unit) + pdu
            try:
                self.sock.sendall(frame)
            except OSError as exc:
                self.close()
                raise TransportError(f"send failed: {exc}") from None
            if self._on_frame is not None:
                self._on_frame("tx", frame)
            # A read that fails part way through leaves the stream in the
            # middle of a frame, so the socket is dropped rather than reused:
            # the next request reconnects instead of misreading the remainder.
            for _ in range(MAX_STALE_REPLIES + 1):
                try:
                    header = self._recv_exactly(7)
                    transaction, protocol, length = struct.unpack(">HHH", header[:6])
                    if protocol != 0 or not 2 <= length <= 254:
                        raise TransportError("the reply is not a Modbus TCP frame")
                    body = self._recv_exactly(length - 1)
                except TransportError:
                    self.close()
                    raise
                if self._on_frame is not None:
                    self._on_frame("rx", header + body)
                if transaction == self.transaction:
                    return body
                # a late reply to an earlier request: skip it and keep reading
            self.close()
            raise TransportError("no reply matched the request id")


@dataclass
class LinkStats:
    requests: int = 0
    replies: int = 0
    exceptions: int = 0
    timeouts: int = 0
    writes: int = 0
    started_at: float = 0.0
    last_request: float = 0.0
    connected_since: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "requests": self.requests, "replies": self.replies,
                "exceptions": self.exceptions, "timeouts": self.timeouts,
                "writes": self.writes, "started_at": self.started_at,
                "last_request": self.last_request, "connected_since": self.connected_since,
            }


@dataclass
class Block:
    """A contiguous range of registers read with one request."""

    table: str
    address: int
    count: int
    registers: list           # RegisterSpec objects inside this block


def plan_blocks(profile: DeviceProfile, max_gap: int = MAX_BLOCK_GAP) -> list[Block]:
    """Group a profile's registers into as few requests as the limits allow."""
    blocks: list[Block] = []
    for table in ("input", "holding", "coil", "discrete"):
        specs = sorted((s for s in profile.registers if s.table == table),
                       key=lambda s: s.address)
        limit = MAX_READ_BITS if table in BIT_TABLES else MAX_READ_REGISTERS
        current: Block | None = None
        for spec in specs:
            width = 1 if spec.table in BIT_TABLES else spec.word_count
            end = spec.address + width
            if current is not None:
                gap = spec.address - (current.address + current.count)
                if gap <= max_gap and end - current.address <= limit:
                    current.count = max(current.count, end - current.address)
                    current.registers.append(spec)
                    continue
            current = Block(table, spec.address, width, [spec])
            blocks.append(current)
    return blocks


class ModbusMaster:
    """Sends requests over whichever transport it was given."""

    def __init__(self, transport, unit: int = 1, retries: int = 1,
                 on_event: Callable[..., None] | None = None):
        self.transport = transport
        self.unit = unit
        self.retries = max(0, retries)
        self.stats = LinkStats()
        # One request at a time. The poller runs on its own thread and the
        # window reads single registers on another, and Modbus has no way to
        # tell two overlapping replies apart: on TCP the second reader takes
        # the first one's reply, and on RTU the two requests collide on the
        # wire. Held across the retry loop so a retry cannot be interleaved
        # either. Reentrant so a caller can hold it to group several requests.
        self.lock = threading.RLock()
        self._on_event = on_event or (lambda *a, **k: None)

    def describe(self) -> str:
        return self.transport.describe()

    @property
    def connected(self) -> bool:
        return getattr(self.transport, "connected", False)

    def close(self) -> None:
        self.transport.close()

    def request(self, pdu: bytes, describe: bool = True) -> bytes:
        """One request with retries. Raises TransportError or ModbusError."""
        with self.lock:
            return self._request(pdu, describe)

    def _request(self, pdu: bytes, describe: bool = True) -> bytes:
        function = pdu[0]
        label = describe_request(function, pdu)
        peer = self.transport.describe()
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            with self.stats.lock:
                self.stats.requests += 1
                self.stats.last_request = time.time()
            try:
                response = self.transport.transact(pdu, self.unit)
            except TransportError as exc:
                last_error = exc
                with self.stats.lock:
                    self.stats.timeouts += 1
                if attempt < self.retries:
                    self._on_event("warn", f"{peer} {label} -> {exc} (retrying)")
                    continue
                self._on_event("error", f"{peer} {label} -> {exc}",
                               peer=peer, function=function,
                               name=FUNCTION_NAMES.get(function, "unknown"),
                               address=None, count=None, failed=True, result=str(exc))
                raise
            with self.stats.lock:
                self.stats.replies += 1
            try:
                raise_for_exception(response)
            except ModbusError as exc:
                with self.stats.lock:
                    self.stats.exceptions += 1
                self._on_event("error", f"{peer} {label} -> exception {exc.describe()}",
                               peer=peer, function=function,
                               name=FUNCTION_NAMES.get(function, "unknown"),
                               address=None, count=None, failed=True, result=exc.describe())
                raise
            if describe:
                self._on_event("request", f"{peer} {label} -> {len(response)} byte reply",
                               peer=peer, function=function,
                               name=FUNCTION_NAMES.get(function, "unknown"),
                               address=None, count=None, failed=False,
                               result=f"{len(response)} byte reply", quiet=True)
            return response
        raise last_error if last_error else TransportError("request failed")

    # -- typed helpers --------------------------------------------------------
    def read_block(self, block: Block) -> list:
        function = FUNCTION_FOR_TABLE[block.table]
        response = self.request(read_request(function, block.address, block.count),
                                describe=False)
        if block.table in BIT_TABLES:
            return decode_bit_response(response, block.count)
        return decode_register_response(response)

    def write_register_value(self, profile: DeviceProfile, spec, value) -> None:
        """Write one profile register back to the device, in its own encoding."""
        if spec.table == "coil":
            self.request(write_single_coil(spec.address, bool(value)))
        elif spec.table == "holding":
            words = encode_value(spec, value, profile.word_order, profile.byte_order)
            if len(words) == 1:
                self.request(write_single_register(spec.address, words[0]))
            else:
                self.request(write_multiple_registers(spec.address, words))
        else:
            raise ModbusError(0x02, f"{spec.table} registers are read only")
        with self.stats.lock:
            self.stats.writes += 1


class Poller:
    """Polls a device on a timer and writes what it reads into a DataStore."""

    def __init__(
        self,
        master: ModbusMaster,
        store: DataStore,
        interval: float = 1.0,
        on_event: Callable[..., None] | None = None,
    ):
        self.master = master
        self.store = store
        self.interval = interval
        self.blocks = plan_blocks(store.profile)
        self.log_requests = True
        self._on_event = on_event or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.cycles = 0
        self.last_cycle_seconds = 0.0
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self.master.stats.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="modbus-poll", daemon=True)
        self._thread.start()
        self._on_event("info", f"polling {self.master.describe()} unit "
                               f"{self.master.unit} every {self.interval:g}s "
                               f"({len(self.blocks)} request(s) per cycle)")

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)
        self.master.close()
        self._on_event("info", "polling stopped")

    def poll_once(self) -> None:
        """One cycle: every block, decoded into the store."""
        profile = self.store.profile
        peer = self.master.describe()
        started = time.monotonic()
        for block in self.blocks:
            if self._stop.is_set():
                return
            function = FUNCTION_FOR_TABLE[block.table]
            label = describe_request(function, read_request(function, block.address,
                                                            block.count))
            try:
                data = self.master.read_block(block)
                self.last_error = ""
            except (TransportError, ModbusError) as exc:
                reason = exc.describe() if isinstance(exc, ModbusError) else str(exc)
                self.last_error = reason
                for spec in block.registers:
                    self.store.apply_poll(spec.name, None, peer, error=reason)
                self._on_event("error", f"{peer} {label} -> {reason}",
                               peer=peer, function=function,
                               name=FUNCTION_NAMES.get(function, "unknown"),
                               address=block.address, count=block.count,
                               failed=True, result=reason)
                if isinstance(exc, TransportError):
                    return          # the link is down, do not hammer it
                continue
            for spec in block.registers:
                offset = spec.address - block.address
                try:
                    if spec.table in BIT_TABLES:
                        value = data[offset]
                    else:
                        words = data[offset:offset + spec.word_count]
                        if len(words) < spec.word_count:
                            raise IndexError("short reply")
                        value = decode_words(spec, words, profile.word_order,
                                             profile.byte_order)
                    self.store.apply_poll(spec.name, value, peer)
                except (IndexError, ValueError, struct.error) as exc:
                    self.store.apply_poll(spec.name, None, peer, error=str(exc))
            summary = self.store.describe_range(block.table, block.address, block.count)
            self._on_event("request", f"{peer} {label} -> {summary}",
                           peer=peer, function=function,
                           name=FUNCTION_NAMES.get(function, "unknown"),
                           address=block.address, count=block.count,
                           failed=False, result=summary, quiet=not self.log_requests)
        self.cycles += 1
        self.last_cycle_seconds = time.monotonic() - started

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._on_event("error", f"poll cycle failed: {self.last_error}")
            wait = max(0.05, self.interval - (time.monotonic() - started))
            self._stop.wait(wait)


def make_master(
    link: str,
    unit: int,
    on_event: Callable[..., None] | None = None,
    on_frame: Callable[[str, bytes], None] | None = None,
    host: str = "127.0.0.1",
    port: int = 502,
    serial_settings: SerialSettings | None = None,
    timeout: float = 2.0,
    retries: int = 1,
) -> ModbusMaster:
    """Build a master for 'tcp' or 'rtu'."""
    if link == "rtu":
        if serial_settings is None:
            raise TransportError("no serial settings given for an RTU client")
        transport = RtuMasterTransport(serial_settings, timeout=timeout, on_frame=on_frame)
    else:
        transport = TcpMasterTransport(host, port, timeout=timeout, on_frame=on_frame)
    return ModbusMaster(transport, unit=unit, retries=retries, on_event=on_event)
