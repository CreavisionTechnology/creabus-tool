# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""Self test for the transports and the client role.

Exercises Modbus RTU end to end over an in-memory loopback "cable" (no serial
hardware needed) and the polling client against the built in TCP server.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from creabus_tool.client import (
    ModbusMaster,
    Poller,
    TcpMasterTransport,
    plan_blocks,
)
from creabus_tool.datastore import DataStore, decode_words
from creabus_tool.pdu import (
    ModbusError,
    TransportError,
    append_crc,
    check_crc,
    crc16,
    decode_bit_response,
    decode_register_response,
    expected_request_length,
    expected_response_length,
    read_request,
    write_multiple_registers,
    write_single_register,
)
from creabus_tool.profile import load_profile
from creabus_tool.rtu import (
    MAX_BUFFERED_BYTES,
    LoopbackSerial,
    RtuMasterTransport,
    SerialFramer,
    SerialSettings,
)
from creabus_tool.server import RtuServer, SimulationRunner, TcpServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 15040
passed = failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def section(title: str) -> None:
    print(f"\n-- {title} " + "-" * max(0, 60 - len(title)))


# =============================================================================
def test_crc_and_framing() -> None:
    section("CRC and RTU frame length prediction")

    # the standard CRC-16/MODBUS check value
    check("CRC of '123456789' is 0x4B37", crc16(b"123456789") == 0x4B37,
          hex(crc16(b"123456789")))
    frame = append_crc(bytes.fromhex("0103000A0001"))
    check("append_crc then check_crc round trips", check_crc(frame), frame.hex())
    corrupted = bytearray(frame)
    corrupted[3] ^= 0xFF
    check("a flipped bit fails the CRC", not check_crc(bytes(corrupted)))

    check("read request is 8 bytes",
          expected_request_length(bytes.fromhex("010300000002")) == 8)
    check("write multiple needs the byte count first",
          expected_request_length(bytes.fromhex("0110000000")) is None)
    check("write multiple length follows the byte count",
          expected_request_length(bytes.fromhex("011000000002" + "04")) == 13)
    check("report server id request is 4 bytes",
          expected_request_length(bytes.fromhex("0111")) == 4)
    check("unknown function has no predictable length",
          expected_request_length(bytes.fromhex("0163")) is None)

    check("read response length follows the byte count",
          expected_response_length(bytes.fromhex("010304")) == 9)
    # an RTU frame starts with the slave address, so 01 83 02 is "unit 1,
    # exception reply to function 3, illegal data address"
    check("exception response is 5 bytes",
          expected_response_length(bytes.fromhex("018302")) == 5,
          str(expected_response_length(bytes.fromhex("018302"))))
    check("write echo response is 8 bytes",
          expected_response_length(bytes.fromhex("0110")) == 8)


