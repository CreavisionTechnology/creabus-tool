# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Modbus RTU framing over a serial port, for both roles.

RTU has no length field: a frame ends when the line has been quiet for 3.5
character times. Waiting for that silence on every frame is slow, so the
receivers here work the length out from the function code (see
:func:`creabus_tool.pdu.expected_request_length`) and keep the silence rule as
the fall back for unknown codes and as the resynchronisation rule after a bad
CRC.

The serial port is reached through a small factory so the framing can be tested
against an in-memory loopback instead of real hardware.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .pdu import (
    TransportError,
    append_crc,
    check_crc,
    expected_request_length,
    expected_response_length,
)

PARITIES = ("none", "even", "odd")
COMMON_BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)

# expected_request_length / expected_response_length read at most the unit id,
# the function code and a byte count, so seven bytes is always enough for them.
_PREDICT_HEAD = 7

# The longest Modbus RTU frame is 256 bytes (address + 253 PDU + CRC). Twice
# that is room for a frame plus whatever preceded it; anything beyond is noise.
MAX_RTU_FRAME = 256
MAX_BUFFERED_BYTES = 2 * MAX_RTU_FRAME


def _pyserial():
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise TransportError(
            "Modbus RTU needs pyserial - run 'pip install pyserial' "
            "(it is included in requirements.txt)"
        ) from exc
    return serial


def list_serial_ports() -> list[tuple[str, str]]:
    """Every serial port on this machine as (device, description)."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    found = []
    for port in list_ports.comports():
        description = port.description or ""
        if port.manufacturer and port.manufacturer not in description:
            description = f"{description} - {port.manufacturer}"
        found.append((port.device, description.strip() or port.device))
    return sorted(found)


@dataclass
class SerialSettings:
    """Everything needed to open a serial port for Modbus RTU."""

    port: str = ""
    baudrate: int = 9600
    parity: str = "none"          # none | even | odd
    bytesize: int = 8
    stopbits: float = 1
    rtscts: bool = False
    dsrdtr: bool = False

    def describe(self) -> str:
        parity = {"none": "N", "even": "E", "odd": "O"}.get(self.parity, "N")
        stop = int(self.stopbits) if float(self.stopbits).is_integer() else self.stopbits
        return f"{self.port} {self.baudrate} {self.bytesize}{parity}{stop}"

    @property
    def bits_per_character(self) -> int:
        parity_bit = 0 if self.parity == "none" else 1
        return 1 + self.bytesize + parity_bit + round(self.stopbits)

    @property
    def silence_seconds(self) -> float:
        """3.5 character times; fixed at 1.75 ms above 19200 baud, per the spec."""
        if self.baudrate > 19200:
            return 0.00175
        return 3.5 * self.bits_per_character / float(self.baudrate)

    @property
    def character_seconds(self) -> float:
        return self.bits_per_character / float(self.baudrate)

    def open(self):
        """Open the real port. Raises TransportError with a readable message."""
        serial = _pyserial()
        parity = {"none": serial.PARITY_NONE, "even": serial.PARITY_EVEN,
                  "odd": serial.PARITY_ODD}[self.parity]
        stopbits = {1: serial.STOPBITS_ONE, 1.5: serial.STOPBITS_ONE_POINT_FIVE,
                    2: serial.STOPBITS_TWO}.get(self.stopbits, serial.STOPBITS_ONE)
        try:
            return serial.Serial(
                port=self.port, baudrate=self.baudrate, parity=parity,
                bytesize=self.bytesize, stopbits=stopbits,
                timeout=0.05, write_timeout=2.0,
                rtscts=self.rtscts, dsrdtr=self.dsrdtr,
            )
        except Exception as exc:
            raise TransportError(f"cannot open {self.port}: {exc}") from None


class SerialFramer:
    """Reads whole RTU frames off a byte stream."""

    def __init__(self, settings: SerialSettings, port, predict: Callable[[bytes], int | None]):
        self.settings = settings
        self.port = port
        self.predict = predict
        self.buffer = bytearray()
        self.last_byte_at = 0.0
        self.discarded = 0

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes; return every complete, CRC-valid frame in them."""
        if chunk:
            self.buffer += chunk
            self.last_byte_at = time.monotonic()
            # A frame is at most 256 bytes. More than that without one coming
            # out means noise the length rules cannot even measure - a wrong
            # baud rate, or a line that never goes quiet enough to resynchronise
            # on silence - so the oldest bytes are dropped rather than kept for
            # ever. Without this the buffer grows for as long as the noise does.
            if len(self.buffer) > MAX_BUFFERED_BYTES:
                excess = len(self.buffer) - MAX_BUFFERED_BYTES
                del self.buffer[:excess]
                self.discarded += 1
        return self._extract()

    def flush_on_silence(self) -> list[bytes]:
        """Called when the line has gone quiet: close off whatever is buffered."""
        if not self.buffer:
            return []
        if time.monotonic() - self.last_byte_at < self.settings.silence_seconds:
            return []
        if check_crc(bytes(self.buffer)):
            frames = [bytes(self.buffer)]
        else:
            # Not one clean frame. Rather than throw the lot away, look for
            # whole frames inside it: a noise burst arriving immediately before
            # a real request would otherwise take the request down with it, and
            # the sender - which cannot see that the line was dirty - would get
            # nothing back but a timeout.
            frames = self._salvage()
            if not frames and len(self.buffer) > 2:
                self.discarded += 1
        self.buffer.clear()
        return frames

    def _salvage(self) -> list[bytes]:
        """Whole, CRC-valid frames hiding inside a buffer that is not one."""
        found: list[bytes] = []
        start = 0
        while start < len(self.buffer):
            expected = self.predict(bytes(self.buffer[start:start + _PREDICT_HEAD]))
            if expected is not None and start + expected <= len(self.buffer):
                candidate = bytes(self.buffer[start:start + expected])
                if check_crc(candidate):
                    found.append(candidate)
                    start += expected
                    continue
            start += 1
        if found:
            self.discarded += 1          # whatever surrounded them was noise
        return found

    def _extract(self) -> list[bytes]:
        frames = []
        while True:
            # The length rules never look past the byte count field, so only
            # the head is handed over. Copying the whole buffer here would make
            # a byte-at-a-time resynchronisation quadratic in the noise.
            expected = self.predict(bytes(self.buffer[:_PREDICT_HEAD]))
            if expected is None:
                # The head does not parse as anything. That is allowed to be a
                # frame whose function code we do not know, which the silence
                # rule will close off - but only until more than a whole frame
                # has piled up behind it, because no frame is longer than that.
                # Past that point the line is babbling and silence may never
                # come, so skip ahead to something that does parse.
                if len(self.buffer) <= MAX_RTU_FRAME:
                    return frames
                offset = self._next_parseable_offset()
                self.discarded += 1
                if offset is None:
                    # Nothing in the buffer can begin a frame. Keep only the
                    # newest bytes, which may be a frame still arriving.
                    del self.buffer[:len(self.buffer) - MAX_RTU_FRAME]
                    return frames
                del self.buffer[:offset]
                continue
            if len(self.buffer) < expected:
                return frames
            candidate = bytes(self.buffer[:expected])
            if check_crc(candidate):
                del self.buffer[:expected]
                frames.append(candidate)
                continue
            # bad CRC: drop one byte and try to resynchronise
            self.discarded += 1
            del self.buffer[:1]
            if not self.buffer:
                return frames

    def _next_parseable_offset(self) -> int | None:
        """The first offset after 0 whose head the length rules can measure."""
        for offset in range(1, len(self.buffer)):
            if self.predict(bytes(self.buffer[offset:offset + _PREDICT_HEAD])) is not None:
                return offset
        return None

    def reset(self) -> None:
        self.buffer.clear()


