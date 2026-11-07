"""Bluetooth LE driver for Philips Hue Bluetooth bulbs.

Keeps one persistent connection to the bulb, reconnects with exponential
backoff, coalesces rapid commands (e.g. a HomeKit brightness slider) so only
the latest value is written, and reports state changes to listeners.

The GATT layout below is community reverse-engineered, not an official API.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Tuple

from bleak import BleakClient, BleakScanner

log = logging.getLogger(__name__)

LIGHT_SERVICE = "932c32bd-0000-47a2-835a-a8d455b859dd"
CHAR_POWER = "932c32bd-0002-47a2-835a-a8d455b859dd"        # uint8: 0 off, 1 on
CHAR_BRIGHTNESS = "932c32bd-0003-47a2-835a-a8d455b859dd"   # uint8: 1..254
CHAR_TEMPERATURE = "932c32bd-0004-47a2-835a-a8d455b859dd"  # uint16 LE, mireds
CHAR_COLOR = "932c32bd-0005-47a2-835a-a8d455b859dd"        # xy (experimental)
SIGNIFY_ADV_SERVICE = "0000fe0f-0000-1000-8000-00805f9b34fb"

MIN_LEVEL, MAX_LEVEL = 1, 254
MIN_MIREDS, MAX_MIREDS = 153, 454


def _clamp(value: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, round(value))))


@dataclass(frozen=True)
class LightState:
    on: Optional[bool] = None
    brightness: Optional[int] = None  # 1..254
    mireds: Optional[int] = None


Listener = Callable[[LightState], None]


class HueBulb:
    def __init__(
        self,
        address: str,
        *,
        color_temperature: bool = True,
        color: bool = False,
        connect_timeout: float = 20.0,
        poll_interval: float = 30.0,
        command_ttl: float = 10.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.address = address
        self.color_temperature = color_temperature
        self.color = color
        self.connect_timeout = connect_timeout
        self.poll_interval = poll_interval
        self.command_ttl = command_ttl
        self.max_backoff = max_backoff

        self.state = LightState()
        self._listeners: List[Listener] = []
        self._pending: Dict[str, Tuple[object, float]] = {}
        self._client: Optional[BleakClient] = None
        self._tasks: List[asyncio.Task] = []
        # asyncio primitives are created in start() so they bind to the running loop.
        self._connected: Optional[asyncio.Event] = None
        self._disconnected: Optional[asyncio.Event] = None
        self._wake: Optional[asyncio.Event] = None

    # ------------------------------------------------------------------ public

    @property
    def connected(self) -> bool:
        return bool(self._connected and self._connected.is_set())

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def start(self) -> None:
        self._connected = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks = [
            asyncio.ensure_future(self._connection_loop()),
            asyncio.ensure_future(self._writer_loop()),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self._safe_disconnect()

    async def wait_connected(self) -> None:
        assert self._connected is not None, "call start() first"
        await self._connected.wait()

    def set(
        self,
        *,
        on: Optional[bool] = None,
        brightness: Optional[int] = None,
        mireds: Optional[int] = None,
        xy: Optional[Tuple[float, float]] = None,
    ) -> None:
        """Queue a change. Non-blocking; newer values replace queued ones."""
        now = time.monotonic()
        for key, value in (("on", on), ("brightness", brightness), ("mireds", mireds), ("xy", xy)):
            if value is not None:
                self._pending[key] = (value, now)
        if self._wake is not None:
            self._wake.set()

    async def refresh(self) -> None:
        """Read the bulb's actual state (picks up changes from wall switches, the Hue app...)."""
        client = self._require_client()
        power = await client.read_gatt_char(CHAR_POWER)
        level = await client.read_gatt_char(CHAR_BRIGHTNESS)
        new = replace(self.state, on=bool(power[0]), brightness=_clamp(level[0], MIN_LEVEL, MAX_LEVEL))
        if self.color_temperature:
            try:
                data = await client.read_gatt_char(CHAR_TEMPERATURE)
                if len(data) >= 2:
                    mireds = struct.unpack("<H", bytes(data[:2]))[0]
                    if mireds:
                        new = replace(new, mireds=_clamp(mireds, MIN_MIREDS, MAX_MIREDS))
            except Exception as exc:  # bulb may be white-only
                log.debug("Could not read colour temperature: %s", exc)
        self._update(new)

    # ---------------------------------------------------------------- internal

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise ConnectionError("bulb not connected")
        return self._client

    def _update(self, new: LightState) -> None:
        if new == self.state:
            return
        self.state = new
        log.info("Bulb state: %s", new)
        for listener in self._listeners:
            try:
                listener(new)
            except Exception:
                log.exception("State listener failed")

    async def _connection_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._connect()
                backoff = 1.0
                await self._stay_connected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Bulb connection problem: %s", exc)
            assert self._connected is not None
            self._connected.clear()
            await self._safe_disconnect()
            log.info("Reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _connect(self) -> None:
        log.info("Connecting to %s", self.address)
        device = await BleakScanner.find_device_by_address(self.address, timeout=self.connect_timeout)
        disconnected = asyncio.Event()
        client = BleakClient(
            device or self.address,  # bonded devices are known to BlueZ even when not seen in the scan
            disconnected_callback=lambda _c: disconnected.set(),
            timeout=self.connect_timeout,
        )
        await client.connect()
        self._client = client
        self._disconnected = disconnected
        await self._subscribe()
        await self.refresh()
        log.info("Connected to %s", self.address)
        assert self._connected is not None and self._wake is not None
        self._connected.set()
        self._wake.set()  # flush anything queued while offline

    async def _stay_connected(self) -> None:
        assert self._disconnected is not None
        while True:
            try:
                await asyncio.wait_for(self._disconnected.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                await self.refresh()
                continue
            log.warning("Bulb disconnected")
            return

    async def _safe_disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.debug("Disconnect error ignored: %s", exc)

    async def _subscribe(self) -> None:
        client = self._require_client()

        def on_power(_sender, data: bytearray) -> None:
            if data:
                self._update(replace(self.state, on=bool(data[0])))

        def on_brightness(_sender, data: bytearray) -> None:
            if data:
                self._update(replace(self.state, brightness=_clamp(data[0], MIN_LEVEL, MAX_LEVEL)))

        for uuid, handler in ((CHAR_POWER, on_power), (CHAR_BRIGHTNESS, on_brightness)):
            try:
                await client.start_notify(uuid, handler)
            except Exception as exc:  # falls back to polling
                log.debug("Notifications unavailable for %s: %s", uuid, exc)

    async def _writer_loop(self) -> None:
        assert self._wake is not None and self._connected is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self._pending:
                continue
            await self._connected.wait()

            batch, self._pending = self._pending, {}
            now = time.monotonic()
            fresh = {k: v for k, v in batch.items() if now - v[1] <= self.command_ttl}
            if len(fresh) < len(batch):
                log.info("Dropped stale commands: %s", sorted(set(batch) - set(fresh)))
            if not fresh:
                continue
            try:
                await self._write({k: v for k, (v, _t) in fresh.items()})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Write failed, will retry after reconnect: %s", exc)
                for key, item in fresh.items():
                    self._pending.setdefault(key, item)  # anything newer wins
                if self._disconnected is not None:
                    self._disconnected.set()  # force the connection loop to cycle

    async def _write(self, changes: Dict[str, object]) -> None:
        client = self._require_client()
        new = self.state

        async def write(uuid: str, data: bytes) -> None:
            await client.write_gatt_char(uuid, data, response=True)

        on = changes.get("on")
        if on is True:
            await write(CHAR_POWER, b"\x01")
            new = replace(new, on=True)
        if "brightness" in changes:
            level = _clamp(changes["brightness"], MIN_LEVEL, MAX_LEVEL)  # type: ignore[arg-type]
            await write(CHAR_BRIGHTNESS, bytes([level]))
            new = replace(new, brightness=level)
        if "mireds" in changes and self.color_temperature:
            mireds = _clamp(changes["mireds"], MIN_MIREDS, MAX_MIREDS)  # type: ignore[arg-type]
            await write(CHAR_TEMPERATURE, struct.pack("<H", mireds))
            new = replace(new, mireds=mireds)
        if "xy" in changes and self.color:
            x, y = changes["xy"]  # type: ignore[misc]
            await write(CHAR_COLOR, struct.pack("<HH", _clamp(x * 0xFFFF, 0, 0xFFFF), _clamp(y * 0xFFFF, 0, 0xFFFF)))
        if on is False:
            await write(CHAR_POWER, b"\x00")
            new = replace(new, on=False)
        self._update(new)


async def discover(timeout: float = 10.0, show_all: bool = False) -> List[Tuple[str, str, int]]:
    """Return (address, name, rssi) for nearby devices that look like Hue bulbs."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    results = []
    for address, (device, adv) in found.items():
        name = adv.local_name or device.name or ""
        uuids = {u.lower() for u in adv.service_uuids}
        looks_like_hue = (
            "hue" in name.lower() or LIGHT_SERVICE in uuids or SIGNIFY_ADV_SERVICE in uuids
        )
        if show_all or looks_like_hue:
            results.append((address, name, adv.rssi))
    return sorted(results, key=lambda r: -r[2])
