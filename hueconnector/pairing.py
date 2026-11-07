"""Pairing a Hue Bluetooth bulb with BlueZ.

Hue bulbs reject "Just Works" pairing (what `bluetoothctl` does with a
NoInputNoOutput agent) with AuthenticationFailed. They require LE Secure
Connections *numeric comparison*. That works if we:

  1. connect with bleak first,
  2. register a KeyboardDisplay agent on D-Bus that auto-confirms the passkey,
  3. call pair() over that existing connection.

The bulb must be in pairing mode: brand new, "Make visible" / voice-assistant
pairing in the Hue Bluetooth app, or factory reset. Approach adapted from the
HueBLE library (used by Home Assistant's hue_ble integration).
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from bleak import BleakClient

log = logging.getLogger(__name__)


async def pair_bluez(client: BleakClient, timeout: float = 20.0) -> None:
    """Pair an already-connected BleakClient using an auto-confirming agent."""
    from dbus_fast import BusType
    from dbus_fast.aio import MessageBus
    from dbus_fast.service import ServiceInterface, method

    class AutoConfirmAgent(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: F821
            log.info("Auto-confirming passkey %06d for %s", passkey, device)

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821
            log.info("Authorizing %s", device)

        @method()
        def Cancel(self):
            log.warning("Pairing cancelled by the bulb")

        @method()
        def Release(self):
            pass

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = AutoConfirmAgent()
    path = f"/hueconnector/agent_{uuid.uuid4().hex[:8]}"
    bus.export(path, agent)
    introspection = await bus.introspect("org.bluez", "/org/bluez")
    manager = bus.get_proxy_object("org.bluez", "/org/bluez", introspection).get_interface(
        "org.bluez.AgentManager1"
    )
    await manager.call_register_agent(path, "KeyboardDisplay")
    try:
        await asyncio.sleep(1)
        await asyncio.wait_for(client.pair(), timeout=timeout)
    finally:
        try:
            await manager.call_unregister_agent(path)
        except Exception as exc:
            log.debug("Agent unregister failed: %s", exc)
        bus.unexport(path, agent)
        bus.disconnect()


async def pair_bulb(address: str, timeout: float = 30.0, wait: float = 0) -> None:
    """Pair a bulb, retrying for up to `wait` seconds while its pairing window is closed."""
    deadline = asyncio.get_running_loop().time() + wait
    attempt = 0
    while True:
        attempt += 1
        try:
            await _pair_once(address, timeout)
            return
        except Exception as exc:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise
            log.warning("Attempt %d failed (%s); bulb probably not in pairing mode. Retrying...", attempt, exc)
            await asyncio.sleep(3)


async def _pair_once(address: str, timeout: float) -> None:
    """Scan for, connect to and pair a bulb, then trust it so BlueZ keeps the bond."""
    from bleak import BleakScanner

    log.info("Looking for %s (bulb must be in pairing mode)...", address)
    device = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if device is None:
        raise SystemExit(f"{address} not found. Is the bulb powered and in range?")
    async with BleakClient(device, timeout=timeout) as client:
        log.info("Connected, pairing with numeric comparison...")
        await pair_bluez(client)
        log.info("Paired. Reading power state to confirm access...")
        from .bulb import CHAR_POWER

        state = await client.read_gatt_char(CHAR_POWER)
        log.info("Power characteristic = %s", bytes(state).hex())
    proc = await asyncio.create_subprocess_exec("bluetoothctl", "trust", address)
    await proc.wait()
    log.info("Done. %s is paired and trusted.", address)