def read_available(port) -> bytes:
    """Read whatever is waiting.

    Blocks at most as long as the port's own timeout, which is set when the
    port is opened, waiting for the first byte; everything already buffered
    then comes back with it.
    """
    waiting = getattr(port, "in_waiting", 0)
    if waiting:
        return port.read(waiting)
    first = port.read(1)
    if not first:
        return b""
    waiting = getattr(port, "in_waiting", 0)
    return first + (port.read(waiting) if waiting else b"")


@dataclass
class RtuStats:
    frames_in: int = 0
    frames_out: int = 0
    crc_errors: int = 0
    not_for_us: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class RtuListener:
    """Server side: answers Modbus RTU requests arriving on a serial port.

    `on_request(pdu, unit)` returns the response PDU, or None to stay silent
    (a broadcast, or a request addressed to another slave).
    """

    def __init__(
        self,
        settings: SerialSettings,
        on_request: Callable[[bytes, int], bytes | None],
        on_event: Callable[..., None] | None = None,
        port_factory: Callable[[], object] | None = None,
        log_frames: bool = False,
    ):
        self.settings = settings
        self.on_request = on_request
        self._on_event = on_event or (lambda *a, **k: None)
        self._port_factory = port_factory or settings.open
        self.log_frames = log_frames
        self.stats = RtuStats()
        self.port = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self.port = self._port_factory()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="modbus-rtu", daemon=True)
        self._thread.start()
        self._on_event("info", f"Modbus RTU listening on {self.settings.describe()} "
                               f"(silence {self.settings.silence_seconds * 1000:.2f} ms)")

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None
        self._on_event("info", "Modbus RTU listener stopped")

    def _run(self) -> None:
        framer = SerialFramer(self.settings, self.port, expected_request_length)
        while not self._stop.is_set():
            try:
                chunk = read_available(self.port)
            except Exception as exc:
                if not self._stop.is_set():
                    self._on_event("error", f"serial read failed: {exc}")
                return
            discarded_before = framer.discarded
            frames = framer.feed(chunk) if chunk else framer.flush_on_silence()
            for frame in frames:
                self._handle(frame)
            dropped = framer.discarded - discarded_before
            if dropped:
                with self.stats.lock:
                    self.stats.crc_errors += dropped
                self._on_event("warn", f"discarded {dropped} byte(s)/frame(s) with a bad CRC")

    def _handle(self, frame: bytes) -> None:
        if self.log_frames:
            self._on_event("frame", f"RTU RX {frame.hex(' ')}")
        with self.stats.lock:
            self.stats.frames_in += 1
        unit = frame[0]
        pdu = frame[1:-2]
        response = self.on_request(pdu, unit)
        if response is None:
            with self.stats.lock:
                self.stats.not_for_us += 1
            return
        reply = append_crc(bytes([unit]) + response)
        try:
            self.port.write(reply)
            flush = getattr(self.port, "flush", None)
            if flush is not None:
                flush()
        except Exception as exc:
            self._on_event("error", f"serial write failed: {exc}")
            return
        with self.stats.lock:
            self.stats.frames_out += 1
        if self.log_frames:
            self._on_event("frame", f"RTU TX {reply.hex(' ')}")