# =============================================================================
def test_rtu_over_loopback() -> None:
    section("Modbus RTU over an in-memory loopback")

    profile = load_profile(os.path.join(HERE, "devices", "sdm120.yaml"))
    store = DataStore(profile)
    simulation = SimulationRunner(store, tick=0.1)
    simulation.start()

    device_side, master_side = LoopbackSerial.pair("virtual-rs485")
    settings = SerialSettings(port="virtual-rs485", baudrate=19200)
    server = RtuServer(store, settings, log_requests=False,
                       port_factory=lambda: device_side)
    server.start()
    transport = RtuMasterTransport(settings, timeout=1.0, port_factory=lambda: master_side)
    master = ModbusMaster(transport, unit=profile.unit_id, retries=0)

    try:
        response = master.request(read_request(0x04, 0x0000, 2), describe=False)
        words = [int.from_bytes(response[2 + i:4 + i], "big") for i in (0, 2)]
        voltage = decode_words(profile.by_name("voltage"), words, "big", "big")
        check("read input registers over RTU", 220.0 < voltage < 250.0, f"{voltage}")

        response = master.request(read_request(0x04, 0x0000, 40), describe=False)
        check("a 40 register RTU read comes back whole", len(response) == 2 + 80,
              str(len(response)))

        master.request(write_single_register(0x0000, 0x4348), describe=False)
        master.request(write_single_register(0x0001, 0x0000), describe=False)
        back = decode_words(profile.by_name("relay_pulse_width"),
                            [int.from_bytes(master.request(
                                read_request(0x03, 0x0000, 2), describe=False)[2 + i:4 + i], "big")
                             for i in (0, 2)], "big", "big")
        check("write single register over RTU", abs(back - 200.0) < 0.01, f"{back}")

        master.request(write_multiple_registers(0x0014, [0x4120, 0x0000]), describe=False)
        check("write multiple registers over RTU",
              abs(store.snapshot()[
                  [r.name for r in store.snapshot()].index("scroll_display_time")].value
                  - 10.0) < 0.01)

        try:
            master.request(read_request(0x63, 0, 1), describe=False)
            check("unsupported function returns an exception over RTU", False)
        except ModbusError as exc:
            check("unsupported function returns an exception over RTU", exc.code == 0x01,
                  exc.describe())

        # a request for another slave must be met with silence, not an exception
        other = ModbusMaster(RtuMasterTransport(settings, timeout=0.4,
                                                port_factory=lambda: master_side),
                             unit=9, retries=0)
        try:
            other.request(read_request(0x04, 0, 2), describe=False)
            check("a request for another slave address is ignored", False)
        except TransportError:
            check("a request for another slave address is ignored", True)

        # broadcast: acted on, never answered
        broadcast = ModbusMaster(RtuMasterTransport(settings, timeout=0.4,
                                                    port_factory=lambda: master_side),
                                 unit=0, retries=0)
        try:
            broadcast.request(write_single_register(0x0000, 0x42C8), describe=False)
            check("a broadcast write gets no reply", False)
        except TransportError:
            check("a broadcast write gets no reply", True)
        words = [int.from_bytes(master.request(read_request(0x03, 0x0000, 2),
                                               describe=False)[2 + i:4 + i], "big")
                 for i in (0, 2)]
        check("the broadcast write was still applied",
              abs(decode_words(profile.by_name("relay_pulse_width"), words, "big", "big")
                  - 100.0) < 0.5,
              str(decode_words(profile.by_name("relay_pulse_width"), words, "big", "big")))

        # garbage on the line must not wedge the framer
        master_side.write(b"\x01\x03\xff\xff")           # bad CRC fragment
        time.sleep(0.1)
        response = master.request(read_request(0x04, 0x0000, 2), describe=False)
        check("the framer recovers after a bad frame", len(response) == 6, str(len(response)))
        check("bad frames are counted", server.rtu_stats.crc_errors > 0,
              str(server.rtu_stats.crc_errors))

        stats = server.stats.snapshot()
        check("the RTU server counted its requests", stats["requests"] >= 8,
              str(stats["requests"]))
        check("the bus master shows up as a client", len(server.clients()) == 1)
    finally:
        server.stop()
        simulation.stop()
        transport.close()


