"""hueconnector command line: scan | test | run."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .bulb import HueBulb, discover
from .config import load_config

log = logging.getLogger("hueconnector")


def _make_bulb(cfg) -> HueBulb:
    return HueBulb(
        cfg["address"],
        color_temperature=cfg["color_temperature"],
        color=cfg["color"],
        **cfg["ble"],
    )


def _make_http(cfg, bulb):
    if not cfg["http"]["enabled"]:
        return None
    from .http_api import HttpApi

    h = cfg["http"]
    return HttpApi(bulb, host=h["host"], port=h["port"], token=h["token"])


async def _scan(args) -> None:
    print(f"Scanning for {args.timeout:.0f}s...")
    results = await discover(timeout=args.timeout, show_all=args.all)
    if not results:
        print("Nothing found. Make the bulb visible in the Hue Bluetooth app, or try --all.")
    for address, name, rssi in results:
        print(f"{address}  {rssi:>4} dBm  {name}")


async def _test(args) -> None:
    bulb = HueBulb(args.address)
    await bulb.start()
    try:
        await asyncio.wait_for(bulb.wait_connected(), timeout=60)
        original = bulb.state
        print(f"Connected. State: {original}")
        bulb.set(on=not original.on)
        await asyncio.sleep(2)
        bulb.set(on=bool(original.on))
        await asyncio.sleep(2)
        print("Toggled off/on successfully." if bulb.state.on == original.on else f"State now: {bulb.state}")
    finally:
        await bulb.stop()


async def _run_http_only(cfg) -> None:
    bulb = _make_bulb(cfg)
    http = _make_http(cfg, bulb)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await bulb.start()
    if http:
        await http.start()
    await stop.wait()
    if http:
        await http.stop()
    await bulb.stop()


def _run(args) -> None:
    cfg = load_config(args.config)
    if not cfg["homekit"]["enabled"]:
        if not cfg["http"]["enabled"]:
            raise SystemExit("Both homekit and http are disabled in the config; nothing to do.")
        asyncio.run(_run_http_only(cfg))
        return

    from pyhap.accessory_driver import AccessoryDriver

    from .homekit import HueLight

    hk = cfg["homekit"]
    pincode = hk["pincode"].encode() if hk["pincode"] else None
    driver = AccessoryDriver(port=hk["port"], persist_file=hk["state_file"], pincode=pincode)
    bulb = _make_bulb(cfg)
    driver.add_accessory(HueLight(driver, cfg["name"], bulb, http_api=_make_http(cfg, bulb)))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()  # blocks; prints the setup code / QR while unpaired


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="hueconnector", description="Hue Bluetooth bulb -> HomeKit / HTTP bridge")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p_scan = sub.add_parser("scan", help="find nearby Hue bulbs")
    p_scan.add_argument("--timeout", type=float, default=10)
    p_scan.add_argument("--all", action="store_true", help="list every BLE device")

    p_pair = sub.add_parser("pair", help="pair a bulb that is in pairing mode")
    p_pair.add_argument("address")
    p_pair.add_argument("--wait", type=float, default=0, help="keep retrying for this many seconds")

    p_test = sub.add_parser("test", help="connect and blink a bulb")
    p_test.add_argument("address")

    p_run = sub.add_parser("run", help="run the bridge (default)")
    p_run.add_argument("-c", "--config", default="config.json")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if args.cmd == "scan":
        asyncio.run(_scan(args))
    elif args.cmd == "pair":
        from .pairing import pair_bulb

        asyncio.run(pair_bulb(args.address, wait=args.wait))
    elif args.cmd == "test":
        asyncio.run(_test(args))
    else:
        if args.cmd is None:
            args.config = "config.json"
        _run(args)


if __name__ == "__main__":
    main()
