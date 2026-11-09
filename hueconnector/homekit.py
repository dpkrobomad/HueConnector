"""HomeKit Lightbulb accessory (HAP-python) backed by a HueBulb."""
from __future__ import annotations

import colorsys
import logging
from typing import Any, Dict, Optional, Tuple

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB

from .bulb import MAX_LEVEL, MAX_MIREDS, MIN_MIREDS, HueBulb, LightState
from .colour import rgb_to_xy

log = logging.getLogger(__name__)


def pct_to_level(pct: float) -> int:
    return max(1, min(MAX_LEVEL, round(pct * MAX_LEVEL / 100)))


def level_to_pct(level: int) -> int:
    return max(1, min(100, round(level * 100 / MAX_LEVEL)))


def hs_to_xy(hue: float, sat: float) -> Tuple[float, float]:
    """HomeKit hue (0-360) / saturation (0-100) -> CIE xy (Philips wide-gamut formula)."""
    return rgb_to_xy(*colorsys.hsv_to_rgb(hue / 360.0, sat / 100.0, 1.0))


class HueLight(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, driver, display_name: str, bulb: HueBulb, http_api=None) -> None:
        super().__init__(driver, display_name)
        self.bulb = bulb
        self.http_api = http_api

        chars = ["On", "Brightness"]
        if bulb.color_temperature:
            chars.append("ColorTemperature")
        if bulb.color:
            chars += ["Hue", "Saturation"]
        serv = self.add_preload_service("Lightbulb", chars=chars)

        self.char_on = serv.configure_char("On", value=False)
        self.char_brightness = serv.configure_char("Brightness", value=100)
        self.char_ct = (
            serv.configure_char(
                "ColorTemperature",
                properties={"minValue": MIN_MIREDS, "maxValue": MAX_MIREDS},
                value=366,
            )
            if bulb.color_temperature
            else None
        )
        self.char_hue = serv.configure_char("Hue", value=0) if bulb.color else None
        self.char_sat = serv.configure_char("Saturation", value=0) if bulb.color else None

        # One callback per HomeKit request, with every characteristic it touched,
        # so "on + 40%" arrives as a single change.
        serv.setter_callback = self._on_homekit_set

        self.set_info_service(
            manufacturer="Signify (via HueConnector)",
            model="Hue Bluetooth bulb",
            serial_number=bulb.address.replace(":", ""),
        )
        bulb.add_listener(self._on_bulb_state)

    async def run(self) -> None:
        await self.bulb.start()
        if self.http_api is not None:
            await self.http_api.start()

    async def stop(self) -> None:
        if self.http_api is not None:
            await self.http_api.stop()
        await self.bulb.stop()

    # HomeKit -> bulb
    def _on_homekit_set(self, values: Dict[str, Any]) -> None:
        log.info("HomeKit set: %s", values)
        changes: Dict[str, Any] = {}
        if "On" in values:
            changes["on"] = bool(values["On"])
        if "Brightness" in values:
            pct = values["Brightness"]
            if pct <= 0:
                changes["on"] = False
            else:
                changes["brightness"] = pct_to_level(pct)
        if "ColorTemperature" in values:
            changes["mireds"] = int(values["ColorTemperature"])
        if self.char_hue is not None and ("Hue" in values or "Saturation" in values):
            hue = values.get("Hue", self.char_hue.value)
            sat = values.get("Saturation", self.char_sat.value)
            changes["xy"] = hs_to_xy(hue, sat)
        self.bulb.set(**changes)

    # bulb -> HomeKit (wall switch, Hue app, HTTP API, reconnect refresh)
    def _on_bulb_state(self, state: LightState) -> None:
        self._push(self.char_on, state.on)
        if state.brightness:
            self._push(self.char_brightness, level_to_pct(state.brightness))
        if self.char_ct is not None and state.mireds:
            self._push(self.char_ct, state.mireds)

    @staticmethod
    def _push(char, value: Optional[Any]) -> None:
        if value is not None and char.value != value:
            char.set_value(value)
