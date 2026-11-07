# HueConnector

Control a Philips Hue **Bluetooth** bulb with Siri, without a Hue Bridge. A Jetson, Raspberry Pi or any other machine that can run Python and has Bluetooth LE holds a Bluetooth connection to the bulb and presents it to Apple Home as a HomeKit light.

```
Siri / Home app  →  HomeKit (HAP over Wi-Fi)  →  Jetson  →  BLE  →  Hue bulb
iPhone Shortcut  →  HTTP (optional)          ↗
```

What it does:
- Keeps one Bluetooth connection open and reconnects with backoff if it drops.
- Merges rapid changes, so dragging the brightness slider sends only the latest value.
- Throws away commands that are more than 10 s old, so a light doesn't suddenly change when the connection comes back.
- Reads the bulb's real state (by notification, and every 30 s) so the Home app stays correct when the light is changed from the Hue app or a switch.
- Provides an optional HTTP API for iPhone Shortcuts, using the same Bluetooth connection.

## Where it runs

It's plain Python (3.8+), so it runs on more than a Jetson: any always-on machine with Bluetooth LE and a Wi-Fi or Ethernet connection to your home network, within Bluetooth range of the bulb.

| Platform | Status |
|---|---|
| **Linux with BlueZ**: Jetson, Raspberry Pi, Ubuntu/Debian PC, other SBCs | Fully supported. Tested on a Jetson Orin Nano (JetPack 7 / Ubuntu 24.04, Python 3.12) |
| **macOS** | Control and HomeKit should work, but it's untested. `hueconnector pair` won't work, so pair the bulb when macOS prompts on first connection |
| **Windows** | Control and HomeKit should work, but it's untested. Pair the bulb in Windows Bluetooth settings instead of with `hueconnector pair` |

The parts that need Linux are the `pair` command (it uses BlueZ's D-Bus pairing agent), `scripts/install.sh` (apt) and the systemd service. On other systems, install with `pip install .` and start it with `hueconnector run` however you like (launchd, Task Scheduler, a terminal).

## 1. Install (on the Jetson or any Linux box)

```bash
git clone <this repo> ~/HueConnector   # or copy the folder over
cd ~/HueConnector
./scripts/install.sh
```

This sets up `.venv`, installs `bleak`, `HAP-python` and `aiohttp`, copies `config.example.json` to `config.json` and registers a systemd service. The service is enabled but not started yet.

## 2. Pair the bulb

The bulb accepts new devices only while it is discoverable:

1. In the **Hue Bluetooth** app, go to Settings → Voice Assistants and tap **Make visible**. You can also power-cycle the bulb, which makes it discoverable for a short time.
2. Find it:
   ```bash
   .venv/bin/hueconnector scan
   ```
3. Pair it (this keeps retrying for 5 minutes, so you can start it first and then open the pairing window):
   ```bash
   .venv/bin/hueconnector pair AA:BB:CC:DD:EE:FF --wait 300
   ```
   Don't use `bluetoothctl pair`. Hue bulbs reject that kind of pairing ("Just Works") with `AuthenticationFailed`. They require numeric-comparison pairing, which `hueconnector pair` does: it connects first, registers a BlueZ agent that confirms the code automatically, and then pairs over that connection. If the bulb answers `Pairing not supported` (visible with `sudo btmon`), its pairing window is closed.
4. Check that control works (the bulb turns off and back on):
   ```bash
   .venv/bin/hueconnector test AA:BB:CC:DD:EE:FF
   ```

The bulb accepts several connections at once, so the Hue app on your phone can stay paired too.

## 3. Configure and start

Edit `config.json`: set `address` and give the light a `name` that Siri will understand.

```bash
sudo systemctl start hueconnector
journalctl -u hueconnector -f
```

The log shows a **setup code** (and a QR code). In the Home app, tap **+ → Add Accessory → More options**, pick the light and enter the code. Then say "Hey Siri, turn off the bedroom light".

To set your own code, put a value like `"pincode": "031-45-154"` in `config.json`. Avoid simple codes like `111-11-111`, which HomeKit rejects. Pairing data is stored in `hueconnector.state`. Delete that file to reset pairing, and remove the accessory from the Home app too.

## Config reference

| Key | Default | Meaning |
|---|---|---|
| `name` | `Bedroom Light` | Name of the accessory in Home and for Siri |
| `address` | – | Bulb's Bluetooth MAC address |
| `color_temperature` | `true` | Shows the warm/cool slider. Set to `false` for white-only bulbs |
| `color` | `false` | **Experimental** colour (hue/saturation) support. See below |
| `ble.poll_interval` | `30` | Seconds between state reads |
| `ble.command_ttl` | `10` | Commands queued while offline expire after this many seconds |
| `ble.max_backoff` | `60` | Longest wait between reconnect attempts, in seconds |
| `homekit.enabled` | `true` | Turns the HomeKit accessory on or off |
| `homekit.port` | `51826` | HAP port |
| `homekit.pincode` | random | Setup code in `XXX-XX-XXX` format |
| `http.enabled` | `false` | Turns the Shortcuts HTTP API on or off |
| `http.token` | `null` | If set, requests need the header `Authorization: Bearer <token>` |

## Optional: iPhone Shortcuts via HTTP

Set `"http": {"enabled": true, "token": "some-long-secret"}` and restart the service. HTTP can run alongside HomeKit or on its own (set `homekit.enabled` to `false`).

| Endpoint | Effect |
|---|---|
| `/status` | JSON with on, brightness %, kelvin |
| `/on`, `/off`, `/toggle` | Power |
| `/brightness/40` | 40 %. A value of 0 turns the light off |
| `/temperature/2700` | Colour temperature in kelvin (2200–6500) |

In Shortcuts, add **Get Contents of URL** with `http://<jetson-ip>:8080/off` and a header `Authorization: Bearer some-long-secret`. Name the shortcut "Lights off" and Siri will run it when you say that phrase.

## Notes and limits

- **Range:** keep the Jetson within Bluetooth range of the bulb, ideally in the same room. The Orin Nano dev kit's Wi-Fi/BT card works.
- **Away from home:** using Siri or the Home app outside your home network needs an Apple home hub (HomePod or Apple TV). On home Wi-Fi, an iPhone alone is enough.
- **The GATT protocol is reverse-engineered.** On/off and brightness are well documented. Colour temperature (`0004`, uint16 mireds) is generally reliable. The colour characteristic (`0005`) is less well understood: the xy encoding used here is a best guess, so `color` is off by default.
- **Permissions:** BlueZ on Ubuntu/JetPack normally lets a regular user use Bluetooth through D-Bus. If you get `org.bluez.Error.NotPermitted`, add the user to the `bluetooth` group.
- **Debugging:** `hueconnector -v run` turns on debug logging, which includes notification support and dropped commands.

## Layout

```
hueconnector/bulb.py      BLE driver: connection, reconnect, coalesced writes, state
hueconnector/homekit.py   HAP-python Lightbulb accessory
hueconnector/http_api.py  aiohttp endpoints for Shortcuts
hueconnector/__main__.py  CLI: scan | test | run
systemd/                  service unit template (filled in by install.sh)
```
