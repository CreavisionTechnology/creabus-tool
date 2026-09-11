# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Creavision Technology
"""CreaBus Tool - simulate a Modbus device, or poll a real one, over TCP or RTU.

Four combinations, one code base:

    server + TCP   pretend to be a device on the network
    server + RTU   pretend to be a device on an RS-485 / USB serial bus
    client + TCP   poll a real device on the network
    client + RTU   poll a real device on a serial bus

What the device looks like lives entirely in a profile file under ``devices/``
- see :mod:`creabus_tool.profile` for the schema. The same profile describes a
device to simulate and tells the client how to decode a real one.
"""

from .branding import APP_NAME, LICENCE, PROJECT_URL, VENDOR, VENDOR_URL, VERSION
from .client import ModbusMaster, Poller, TcpMasterTransport, make_master, plan_blocks
from .datastore import DataStore
from .pdu import ModbusError, TransportError, crc16
from .profile import DeviceProfile, ProfileError, RegisterSpec, discover_profiles, load_profile
from .rtu import RtuListener, RtuMasterTransport, SerialSettings, list_serial_ports
from .server import (
    RtuServer,
    SimulationRunner,
    TcpServer,
    local_addresses,
)

__version__ = VERSION

__all__ = [
    "APP_NAME",
    "LICENCE",
    "PROJECT_URL",
    "VENDOR",
    "VENDOR_URL",
    "VERSION",
    "DataStore",
    "DeviceProfile",
    "ModbusError",
    "ModbusMaster",
    "Poller",
    "ProfileError",
    "RegisterSpec",
    "RtuListener",
    "RtuMasterTransport",
    "RtuServer",
    "SerialSettings",
    "SimulationRunner",
    "TcpMasterTransport",
    "TcpServer",
    "TransportError",
    "crc16",
    "discover_profiles",
    "list_serial_ports",
    "load_profile",
    "local_addresses",
    "make_master",
    "plan_blocks",
]