# =============================================================================
def test_client_against_server() -> None:
    section("polling client against the built in TCP server")

    profile = load_profile(os.path.join(HERE, "devices", "sdm120.yaml"))
    device_store = DataStore(profile)
    simulation = SimulationRunner(device_store, tick=0.1)
    simulation.start()
    server = TcpServer(device_store, "127.0.0.1", PORT, log_requests=False)
    server.start()
    time.sleep(0.3)

    blocks = plan_blocks(profile)
    check("registers are grouped into fewer requests than registers",
          0 < len(blocks) < len(profile.registers), f"{len(blocks)} blocks")
    check("no block exceeds the 125 register limit",
          all(b.count <= 125 for b in blocks), str(max(b.count for b in blocks)))
    covered = sum(len(b.registers) for b in blocks)
    check("every register is covered by exactly one block",
          covered == len(profile.registers), f"{covered}/{len(profile.registers)}")

    # a second, empty store that the client fills in from the wire
    client_profile = load_profile(os.path.join(HERE, "devices", "sdm120.yaml"))
    client_store = DataStore(client_profile)
    master = ModbusMaster(TcpMasterTransport("127.0.0.1", PORT, timeout=2.0),
                          unit=profile.unit_id)
    poller = Poller(master, client_store, interval=0.3)

    try:
        poller.poll_once()
        by_name = {r.name: r for r in client_store.snapshot()}
        device = {r.name: r for r in device_store.snapshot()}
        check("the client read the voltage the server is serving",
              abs(by_name["voltage"].value - device["voltage"].value) < 0.01,
              f"{by_name['voltage'].value} vs {device['voltage'].value}")
        check("the client read the energy counter",
              abs(by_name["import_active_energy"].value
                  - device["import_active_energy"].value) < 0.01)
        check("the client read a holding register",
              abs(by_name["serial_number"].value - device["serial_number"].value) < 0.5,
              f"{by_name['serial_number'].value}")
        check("polling one cycle costs one request per block",
              master.stats.snapshot()["requests"] == len(blocks),
              str(master.stats.snapshot()["requests"]))
        check("every register was marked as read",
              all(r.reads > 0 for r in client_store.snapshot()))

        # the client can write back, and the server sees it
        master.write_register_value(client_profile,
                                    client_profile.by_name("relay_pulse_width"), 250.0)
        time.sleep(0.1)
        served = {r.name: r for r in device_store.snapshot()}
        check("a client write reaches the server",
              abs(served["relay_pulse_width"].value - 250.0) < 0.01,
              str(served["relay_pulse_width"].value))

        # background polling
        poller.start()
        time.sleep(1.2)
        check("the background poller keeps cycling", poller.cycles >= 2, str(poller.cycles))
        poller.stop()

        # a device that is not there fails cleanly
        dead = ModbusMaster(TcpMasterTransport("127.0.0.1", PORT + 7, timeout=0.5),
                            unit=1, retries=0)
        try:
            dead.request(read_request(0x04, 0, 2), describe=False)
            check("connecting to nothing raises TransportError", False)
        except TransportError:
            check("connecting to nothing raises TransportError", True)
    finally:
        poller.stop()
        server.stop()
        simulation.stop()


# =============================================================================
def test_client_against_rtu_server() -> None:
    section("polling client against the RTU server (full loop)")

    profile = load_profile(os.path.join(HERE, "devices", "sdm630.yaml"))
    device_store = DataStore(profile)
    simulation = SimulationRunner(device_store, tick=0.1)
    simulation.start()

    device_side, master_side = LoopbackSerial.pair("virtual-rs485")
    settings = SerialSettings(port="virtual-rs485", baudrate=115200)
    server = RtuServer(device_store, settings, log_requests=False,
                       port_factory=lambda: device_side)
    server.start()

    client_store = DataStore(load_profile(os.path.join(HERE, "devices", "sdm630.yaml")))
    master = ModbusMaster(RtuMasterTransport(settings, timeout=1.0,
                                             port_factory=lambda: master_side),
                          unit=profile.unit_id, retries=0)
    poller = Poller(master, client_store, interval=0.5)
    try:
        poller.poll_once()
        served = {r.name: r for r in device_store.snapshot()}
        read = {r.name: r for r in client_store.snapshot()}
        check("three phase profile polls over RTU",
              abs(read["voltage_l1"].value - served["voltage_l1"].value) < 0.01,
              f"{read['voltage_l1'].value} vs {served['voltage_l1'].value}")
        check("derived registers survive the round trip",
              abs(read["total_power"].value - served["total_power"].value) < 0.5)
        check("every one of the 45 registers came back",
              all(not r.error for r in client_store.snapshot()),
              next((r.error for r in client_store.snapshot() if r.error), ""))
    finally:
        poller.stop()
        server.stop()
        simulation.stop()


