#!/usr/bin/env python3
"""
ChunkPreventionSystem — Cat Door Controller with Remote Server Integration
"""

import time
import sys
import threading
from enum import Enum
from datetime import datetime

import RPi.GPIO as GPIO
from hx711 import HX711

# ── Optional: Socket.IO for server connectivity ──────────
try:
    import socketio

    SOCKET_ENABLED = True
except ImportError:
    SOCKET_ENABLED = False
    print("⚠️  python-socketio not installed — running in offline mode")
    print("   Install with:  pip install 'python-socketio[client]'")


import os
import subprocess

# ── Version & repo paths ──
REPO_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(REPO_DIR, "VERSION")
AUTO_UPDATE_INTERVAL = 6 * 60 * 60   # 6 hours
GIT_BRANCH = "main"

def read_version() -> str:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        try:
            return subprocess.check_output(
                ["git", "-C", REPO_DIR, "rev-parse", "--short", "HEAD"],
                text=True,
            ).strip()
        except Exception:
            return "unknown"

VERSION = read_version()

# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

class Mode(Enum):
    Training = "training"
    Standard = "standard"


# Server
SERVER_URL = "http://192.168.2.10:3001"          # ← change this
DEVICE_API_KEY = "catdoor-device-secret-key"     # ← must match server .env
DEVICE_ID = "catdoor-1"

# Hardware pins
LOCK_RELAY = 23
DOOR_SENSOR = 17
HX711_DT = 5
HX711_SCK = 6
REFERENCE_UNIT = 24.98

# Behaviour
MODE = Mode.Standard
MIN_WEIGHT = -3100
MAX_WEIGHT = -4600
UNLOCK_TIME = 10
STATE_CHANGE_DEBOUNCE = 2        # seconds
VALID_READ_MIN_SECONDS = 0       # seconds
STATUS_INTERVAL = 1.0            # how often to push status to server


# ═══════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════
door_open = False
valid = False
current_weight: float = 0.0
lock_timer: threading.Timer | None = None
close_timer: threading.Timer | None = None
lid_open = False                    # legacy flag (kept for parity)
validity_change_time = datetime.now()
state_change_time = datetime.now()


# ═══════════════════════════════════════════════════════════
#  SOCKET.IO SETUP
# ═══════════════════════════════════════════════════════════
if SOCKET_ENABLED:
    sio = socketio.Client(
        reconnection=True,
        reconnection_delay=5,
        reconnection_delay_max=30,
        logger=False,
    )

    @sio.on("connect", namespace="/device")
    def _on_connect():
        print("✅ Connected to server")
        send_status()
        log_event("device_connected", f"Device {DEVICE_ID} connected")

    @sio.on("disconnect", namespace="/device")
    def _on_disconnect():
        print("❌ Disconnected from server")

    @sio.on("command", namespace="/device")
    def _on_command(data):
        _handle_remote_command(data)


# ═══════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════
def safe_emit(event: str, data: dict):
    """Emit a Socket.IO event; no-op when offline."""
    if not SOCKET_ENABLED:
        return
    try:
        if sio.connected:
            sio.emit(event, data, namespace="/device")
    except Exception as exc:
        print(f"Socket emit error: {exc}")


