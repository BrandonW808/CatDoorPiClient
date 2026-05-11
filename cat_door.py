#!/usr/bin/env python3
"""
ChunkPreventionSystem — Cat Door Controller with Remote Server Integration
Supports: remote lock/unlock, live weight, OTA updates, remote weight-range config.
"""

import time
import sys
import threading
from enum import Enum
from datetime import datetime

import RPi.GPIO as GPIO
from hx711 import HX711

import updater
import config_store

# ── Optional: Socket.IO for server connectivity ──────────
try:
    import socketio
    SOCKET_ENABLED = True
except ImportError:
    SOCKET_ENABLED = False
    print("⚠️  python-socketio not installed — running in offline mode")
    print("   Install with:  pip install 'python-socketio[client]'")


# ═══════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════

class Mode(Enum):
    Training = "training"
    Standard = "standard"


# Server
SERVER_URL        = "http://192.168.1.91:3001"
DEVICE_API_KEY    = "catdoor-device-secret-key"
DEVICE_ID         = "catdoor-1"

# Hardware pins
LOCK_RELAY  = 23
DOOR_SENSOR = 17
HX711_DT    = 5
HX711_SCK   = 6
REFERENCE_UNIT = 24.98

# Behaviour (defaults — overridden by local_config.json)
MODE = Mode.Standard
UNLOCK_TIME              = 10
STATE_CHANGE_DEBOUNCE    = 2        # seconds
VALID_READ_MIN_SECONDS   = 0        # seconds
STATUS_INTERVAL          = 1.0      # seconds between status pushes

# ── Load persistent config ────────────────────────────────
_cfg        = config_store.load()
MIN_WEIGHT  = _cfg["min_weight"]
MAX_WEIGHT  = _cfg["max_weight"]
AUTO_UPDATE = _cfg.get("auto_update", True)
UPDATE_CHECK_INTERVAL = _cfg.get("update_check_interval", 1800)

# ── Version ───────────────────────────────────────────────
CURRENT_VERSION = updater.get_git_version()


# ═══════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════
door_open       = False
valid           = False
current_weight: float = 0.0
lock_timer:  threading.Timer | None = None
close_timer: threading.Timer | None = None
lid_open        = False
update_available = False
validity_change_time = datetime.now()
state_change_time    = datetime.now()


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
        log_event("device_connected",
                  f"Device {DEVICE_ID} connected  (v{CURRENT_VERSION})")

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
    if not SOCKET_ENABLED:
        return
    try:
        if sio.connected:
            sio.emit(event, data, namespace="/device")
    except Exception as exc:
        print(f"Socket emit error: {exc}")


def log_event(event_type: str, message: str, data: dict | None = None):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{event_type}] {message}")
    safe_emit("log", {
        "eventType": event_type,
        "message":   message,
        "data":      data or {},
    })


def send_status():
    safe_emit("status_update", {
        "locked":           not GPIO.input(LOCK_RELAY),
        "doorOpen":         door_open,
        "weight":           current_weight,
        "valid":            valid,
        "mode":             MODE.value,
        "minWeight":        MIN_WEIGHT,
        "maxWeight":        MAX_WEIGHT,
        "version":          CURRENT_VERSION,
        "updateAvailable":  update_available,
    })


# ── Remote commands ───────────────────────────────────────
def _handle_remote_command(data: dict):
    global MIN_WEIGHT, MAX_WEIGHT, update_available

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

    elif action == "check_update":
        log_event("update_checking", "Checking for updates…")   # ← NEW
        update_available = updater.check_for_updates()
        msg = "Update available" if update_available else "Already up-to-date"
        log_event("update_check", msg,
                  {"updateAvailable": update_available,
                   "version": CURRENT_VERSION})
        send_status()

    elif action == "trigger_update":
        threading.Thread(target=_do_update_and_restart, daemon=True).start()

    elif action == "set_weight_range":
        new_min = data.get("minWeight")
        new_max = data.get("maxWeight")
        if new_min is not None:
            MIN_WEIGHT = float(new_min)
        if new_max is not None:
            MAX_WEIGHT = float(new_max)
        config_store.update({
            "min_weight": MIN_WEIGHT,
            "max_weight": MAX_WEIGHT,
        })
        log_event("config_changed",
                  f"Weight range → {MAX_WEIGHT} … {MIN_WEIGHT}",
                  {"minWeight": MIN_WEIGHT, "maxWeight": MAX_WEIGHT})
        send_status()


def _do_update_and_restart():
    """Pull latest code + restart (runs in its own thread)."""
    global update_available, CURRENT_VERSION

    log_event("update_started", "Pulling latest code from GitHub …")
    send_status()

    if not updater.pull_latest():
        log_event("update_failed", "Git pull failed")
        send_status()
        return

    updater.install_requirements()

    new_ver = updater.get_git_version()
    log_event("update_complete",
              f"Updated to {new_ver}. Restarting …",
              {"oldVersion": CURRENT_VERSION, "newVersion": new_ver})
    update_available = False
    CURRENT_VERSION = new_ver
    send_status()

    time.sleep(3)                       # let socket.io flush

    try:
        hx.power_down()
        GPIO.cleanup()
        if SOCKET_ENABLED and sio.connected:
            sio.disconnect()
    except Exception:
        pass

    updater.restart_process()           # replaces the process


# ── Server connection (background) ────────────────────────
def connect_to_server():
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
            break
        except Exception as exc:
            print(f"Connection failed: {exc}  — retrying in 10 s …")
            time.sleep(10)


# ── Auto-update loop (background) ─────────────────────────
def auto_update_loop():
    global update_available
    time.sleep(60)                      # let the system settle after boot
    while True:
        try:
            update_available = updater.check_for_updates()
            if update_available:
                log_event("update_available", "New version available on remote")
                send_status()
                if AUTO_UPDATE:
                    _do_update_and_restart()
            # If no update, just continue
        except Exception as exc:
            print(f"Auto-update error: {exc}")
        time.sleep(UPDATE_CHECK_INTERVAL)


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
    if not GPIO.input(LOCK_RELAY):
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
        door_open = True
        log_event("door_opened", "Door opened 🚪")
        send_status()
        if close_timer is not None:
            close_timer.cancel()
            close_timer = None
    else:
        door_open = False
        log_event("door_closed", "Door closed 🔒")
        send_status()
        close_timer = threading.Timer(3.0, lock)
        close_timer.start()


try:
    GPIO.add_event_detect(
        DOOR_SENSOR,
        GPIO.BOTH,
        callback=read_sensor,
        bouncetime=500,
    )
except RuntimeError as e:
    # This uses your existing function to log locally and send to Socket.IO
    log_event("error", f"Failed to add edge detection: {e}")
    
    # You can also add a standard print statement just in case
    print(f"⚠️  GPIO Warning: Could not register door sensor. Error: {e}")

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
print(f"🐱 ChunkPreventionSystem v{CURRENT_VERSION}  [{MODE.value} mode]")

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
                            log_event("cat_detected",
                                      f"Cat detected (weight: {val:.0f})")
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