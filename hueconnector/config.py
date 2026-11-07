from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    "name": "Bedroom Light",
    "address": None,
    "color_temperature": True,
    "color": False,
    "ble": {"connect_timeout": 20, "poll_interval": 30, "command_ttl": 10, "max_backoff": 60},
    "homekit": {"enabled": True, "port": 51826, "state_file": "hueconnector.state", "pincode": None},
    "http": {"enabled": False, "host": "0.0.0.0", "port": 8080, "token": None},
}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str) -> Dict[str, Any]:
    cfg_path = Path(path).expanduser().resolve()
    cfg = _merge(DEFAULTS, json.loads(cfg_path.read_text()))
    if not cfg["address"]:
        raise SystemExit(f"{cfg_path}: set \"address\" to the bulb's Bluetooth MAC (see `hueconnector scan`)")
    state_file = Path(cfg["homekit"]["state_file"]).expanduser()
    if not state_file.is_absolute():
        state_file = cfg_path.parent / state_file
    cfg["homekit"]["state_file"] = str(state_file)
    return cfg
