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

import collections
import statistics

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
STATE_CHANGE_DEBOUNCE    = 0.5      # ← was 2 — reduced; confirmation window handles timing
VALID_READ_MIN_SECONDS   = 2        # ← was 0 — cat must stay 2 s before unlock
STATUS_INTERVAL          = 1.0      # seconds between status pushes

# ── Scale tuning ──────────────────────────────────────────
RAW_SAMPLES          = 1        # ← was 5 — single sample for speed (HX711 ≈ 10 Hz)
SMOOTHING_WINDOW     = 5        # ← was 10 — tighter window, converges in ~0.5 s
OUTLIER_MAX_DEV      = 500      # reject reads this far from the median
DEAD_ZONE            = 30       # abs(weight) below this → snap to 0
NEGATIVE_TARE_SECS   = 5        # ← replaces AUTO_TARE_HOLD_SECS / AUTO_TARE_THRESHOLD

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
shutdown_event       = threading.Event()
weight_buffer: collections.deque[float] = collections.deque(maxlen=SMOOTHING_WINDOW)
negative_since: float | None = None                   # ← renamed from near_zero_since

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
        log_event("update_checking", "Checking for updates…")
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
    elif action == "tare":
        threading.Thread(target=do_tare, daemon=True).start()


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

    time.sleep(2)

    shutdown_event.set()
    time.sleep(1)

    for fn in [
        lambda: hx.power_down(),
        GPIO.cleanup,
        lambda: sio.disconnect() if SOCKET_ENABLED and sio.connected else None,
    ]:
        try:
            fn()
        except Exception:
            pass

    updater.restart_process()


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
    time.sleep(60)
    while True:
        try:
            update_available = updater.check_for_updates()
            if update_available:
                log_event("update_available", "New version available on remote")
                send_status()
                if AUTO_UPDATE:
                    _do_update_and_restart()
        except Exception as exc:
            print(f"Auto-update error: {exc}")
        time.sleep(UPDATE_CHECK_INTERVAL)

# ── Scale helpers ─────────────────────────────────────────
def get_smoothed_weight() -> float:
    """Read the HX711, reject outliers, return a moving-median weight."""
    raw = hx.get_weight(RAW_SAMPLES)

    # ── power-cycle REMOVED ──────────────────────────────
    # Eliminating the power_down / power_up / sleep saves ~20 ms per
    # loop and lets us read at the full HX711 sample rate (~10 Hz).

    weight_buffer.append(raw)

    if len(weight_buffer) < 3:
        return round(raw, 1)

    med = statistics.median(weight_buffer)
    good = [w for w in weight_buffer if abs(w - med) <= OUTLIER_MAX_DEV]
    smoothed = statistics.mean(good) if good else med

    if abs(smoothed) < DEAD_ZONE:
        smoothed = 0.0

    return round(smoothed, 1)


def do_tare():
    """Zero the scale and clear the smoothing buffer."""
    global negative_since
    weight_buffer.clear()
    negative_since = None
    hx.reset()
    hx.tare()
    log_event("tare", "Scale tared (zeroed)")
    send_status()


def maybe_auto_tare():
    """Re-tare only when the scale reads negative for a sustained period.

    A negative reading means something was sitting on the platform at
    boot (when tare ran) and has since been removed.  All other cases
    (idle at zero, small positive drift) are left alone.
    """
    global negative_since
    now_t = time.time()

    if current_weight < 0 and not valid and not door_open:
        if negative_since is None:
            negative_since = now_t
        elif (now_t - negative_since) >= NEGATIVE_TARE_SECS:
            log_event("auto_tare",
                      f"Auto-tare triggered (negative drift: {current_weight:.0f} g)")
            do_tare()
    else:
        negative_since = None

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
    """Unlock the door and (re)start the auto-lock countdown.

    The timer is restarted on *every* call so that it counts from the
    last moment the cat was confirmed on the scale — giving the cat the
    full UNLOCK_TIME window to push through the flap after stepping off.
    """
    global lock_timer

    # Always cancel the old timer first
    if lock_timer is not None:
        lock_timer.cancel()

    if not GPIO.input(LOCK_RELAY):          # relay currently OFF → unlock
        GPIO.output(LOCK_RELAY, True)
        log_event("unlocked", "Unlocked (auto — cat detected)")
        send_status()

    # (Re)start the countdown — runs from the most recent valid reading
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
    log_event("error", f"Failed to add edge detection: {e}")
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
    shutdown_event.set()
    print("Cleaning up …")
    for fn in [
        lambda: hx.power_down(),
        GPIO.cleanup,
        lambda: sio.disconnect() if SOCKET_ENABLED and sio.connected else None,
    ]:
        try:
            fn()
        except Exception:
            pass
    print("Bye!")
    sys.exit()


# ═══════════════════════════════════════════════════════════
#  MAIN LOOP
# ═══════════════════════════════════════════════════════════
print(f"🐱 ChunkPreventionSystem v{CURRENT_VERSION}  [{MODE.value} mode]")

threading.Thread(target=connect_to_server, daemon=True).start()
threading.Thread(target=auto_update_loop, daemon=True).start()

last_status_time = time.time()

while not shutdown_event.is_set():
    now = datetime.now()
    try:
        current_weight = get_smoothed_weight()
        val = current_weight

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
                        # ── lock() call REMOVED ──────────────────────
                        # The lock_timer started by unlock() provides a
                        # grace window (UNLOCK_TIME) for the cat to push
                        # through the flap.  The door-close timer and
                        # lock_timer handle re-locking automatically.

        # ── Periodic status push ──
        now_t = time.time()
        if now_t - last_status_time >= STATUS_INTERVAL:
            send_status()
            last_status_time = now_t
        # ── Auto-tare when weight is negative ──
        maybe_auto_tare()

        time.sleep(0.01)                              # ← was 0.05

    except RuntimeError:
        if shutdown_event.is_set():
            break
        raise

    except (KeyboardInterrupt, SystemExit):
        clean_and_exit()

# ── Landed here because shutdown_event was set (update restart) ──
if shutdown_event.is_set():
    print("⏳ Main loop stopped — waiting for process restart …")
    time.sleep(30)
    print("⚠️  Restart didn't happen — exiting.")
    sys.exit(1)