# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Server (slave) side: answer Modbus requests over TCP or over a serial port.

The request handling lives in :class:`RequestProcessor` and knows nothing about
transports. :class:`TcpServer` wraps it in the MBAP header of Modbus TCP,
:class:`RtuServer` wraps it in the CRC framing of Modbus RTU. Both expose the
same interface so the UI can treat them interchangeably.

Function codes answered:
    0x01 read coils            0x02 read discrete inputs
    0x03 read holding regs     0x04 read input registers
    0x05 write single coil     0x06 write single register
    0x0F write multiple coils  0x10 write multiple registers
    0x11 report server id
Everything else replies with exception 0x01 (illegal function).
"""

from __future__ import annotations

import os
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .datastore import DataStore
from .pdu import (
    FUNCTION_NAMES,
    GATEWAY_TARGET_FAILED,
    ILLEGAL_DATA_VALUE,
    ILLEGAL_FUNCTION,
    MAX_WRITE_BITS,
    MAX_WRITE_REGISTERS,
    SERVER_FAILURE,
    ModbusError,
    describe_request,
    pack_bits,
    request_target,
)
from .rtu import RtuListener, SerialSettings


@dataclass
class Stats:
    requests: int = 0
    exceptions: int = 0
    writes: int = 0
    connections: int = 0
    active: int = 0
    started_at: float = 0.0
    last_request: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "requests": self.requests,
                "exceptions": self.exceptions,
                "writes": self.writes,
                "connections": self.connections,
                "active": self.active,
                "started_at": self.started_at,
                "last_request": self.last_request,
            }


@dataclass
class ClientInfo:
    """Live view of one client, shown in the Connections tab."""

    peer: str
    connected_at: float
    requests: int = 0
    exceptions: int = 0
    last_seen: float = 0.0
    last_operation: str = ""      # e.g. "FC04 read input registers @ 0 x40"
    last_result: str = ""         # what it got back, or the exception

    @property
    def address(self) -> str:
        return self.peer.rsplit(":", 1)[0]


class RequestProcessor:
    """Turns a request PDU into a response PDU. Transport independent."""

    def __init__(
        self,
        store: DataStore,
        on_event: Callable[..., None] | None = None,
        log_requests: bool = True,
        response_delay: float = 0.0,
        silent_on_wrong_unit: bool = False,
    ):
        self.store = store
        self.stats = Stats()
        self.log_requests = log_requests
        self.response_delay = response_delay
        # A serial bus must stay quiet when the request is for another slave,
        # otherwise the reply would collide with that slave's. TCP is point to
        # point, so answering with exception 0x0B is friendlier there.
        self.silent_on_wrong_unit = silent_on_wrong_unit
        self._on_event = on_event or (lambda *a, **k: None)

    def event(self, kind: str, message: str, **details) -> None:
        self._on_event(kind, message, **details)

    def process(self, pdu: bytes, unit: int, client: ClientInfo) -> bytes | None:
        if not pdu:
            return None
        function = pdu[0]
        peer = client.peer
        profile = self.store.profile
        client.requests += 1
        client.last_seen = time.time()
        with self.stats.lock:
            self.stats.requests += 1
            self.stats.last_request = time.time()

        broadcast = unit == 0 and self.silent_on_wrong_unit
        if unit != profile.unit_id and not profile.accept_any_unit_id and not broadcast:
            if self.silent_on_wrong_unit:
                self.event("disconn", f"ignored a request for unit id {unit} "
                                      f"(this device answers on {profile.unit_id})")
                return None
            self.event(
                "warn",
                f"{peer} asked for unit id {unit} but this device answers on "
                f"{profile.unit_id} - replying with exception 0x0B",
            )
            client.exceptions += 1
            client.last_operation = describe_request(function, pdu)
            client.last_result = f"exception 0x0B - wrong unit id ({unit})"
            with self.stats.lock:
                self.stats.exceptions += 1
            return self._exception(function, GATEWAY_TARGET_FAILED)

        operation = describe_request(function, pdu)
        client.last_operation = operation
        if self.response_delay > 0:
            time.sleep(self.response_delay)

        try:
            response, summary = self._dispatch(function, pdu, peer)
        except ModbusError as exc:
            client.exceptions += 1
            client.last_result = exc.describe()
            with self.stats.lock:
                self.stats.exceptions += 1
            address, count = request_target(function, pdu)
            self.event(
                "error", f"{peer} {operation} -> exception {exc.describe()}",
                peer=peer, function=function, name=FUNCTION_NAMES.get(function, "unknown"),
                address=address, count=count, failed=True, result=exc.describe(),
            )
            return None if broadcast else self._exception(function, exc.code)
        except Exception as exc:
            client.exceptions += 1
            client.last_result = f"{type(exc).__name__}: {exc}"
            with self.stats.lock:
                self.stats.exceptions += 1
            address, count = request_target(function, pdu)
            self.event("error", f"{peer} {operation} -> {type(exc).__name__}: {exc}",
                       peer=peer, function=function, name=FUNCTION_NAMES.get(function, "unknown"),
                       address=address, count=count, failed=True,
                       result=f"{type(exc).__name__}: {exc}")
            return None if broadcast else self._exception(function, SERVER_FAILURE)

        client.last_result = summary
        address, count = request_target(function, pdu)
        self.event("request", f"{peer} {operation} -> {summary}",
                   peer=peer, function=function, name=FUNCTION_NAMES.get(function, "unknown"),
                   address=address, count=count, failed=False, result=summary,
                   quiet=not self.log_requests)
        return None if broadcast else response

    @staticmethod
    def _exception(function: int, code: int) -> bytes:
        return struct.pack(">BB", function | 0x80, code)

    def _dispatch(self, function: int, pdu: bytes, peer: str = "") -> tuple[bytes, str]:
        if function in (0x01, 0x02):
            table = "coil" if function == 0x01 else "discrete"
            address, count = self._unpack(pdu)
            bits = self.store.read_bits(table, address, count)
            payload = pack_bits(bits)
            self.store.note_access(table, address, count, peer)
            summary = self.store.describe_range(table, address, count)
            return struct.pack(">BB", function, len(payload)) + payload, summary

        if function in (0x03, 0x04):
            table = "holding" if function == 0x03 else "input"
            address, count = self._unpack(pdu)
            words = self.store.read_words(table, address, count)
            payload = b"".join(struct.pack(">H", w) for w in words)
            self.store.note_access(table, address, count, peer)
            summary = self.store.describe_range(table, address, count)
            return struct.pack(">BB", function, len(payload)) + payload, summary

        if function == 0x05:
            address, value = self._unpack(pdu)
            if value not in (0x0000, 0xFF00):
                raise ModbusError(ILLEGAL_DATA_VALUE,
                                  f"coil value must be 0x0000 or 0xFF00, got 0x{value:04X}")
            touched = self.store.write_bits(address, [value == 0xFF00])
            self.store.note_access("coil", address, 1, peer, writing=True)
            self._count_write()
            return pdu[:5], self._write_summary(touched)

        if function == 0x06:
            address, value = self._unpack(pdu)
            touched = self.store.write_words(address, [value])
            self.store.note_access("holding", address, 1, peer, writing=True)
            self._count_write()
            return pdu[:5], self._write_summary(touched)

        if function == 0x0F:
            address, count = self._unpack(pdu)
            if len(pdu) < 6:
                raise ModbusError(ILLEGAL_DATA_VALUE, "truncated request")
            byte_count = pdu[5]
            data = pdu[6:6 + byte_count]
            if (byte_count != len(data) or byte_count != (count + 7) // 8
                    or not 1 <= count <= MAX_WRITE_BITS):
                raise ModbusError(ILLEGAL_DATA_VALUE, "byte count does not match quantity")
            bits = [(data[i // 8] >> (i % 8)) & 1 == 1 for i in range(count)]
            touched = self.store.write_bits(address, bits)
            self.store.note_access("coil", address, count, peer, writing=True)
            self._count_write()
            return struct.pack(">BHH", function, address, count), self._write_summary(touched)

        if function == 0x10:
            address, count = self._unpack(pdu)
            if len(pdu) < 6:
                raise ModbusError(ILLEGAL_DATA_VALUE, "truncated request")
            byte_count = pdu[5]
            data = pdu[6:6 + byte_count]
            if (byte_count != len(data) or byte_count != count * 2
                    or not 1 <= count <= MAX_WRITE_REGISTERS):
                raise ModbusError(ILLEGAL_DATA_VALUE, "byte count does not match quantity")
            words = [int.from_bytes(data[i:i + 2], "big") for i in range(0, byte_count, 2)]
            touched = self.store.write_words(address, words)
            self.store.note_access("holding", address, count, peer, writing=True)
            self._count_write()
            return struct.pack(">BHH", function, address, count), self._write_summary(touched)

        if function == 0x11:
            profile = self.store.profile
            info = profile.title.encode("ascii", "replace")[:64]
            payload = bytes([profile.unit_id, 0xFF]) + info
            return struct.pack(">BB", function, len(payload)) + payload, profile.title

        raise ModbusError(ILLEGAL_FUNCTION, f"function 0x{function:02X} is not supported")

    def _count_write(self) -> None:
        with self.stats.lock:
            self.stats.writes += 1

    @staticmethod
    def _write_summary(touched) -> str:
        if not touched:
            return "written (no named register at that address)"
        return "written: " + ", ".join(f"{r.spec.label}={r.format_value()}" for r in touched)

    @staticmethod
    def _unpack(pdu: bytes) -> tuple[int, int]:
        """The address + quantity/value pair every request starts with."""
        if len(pdu) < 5:
            raise ModbusError(ILLEGAL_DATA_VALUE, "truncated request")
        return struct.unpack(">HH", pdu[1:5])


class DeviceEndpoint:
    """Common surface for the TCP and RTU servers, so the UI can swap them."""

    transport = "?"

    def __init__(self, processor: RequestProcessor, on_event: Callable[..., None] | None):
        self.processor = processor
        self._on_event = on_event or (lambda *a, **k: None)

    @property
    def store(self) -> DataStore:
        return self.processor.store

    @property
    def stats(self) -> Stats:
        return self.processor.stats

    @property
    def log_requests(self) -> bool:
        return self.processor.log_requests

    @log_requests.setter
    def log_requests(self, value: bool) -> None:
        self.processor.log_requests = value

    @property
    def response_delay(self) -> float:
        return self.processor.response_delay

    @response_delay.setter
    def response_delay(self, value: float) -> None:
        self.processor.response_delay = value

    def event(self, kind: str, message: str, **details) -> None:
        self._on_event(kind, message, **details)

    # -- provided by the transports ------------------------------------------
    @property
    def running(self) -> bool:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def clients(self) -> list[ClientInfo]:
        raise NotImplementedError

    def endpoint(self) -> str:
        raise NotImplementedError


# =============================================================================
#  Modbus TCP
# =============================================================================

class _Handler(socketserver.BaseRequestHandler):
    """One connected Modbus TCP client."""

    server: _TCPServer

    def setup(self) -> None:
        self.request.settimeout(0.5)
        try:
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        host, port = self.client_address[:2]
        self.peer = f"{host}:{port}"
        self.served = 0
        self.connected_at = time.monotonic()
        self.info = self.server.owner.register_client(self.request, self.peer)

    def handle(self) -> None:
        owner = self.server.owner
        buffer = b""
        while not owner.stopping:
            try:
                chunk = self.request.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if owner.log_frames:
                owner.event("frame", f"{self.peer} RX {chunk.hex(' ')}")
            while len(buffer) >= 7:
                transaction, protocol, length = struct.unpack(">HHH", buffer[:6])
                if protocol != 0:
                    owner.event("warn", f"{self.peer} not a Modbus frame "
                                        f"(protocol id {protocol}) - dropping connection")
                    return
                if not 2 <= length <= 254:
                    owner.event("warn", f"{self.peer} bad MBAP length {length} "
                                        "- dropping connection")
                    return
                if len(buffer) < 6 + length:
                    break                      # wait for the rest of the frame
                unit = buffer[6]
                pdu = buffer[7:6 + length]
                buffer = buffer[6 + length:]
                response = owner.processor.process(pdu, unit, self.info)
                if response is None:
                    continue
                frame = struct.pack(">HHHB", transaction, 0, len(response) + 1, unit) + response
                if owner.log_frames:
                    owner.event("frame", f"{self.peer} TX {frame.hex(' ')}")
                try:
                    self.request.sendall(frame)
                except OSError:
                    return
                self.served += 1

    def finish(self) -> None:
        owner = self.server.owner
        held = time.monotonic() - self.connected_at
        owner.unregister_client(self.request, self.peer, self.served, held)


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    # SO_REUSEADDR on Windows lets two sockets share a port silently, which
    # hides "port already in use" mistakes - only enable it elsewhere.
    allow_reuse_address = os.name != "nt"
    request_queue_size = 16

    def __init__(self, address, handler, owner: TcpServer):
        self.owner = owner
        super().__init__(address, handler)

    def verify_request(self, request, client_address) -> bool:
        limit = self.owner.max_clients
        if limit and self.owner.client_count() >= limit:
            self.owner.event(
                "warn",
                f"refused {client_address[0]}:{client_address[1]} - already at the "
                f"{limit} client limit",
            )
            return False
        return True

    def handle_error(self, request, client_address) -> None:
        import traceback
        self.owner.event("error", f"handler failure: {traceback.format_exc(limit=2).strip()}")


class TcpServer(DeviceEndpoint):
    """Modbus TCP endpoint for a DataStore."""

    transport = "tcp"

    def __init__(
        self,
        store: DataStore,
        host: str = "0.0.0.0",
        port: int = 502,
        on_event: Callable[..., None] | None = None,
        log_requests: bool = True,
        log_frames: bool = False,
        response_delay: float = 0.0,
        max_clients: int = 16,
    ):
        super().__init__(
            RequestProcessor(store, on_event, log_requests, response_delay,
                             silent_on_wrong_unit=False),
            on_event,
        )
        self.host = host
        self.port = port
        self.log_frames = log_frames
        self.max_clients = max_clients            # 0 = unlimited
        self.stopping = False
        self._server: _TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._clients: dict[socket.socket, ClientInfo] = {}
        self._clients_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._server is not None

    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def start(self) -> None:
        if self._server is not None:
            return
        self.stopping = False
        self._server = _TCPServer((self.host, self.port), _Handler, self)
        self.stats.started_at = time.time()
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
            name="modbus-tcp", daemon=True,
        )
        self._thread.start()
        bound = self._server.server_address
        self.event("info", f"Modbus TCP server listening on {bound[0]}:{bound[1]} "
                           f"(unit id {self.store.profile.unit_id}, "
                           f"device '{self.store.profile.title}')")

    def stop(self) -> None:
        if self._server is None:
            return
        self.stopping = True
        server, self._server = self._server, None
        server.shutdown()
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for sock in clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self.stats.lock:
            self.stats.active = 0
        self.event("info", "Modbus TCP server stopped")

    # -- client bookkeeping --------------------------------------------------
    def register_client(self, sock: socket.socket, peer: str) -> ClientInfo:
        info = ClientInfo(peer=peer, connected_at=time.time(), last_seen=time.time())
        with self._clients_lock:
            self._clients[sock] = info
            active = len(self._clients)
        with self.stats.lock:
            self.stats.connections += 1
            self.stats.active = active
        self.event("conn", f"client connected: {peer}  ({active} active)")
        return info

    def unregister_client(self, sock: socket.socket, peer: str, served: int, held: float) -> None:
        with self._clients_lock:
            self._clients.pop(sock, None)
            active = len(self._clients)
        with self.stats.lock:
            self.stats.active = active
        self.event(
            "disconn",
            f"client disconnected: {peer}  ({served} request(s) in {held:.1f}s, {active} active)",
        )

    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def clients(self) -> list[ClientInfo]:
        with self._clients_lock:
            return sorted(self._clients.values(), key=lambda i: i.connected_at)


# =============================================================================
#  Modbus RTU
# =============================================================================

class RtuServer(DeviceEndpoint):
    """Modbus RTU endpoint: answers requests arriving on a serial port."""

    transport = "rtu"

    def __init__(
        self,
        store: DataStore,
        settings: SerialSettings,
        on_event: Callable[..., None] | None = None,
        log_requests: bool = True,
        log_frames: bool = False,
        response_delay: float = 0.0,
        port_factory: Callable[[], object] | None = None,
    ):
        super().__init__(
            RequestProcessor(store, on_event, log_requests, response_delay,
                             silent_on_wrong_unit=True),
            on_event,
        )
        self.settings = settings
        self.max_clients = 0                     # meaningless on a serial bus
        self._master: ClientInfo | None = None
        self._listener = RtuListener(
            settings, self._on_request, on_event=on_event,
            port_factory=port_factory, log_frames=log_frames,
        )

    @property
    def log_frames(self) -> bool:
        return self._listener.log_frames

    @log_frames.setter
    def log_frames(self, value: bool) -> None:
        self._listener.log_frames = value

    @property
    def running(self) -> bool:
        return self._listener.running

    def endpoint(self) -> str:
        return self.settings.describe()

    def start(self) -> None:
        self._listener.start()
        self.stats.started_at = time.time()

    def stop(self) -> None:
        self._listener.stop()
        self._master = None
        with self.stats.lock:
            self.stats.active = 0

    def clients(self) -> list[ClientInfo]:
        return [self._master] if self._master is not None else []

    def client_count(self) -> int:
        return 1 if self._master is not None else 0

    @property
    def rtu_stats(self):
        return self._listener.stats

    def _on_request(self, pdu: bytes, unit: int) -> bytes | None:
        """Called by the listener thread for every valid frame off the wire."""
        if self._master is None:
            self._master = ClientInfo(
                peer=f"bus master on {self.settings.port}",
                connected_at=time.time(), last_seen=time.time(),
            )
            with self.stats.lock:
                self.stats.connections += 1
                self.stats.active = 1
            self.event("conn", "first request seen from the bus master on "
                               f"{self.settings.describe()}")
        return self.processor.process(pdu, unit, self._master)


# =============================================================================
#  the value simulation
# =============================================================================

class SimulationRunner:
    """Background thread that keeps the dummy values moving."""

    def __init__(self, store: DataStore, tick: float = 0.25,
                 on_change: Callable[[list], None] | None = None):
        self.store = store
        self.tick = tick
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="simulation", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            changed = self.store.tick()
            if changed and self._on_change is not None:
                self._on_change(changed)
            self._stop.wait(self.tick)


def local_addresses() -> list[str]:
    """Bind addresses worth offering in the UI."""
    found = ["0.0.0.0", "127.0.0.1"]
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))          # no traffic, just a route lookup
        address = probe.getsockname()[0]
        probe.close()
        if address not in found:
            found.append(address)
    except OSError:
        pass
    return found
