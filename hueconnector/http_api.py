"""Tiny HTTP API for iPhone Shortcuts (works with or without HomeKit).

Endpoints (GET or POST):
  /status                 current state as JSON
  /on  /off  /toggle
  /brightness/{0-100}     0 turns the light off
  /temperature/{kelvin}   2200-6500
  /color/{hex}            e.g. /color/ff0000 (needs "color": true in config; colour support is experimental)
  /flash/{times}          blink the light N times (1-10), ending on
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Optional

from aiohttp import web

from .bulb import MAX_MIREDS, MIN_MIREDS, HueBulb
from .colour import hex_to_xy

log = logging.getLogger(__name__)


class HttpApi:
    def __init__(self, bulb: HueBulb, host: str = "0.0.0.0", port: int = 8080, token: Optional[str] = None):
        self.bulb = bulb
        self.host = host
        self.port = port
        self.token = token
        self._runner: Optional[web.AppRunner] = None
        self._flash_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth])
        for path, handler in (
            ("/status", self.status),
            ("/on", self.on),
            ("/off", self.off),
            ("/toggle", self.toggle),
            ("/brightness/{pct}", self.brightness),
            ("/temperature/{kelvin}", self.temperature),
            ("/color/{hex}", self.color),
            ("/flash/{times}", self.flash),
        ):
            app.router.add_route("GET", path, handler)
            app.router.add_route("POST", path, handler)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("HTTP API on http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if self.token:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {self.token}"):
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    def _reply(self, **queued) -> web.Response:
        return web.json_response({"ok": True, "connected": self.bulb.connected, "queued": queued})

    async def status(self, _request: web.Request) -> web.Response:
        s = self.bulb.state
        return web.json_response(
            {
                "connected": self.bulb.connected,
                "on": s.on,
                "brightness_pct": round(s.brightness * 100 / 254) if s.brightness else None,
                "kelvin": round(1_000_000 / s.mireds) if s.mireds else None,
            }
        )

    async def on(self, _request: web.Request) -> web.Response:
        self.bulb.set(on=True)
        return self._reply(on=True)

    async def off(self, _request: web.Request) -> web.Response:
        self.bulb.set(on=False)
        return self._reply(on=False)

    async def toggle(self, _request: web.Request) -> web.Response:
        target = not bool(self.bulb.state.on)
        self.bulb.set(on=target)
        return self._reply(on=target)

    async def brightness(self, request: web.Request) -> web.Response:
        pct = _number(request.match_info["pct"], 0, 100)
        if pct == 0:
            self.bulb.set(on=False)
            return self._reply(on=False)
        self.bulb.set(on=True, brightness=max(1, round(pct * 254 / 100)))
        return self._reply(on=True, brightness_pct=pct)

    async def temperature(self, request: web.Request) -> web.Response:
        if not self.bulb.color_temperature:
            raise web.HTTPBadRequest(text="colour temperature disabled in config")
        kelvin = _number(request.match_info["kelvin"], 1_000_000 // MAX_MIREDS, 1_000_000 // MIN_MIREDS)
        self.bulb.set(mireds=round(1_000_000 / kelvin))
        return self._reply(kelvin=kelvin)

    async def color(self, request: web.Request) -> web.Response:
        if not self.bulb.color:
            raise web.HTTPBadRequest(text='colour disabled in config (set "color": true)')
        try:
            xy = hex_to_xy(request.match_info["hex"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc))
        self.bulb.set(on=True, xy=xy)
        return self._reply(on=True, xy=[round(xy[0], 4), round(xy[1], 4)])

    async def flash(self, request: web.Request) -> web.Response:
        times = _number(request.match_info["times"], 1, 10)
        if self._flash_task and not self._flash_task.done():
            self._flash_task.cancel()
        self._flash_task = asyncio.ensure_future(self._blink(times))
        return self._reply(flash=times)

    async def _blink(self, times: int, period: float = 0.5) -> None:
        for _ in range(times):
            self.bulb.set(on=False)
            await asyncio.sleep(period / 2)
            self.bulb.set(on=True)
            await asyncio.sleep(period / 2)


def _number(raw: str, lo: int, hi: int) -> int:
    try:
        value = int(float(raw))
    except ValueError:
        raise web.HTTPBadRequest(text=f"not a number: {raw}")
    if not lo <= value <= hi:
        raise web.HTTPBadRequest(text=f"must be between {lo} and {hi}")
    return value