class RtuMasterTransport:
    """Client side: sends a request on a serial port and waits for the reply."""

    def __init__(
        self,
        settings: SerialSettings,
        timeout: float = 1.0,
        port_factory: Callable[[], object] | None = None,
        on_frame: Callable[[str, bytes], None] | None = None,
    ):
        self.settings = settings
        self.timeout = timeout
        self._port_factory = port_factory or settings.open
        self._on_frame = on_frame
        self.port = None
        self.lock = threading.Lock()

    def describe(self) -> str:
        return self.settings.describe()

    def open(self) -> None:
        if self.port is None:
            self.port = self._port_factory()

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None

    @property
    def connected(self) -> bool:
        return self.port is not None

    def transact(self, pdu: bytes, unit: int) -> bytes:
        """Send one request, return the response PDU. Raises TransportError."""
        with self.lock:
            if self.port is None:
                self.open()
            frame = append_crc(bytes([unit]) + pdu)
            try:
                reset = getattr(self.port, "reset_input_buffer", None)
                if reset is not None:
                    reset()
                self.port.write(frame)
                flush = getattr(self.port, "flush", None)
                if flush is not None:
                    flush()
            except Exception as exc:
                self.close()
                raise TransportError(f"serial write failed: {exc}") from None
            if self._on_frame is not None:
                self._on_frame("tx", frame)

            framer = SerialFramer(self.settings, self.port, expected_response_length)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                try:
                    chunk = read_available(self.port)
                except Exception as exc:
                    self.close()
                    raise TransportError(f"serial read failed: {exc}") from None
                frames = framer.feed(chunk) if chunk else framer.flush_on_silence()
                for reply in frames:
                    if self._on_frame is not None:
                        self._on_frame("rx", reply)
                    if reply[0] != unit:
                        continue                     # someone else's answer
                    return reply[1:-2]
            if framer.discarded:
                raise TransportError("no valid reply (CRC errors on the line)")
            raise TransportError(f"no reply within {self.timeout:.2f}s")


class LoopbackSerial:
    """An in-memory stand-in for a serial port, used by the tests.

    Two of these cross-wired behave like a null modem cable, so the RTU framing
    can be exercised end to end without hardware.
    """

    def __init__(self, name: str = "loopback"):
        self.name = name
        self._incoming = bytearray()
        self._condition = threading.Condition()
        self.peer: LoopbackSerial | None = None
        self.closed = False
        self.timeout = 0.05

    @staticmethod
    def pair(name: str = "loopback") -> tuple[LoopbackSerial, LoopbackSerial]:
        left, right = LoopbackSerial(f"{name}-a"), LoopbackSerial(f"{name}-b")
        left.peer, right.peer = right, left
        return left, right

    @property
    def in_waiting(self) -> int:
        with self._condition:
            return len(self._incoming)

    def write(self, data: bytes) -> int:
        if self.closed or self.peer is None:
            raise OSError("port is closed")
        self.peer.deliver(bytes(data))
        return len(data)

    def deliver(self, data: bytes) -> None:
        with self._condition:
            self._incoming += data
            self._condition.notify_all()

    def read(self, size: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._condition:
            while not self._incoming and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)
            taken = bytes(self._incoming[:size])
            del self._incoming[:size]
            return taken

    def reset_input_buffer(self) -> None:
        with self._condition:
            self._incoming.clear()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._condition:
            self.closed = True
            self._condition.notify_all()