def log_event(event_type: str, message: str, data: dict | None = None):
    """Print locally + push to server."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{event_type}] {message}")
    safe_emit("log", {
        "eventType": event_type,
        "message": message,
        "data": data or {},
    })


def send_status():
    safe_emit("status_update", {
        "locked": not GPIO.input(LOCK_RELAY),
        "doorOpen": door_open,
        "weight": current_weight,
        "valid": valid,
        "mode": MODE.value,
        "minWeight": MIN_WEIGHT,
        "maxWeight": MAX_WEIGHT,
        "version": VERSION,
    })

def _handle_remote_command(data: dict):
    global MIN_WEIGHT, MAX_WEIGHT
    action = data.get("action")
    print(f"📡 Remote command: {action}")

    if action == "unlock":
        GPIO.output(LOCK_RELAY, True)
        log_event("unlocked", "Manual unlock via remote command")
        send_status()
    elif action == "lock":
        if not door_open:
            GPIO.output(LOCK_RELAY, False)
            log_event("locked", "Manual lock via remote command")
            send_status()
        else:
            log_event("error", "Cannot lock — door is open")
    elif action == "update":
        threading.Thread(target=perform_update, daemon=True).start()
    elif action == "set_weight_range":
        try:
            new_min = int(data["minWeight"])
            new_max = int(data["maxWeight"])
            MIN_WEIGHT, MAX_WEIGHT = new_min, new_max
            log_event("config_changed",
                      f"Weight range set to [{MAX_WEIGHT}, {MIN_WEIGHT}]",
                      {"minWeight": MIN_WEIGHT, "maxWeight": MAX_WEIGHT})
            send_status()
        except Exception as exc:
            log_event("error", f"Bad set_weight_range payload: {exc}")

def connect_to_server():
    """Background thread: keeps retrying until connected."""
    if not SOCKET_ENABLED:
        return
    while True:
        try:
            print(f"🔌 Connecting to {SERVER_URL} …")
            sio.connect(
                SERVER_URL,
                namespaces=["/device"],
                auth={"apiKey": DEVICE_API_KEY, "deviceId": DEVICE_ID},
            )
            break                       # connected — socketio handles reconnection from here
        except Exception as exc:
            print(f"Connection failed: {exc}  — retrying in 10 s…")
            time.sleep(10)


# ═══════════════════════════════════════════════════════════
#  GPIO SETUP
# ═══════════════════════════════════════════════════════════
GPIO.setmode(GPIO.BCM)
GPIO.setup(DOOR_SENSOR, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(LOCK_RELAY, GPIO.OUT)
GPIO.output(LOCK_RELAY, False)          # start locked


# ═══════════════════════════════════════════════════════════
#  LOCK / UNLOCK
# ═══════════════════════════════════════════════════════════
def unlock():
    global lock_timer
    current_state = GPIO.input(LOCK_RELAY)
    if not current_state:                       # only if currently locked
        GPIO.output(LOCK_RELAY, True)
        log_event("unlocked", "Unlocked (auto — cat detected)")
        send_status()
        if lock_timer is not None:
            lock_timer.cancel()
        lock_timer = threading.Timer(UNLOCK_TIME, lock)
        lock_timer.start()


def lock():
    global lock_timer, close_timer
    if not valid and not door_open:
        close_timer = None
        if lock_timer is not None:
            lock_timer.cancel()
            lock_timer = None
        GPIO.output(LOCK_RELAY, False)
        log_event("locked", "Locked (auto)")
        send_status()


# ═══════════════════════════════════════════════════════════
#  DOOR SENSOR
# ═══════════════════════════════════════════════════════════
def read_sensor(channel):
    global door_open, close_timer

    if GPIO.input(channel):
        # Door OPENED
        door_open = True
        log_event("door_opened", "Door opened 🚪")
        send_status()
        if close_timer is not None:
            close_timer.cancel()
            close_timer = None
    else:
        # Door CLOSED
        door_open = False
        log_event("door_closed", "Door closed 🔒")
        send_status()
        close_timer = threading.Timer(3.0, lock)
        close_timer.start()


GPIO.add_event_detect(
    DOOR_SENSOR,
    GPIO.BOTH,
    callback=read_sensor,
    bouncetime=500,
)

def perform_update(force_restart: bool = True) -> bool:
    """git pull; return True if anything was updated."""
    try:
        log_event("update_check", f"Checking for updates (current {VERSION})")
        subprocess.check_call(["git", "-C", REPO_DIR, "fetch", "origin", GIT_BRANCH])
        local = subprocess.check_output(
            ["git", "-C", REPO_DIR, "rev-parse", "HEAD"], text=True).strip()
        remote = subprocess.check_output(
            ["git", "-C", REPO_DIR, "rev-parse", f"origin/{GIT_BRANCH}"], text=True).strip()
        if local == remote:
            log_event("update_check", "Already up to date")
            return False
        subprocess.check_call(
            ["git", "-C", REPO_DIR, "reset", "--hard", f"origin/{GIT_BRANCH}"])
        # Optional: install new deps
        req = os.path.join(REPO_DIR, "requirements.txt")
        if os.path.exists(req):
            subprocess.call(["pip", "install", "-r", req])
        log_event("updated", f"Updated to {remote[:7]} — restarting")
        if force_restart:
            # Let systemd / supervisor restart us
            time.sleep(1)
            clean_and_exit()
        return True
    except Exception as exc:
        log_event("error", f"Update failed: {exc}")
        return False


def auto_update_loop():
    while True:
        time.sleep(AUTO_UPDATE_INTERVAL)
        perform_update()


# ═══════════════════════════════════════════════════════════
#  HX711 (SCALE)
# ═══════════════════════════════════════════════════════════
hx = HX711(HX711_DT, HX711_SCK)
hx.power_up()
hx.set_reading_format("MSB", "MSB")
hx.set_reference_unit(REFERENCE_UNIT)
hx.reset()

print("Running tare …")
hx.tare()
print("Tare done — add weight now.")


# ═══════════════════════════════════════════════════════════
#  CLEANUP
# ═══════════════════════════════════════════════════════════
def clean_and_exit():
    hx.power_down()
    print("Cleaning up …")
    GPIO.cleanup()
    if SOCKET_ENABLED and sio.connected:
        sio.disconnect()
    print("Bye!")
    sys.exit()


# ═══════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════
print(f"🐱 ChunkPreventionSystem booting in {MODE.value} mode")
threading.Thread(target=connect_to_server, daemon=True).start()
threading.Thread(target=auto_update_loop, daemon=True).start()

last_status_time = time.time()

while True:
    now = datetime.now()
    try:
        val = hx.get_weight(3)
        current_weight = val

        if MODE == Mode.Training:
            if (now - state_change_time).total_seconds() > STATE_CHANGE_DEBOUNCE:
                if val < MAX_WEIGHT:
                    if lid_open:
                        state_change_time = datetime.now()
                        lock()
                else:
                    if not lid_open:
                        state_change_time = datetime.now()
                        unlock()

        else:  # Standard mode
            if (now - state_change_time).total_seconds() > STATE_CHANGE_DEBOUNCE:
                if MAX_WEIGHT < val < MIN_WEIGHT:
                    if not lid_open:
                        if valid and (now - validity_change_time).total_seconds() > VALID_READ_MIN_SECONDS:
                            state_change_time = datetime.now()
                            unlock()
                        if not valid:
                            validity_change_time = datetime.now()
                            log_event("cat_detected", f"Cat detected (weight: {val:.0f})")
                        valid = True
                else:
                    if valid:
                        state_change_time = datetime.now()
                        valid = False
                        log_event("cat_left", f"Cat left (weight: {val:.0f})")
                        lock()

        # ── Periodic status push ──
        now_t = time.time()
        if now_t - last_status_time >= STATUS_INTERVAL:
            send_status()
            last_status_time = now_t

    except (KeyboardInterrupt, SystemExit):
        clean_and_exit()