# =============================================================================
def test_malformed_replies() -> None:
    """A device that answers badly must not be able to crash the client.

    Everything here is a reply a real - or hostile - device can put on the
    wire, so each one has to come back as a TransportError rather than an
    IndexError or a plausible looking wrong value.
    """
    section("malformed replies from a device")

    def raises_transport_error(call) -> bool:
        try:
            call()
        except TransportError:
            return True
        except Exception:  # noqa: BLE001 - anything else is the bug
            return False
        return False

    # FC03 reply claiming four bytes but carrying two
    check("truncated register reply is refused",
          raises_transport_error(
              lambda: decode_register_response(bytes([0x03, 0x04, 0x00, 0x01]))))

    # an odd byte count cannot describe whole 16 bit registers
    check("odd register byte count is refused",
          raises_transport_error(
              lambda: decode_register_response(bytes([0x03, 0x03, 0x00, 0x01, 0x02]))))

    # 40 coils asked for, one byte (8 bits) supplied
    check("bit reply too short for the quantity is refused",
          raises_transport_error(
              lambda: decode_bit_response(bytes([0x01, 0x01, 0xFF]), 40)))

    # the same reply is fine for the 8 bits it can actually carry
    bits = decode_bit_response(bytes([0x01, 0x01, 0b10100101]), 8)
    check("a bit reply that does cover the quantity still decodes",
          bits == [True, False, True, False, False, True, False, True], str(bits))

    # an exception reply is still an exception, not a decode failure
    raised = None
    try:
        decode_register_response(bytes([0x83, 0x02]))
    except ModbusError as exc:
        raised = exc
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check("exception reply raises ModbusError",
          isinstance(raised, ModbusError) and raised.code == 0x02, repr(raised))

    # a well formed reply still decodes
    words = decode_register_response(bytes([0x03, 0x04, 0x12, 0x34, 0x56, 0x78]))
    check("a good register reply decodes", words == [0x1234, 0x5678], str(words))


# =============================================================================
def test_framer_resynchronisation() -> None:
    """Noise before a good frame must not cost more than linear work."""
    section("framer resynchronisation")

    settings = SerialSettings(port="test", baudrate=19200)
    good = append_crc(bytes.fromhex("0103000A0001"))

    # Noise that does parse as a request but never checksums: this is the path
    # that drops a byte at a time, and the one that used to be quadratic.
    framer = SerialFramer(settings, None, expected_request_length)
    junk = bytes([0x01, 0x03]) * 2048           # 4 kB of plausible looking noise
    started = time.monotonic()
    frames = framer.feed(junk + good)
    elapsed = time.monotonic() - started
    check("the good frame is found after 4 kB of noise", frames == [good],
          f"{len(frames)} frame(s)")
    check("resynchronising 4 kB stays quick", elapsed < 1.0, f"{elapsed:.3f}s")
    check("the discarded bytes were counted", framer.discarded > 0,
          str(framer.discarded))

    # A noise burst immediately before a real request, with no silence between
    # them, used to take the request down with it: the whole buffer failed the
    # CRC check and was dropped, so the sender saw only a timeout. This is the
    # exact shape that made the macOS CI job fail intermittently.
    framer = SerialFramer(settings, None, expected_request_length)
    framer.feed(bytes.fromhex("0103FFFF") + good)   # fragment, then a good frame
    framer.last_byte_at = 0.0                       # pretend the line went quiet
    frames = framer.flush_on_silence()
    check("a good frame survives noise flushed in the same silence window",
          frames == [good], f"{len(frames)} frame(s)")
    check("the surrounding noise was still counted", framer.discarded > 0,
          str(framer.discarded))

    # Noise the length rules cannot even measure (function code 0). There is
    # nothing to resynchronise on, so the buffer must be capped instead of
    # growing for as long as the line babbles.
    framer = SerialFramer(settings, None, expected_request_length)
    for _ in range(64):
        framer.feed(bytes(1024))                # 64 kB of zeros, no silence
    check("an unparseable stream does not grow the buffer without bound",
          len(framer.buffer) <= MAX_BUFFERED_BYTES,
          f"{len(framer.buffer)} bytes buffered")

    # and a good frame still comes through after all that
    frames = framer.feed(good)
    check("a good frame still arrives after the noise was capped",
          frames == [good], f"{len(frames)} frame(s)")


def main() -> int:
    test_crc_and_framing()
    test_malformed_replies()
    test_framer_resynchronisation()
    test_rtu_over_loopback()
    test_client_against_server()
    test_client_against_rtu_server()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
