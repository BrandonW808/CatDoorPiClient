#!/usr/bin/env python3
"""Persistent JSON config for runtime-adjustable settings (survives restarts
and git pulls because local_config.json is in .gitignore)."""

import json
import os
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "local_config.json")

_lock = threading.Lock()

DEFAULTS = {
    "min_weight": -3100,
    "max_weight": -4600,
    "auto_update": True,
    "update_check_interval": 3600,   # seconds (30 min)
}


def load() -> dict:
    """Return merged defaults + stored overrides."""
    with _lock:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as fh:
                    stored = json.load(fh)
                return {**DEFAULTS, **stored}
            except (json.JSONDecodeError, IOError):
                pass
        return dict(DEFAULTS)


def save(cfg: dict):
    with _lock:
        with open(CONFIG_FILE, "w") as fh:
            json.dump(cfg, fh, indent=2)


def update(updates: dict) -> dict:
    """Merge *updates* into the stored config and persist."""
    cfg = load()
    cfg.update(updates)
    save(cfg)
    return cfg