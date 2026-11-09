"""Colour helpers shared by the HomeKit accessory and the HTTP API."""
from __future__ import annotations

from typing import Tuple


def rgb_to_xy(r: float, g: float, b: float) -> Tuple[float, float]:
    """sRGB (0-1 each) -> CIE xy using the Philips wide-gamut formula."""

    def lin(c: float) -> float:
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92

    r, g, b = lin(r), lin(g), lin(b)
    x = r * 0.664511 + g * 0.154324 + b * 0.162028
    y = r * 0.283881 + g * 0.668433 + b * 0.047685
    z = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x + y + z
    return (0.3127, 0.3290) if total == 0 else (x / total, y / total)


def hex_to_xy(value: str) -> Tuple[float, float]:
    """'ff0000' or '#ff0000' -> CIE xy. Raises ValueError on bad input."""
    h = value.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"expected 6 hex digits, got {value!r}")
    r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return rgb_to_xy(r, g, b)
