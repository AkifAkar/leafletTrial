#!/usr/bin/env python3
"""Single-camera libcamera agent for a Raspberry Pi Zero 2 W.

The Pi 5 agent enumerates USB v4l2 cameras and pins them to ports. A Zero 2 W
drives one fixed CSI camera through libcamera, so all of that goes away:

  * there is exactly one camera, its uid given at install as DEVICE_UID;
  * libcamera's ISP scales to arbitrary sizes, so there are no discrete v4l2
    modes to read -- we offer a curated ladder and validate against it;
  * the Zero 2 W (BCM2710, Pi 3 silicon) HAS a hardware H.264 encoder, so
    --hw-accel is the default here. Software encoding on 4x A53 @ 1GHz is not
    viable. (This is the opposite of the Pi 5, which has no hardware encoder.)

It speaks the same MQTT topics as the multi-camera agent, so the Flask app and
the camera page work against it unchanged.

Topics (state retained):
    picam/<uid>/control/config    {"preset": "1280x720", "fps": 30}
    picam/<uid>/control/restart   (payload ignored)
    picam/host/<host>/control/restart  re-detect + restart
    picam/<uid>/state/config      current settings + status
    picam/<uid>/state/modes       the resolution ladder this camera offers
    picam/host/<host>/state       temperature, throttling, camera list
"""

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("pi-webrtc-agent-zero")

DEVICE_UID = os.environ.get("DEVICE_UID", "").strip()
HOST_ID = os.environ.get("HOST_ID", "").strip()
MQTT_HOST = os.environ.get("MQTT_HOST", "10.42.0.1").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CAMERA_SPEC = os.environ.get("CAMERA_SPEC", "libcamera:0").strip()
STREAM_SERVICE = os.environ.get("STREAM_SERVICE", "pi-webrtc-zero.service").strip()
STREAM_ENV = os.environ.get("STREAM_ENV", "/etc/pi-webrtc/stream.env").strip()

DEFAULT_WIDTH = int(os.environ.get("DEFAULT_WIDTH", "1280"))
DEFAULT_HEIGHT = int(os.environ.get("DEFAULT_HEIGHT", "720"))
DEFAULT_FPS = int(os.environ.get("DEFAULT_FPS", "30"))

STARTUP_SETTLE = float(os.environ.get("STARTUP_SETTLE", "3"))
TELEMETRY_INTERVAL = float(os.environ.get("TELEMETRY_INTERVAL", "10"))

# libcamera scales to any size, so this is a curated ladder rather than a
# hardware readout. 1080p is included by request; on a Zero 2 W it will stream
# but is unlikely to hold under encoder load, the same way 1080p60 did not on
# the Pi 5. fps lists are what the ISP/encoder can plausibly sustain.
LADDER = [
    (1920, 1080, [15, 30]),
    (1280, 720, [30, 15]),
    (960, 540, [30, 15]),
    (800, 600, [30, 15]),
    (640, 480, [30, 15]),
    (640, 360, [60, 30, 15]),
    (480, 270, [60, 30, 15]),
]
MODES = [{"preset": f"{w}x{h}", "width": w, "height": h, "fps": fps}
         for (w, h, fps) in LADDER]

HOST_TOPIC = f"picam/host/{HOST_ID}/state"

# Filled at startup: {"present": bool, "name": str}
camera = {"present": True, "name": CAMERA_SPEC}


class ConfigError(Exception):
    """A control message we refuse to act on."""


# --------------------------------------------------------------------------
# camera presence
# --------------------------------------------------------------------------

def detect_camera():
    """Best-effort libcamera presence check.

    Returns (present, name). If no listing tool is found we cannot tell, so we
    assume present rather than refuse to stream a camera that is really there --
    a false "absent" is worse than a false "present", which pi-webrtc's own
    logs will correct.
    """
    for tool in ("rpicam-hello", "libcamera-hello"):
        try:
            out = subprocess.run([tool, "--list-cameras"], capture_output=True,
                                 timeout=10).stdout.decode(errors="replace")
        except FileNotFoundError:
            continue
        except (OSError, subprocess.SubprocessError):
            return True, CAMERA_SPEC        # tool exists but hiccuped; don't block

        if "No cameras available" in out:
            return False, None
        # Lines look like: "0 : imx219 [3280x2464 ...] (/base/soc/i2c0mux/...)"
        match = re.search(r"^\s*\d+\s*:\s*(\S+)", out, re.MULTILINE)
        if match:
            return True, match.group(1)
        return True, CAMERA_SPEC

    return True, CAMERA_SPEC                 # no tool installed: assume present


# --------------------------------------------------------------------------
# config file
# --------------------------------------------------------------------------

def read_stream_env():
    values = {}
    try:
        with open(STREAM_ENV) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    except FileNotFoundError:
        pass

    try:
        width = int(values.get("WIDTH", DEFAULT_WIDTH))
        height = int(values.get("HEIGHT", DEFAULT_HEIGHT))
        fps = int(values.get("FPS", DEFAULT_FPS))
    except ValueError:
        width, height, fps = DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS

    return {"preset": f"{width}x{height}", "width": width, "height": height, "fps": fps}


def write_stream_env(width, height, fps):
    body = (
        "# Managed by pi-webrtc-agent-zero. Hand edits are overwritten on the\n"
        "# next config change. Static settings live in device.env instead.\n"
        f"CAMERA_SPEC={CAMERA_SPEC}\n"
        f"WIDTH={width}\n"
        f"HEIGHT={height}\n"
        f"FPS={fps}\n"
    )
    directory = os.path.dirname(STREAM_ENV) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".stream.env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, STREAM_ENV)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def pick_startup_mode():
    """Last used mode if still in the ladder, else the default, else the top."""
    current = read_stream_env()
    for mode in MODES:
        if mode["preset"] == current["preset"] and current["fps"] in mode["fps"]:
            return current["width"], current["height"], current["fps"]
    for mode in MODES:
        if mode["width"] == DEFAULT_WIDTH and mode["height"] == DEFAULT_HEIGHT:
            fps = DEFAULT_FPS if DEFAULT_FPS in mode["fps"] else mode["fps"][0]
            return mode["width"], mode["height"], fps
    top = MODES[0]
    return top["width"], top["height"], top["fps"][0]


# --------------------------------------------------------------------------
# unit control
# --------------------------------------------------------------------------

def systemctl(*args, check=True):
    return subprocess.run(["systemctl", *args], check=check, timeout=60,
                          capture_output=True)


# --------------------------------------------------------------------------
# MQTT / telemetry
# --------------------------------------------------------------------------

def publish_state(client, state):
    state = dict(state, ts=time.time(), host=HOST_ID)
    client.publish(f"picam/{DEVICE_UID}/state/config", json.dumps(state),
                   qos=1, retain=True)
    return state


def publish_modes(client):
    payload = {
        "name": camera["name"],
        "device": CAMERA_SPEC,
        "port": None,
        # One fixed CSI camera cannot be confused with another, so it counts as
        # pinned -- the page then shows no "which camera is this?" marker.
        "pinned": True,
        "host": HOST_ID,
        "modes": MODES,
    }
    client.publish(f"picam/{DEVICE_UID}/state/modes", json.dumps(payload),
                   qos=1, retain=True)


def clear_camera_state(client):
    client.publish(f"picam/{DEVICE_UID}/state/modes", "", qos=1, retain=True)
    client.publish(f"picam/{DEVICE_UID}/state/config", "", qos=1, retain=True)


def read_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_throttled():
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                             timeout=5, check=True).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
    if not match:
        return None
    raw = int(match.group(1), 16)
    return {
        "raw": match.group(1),
        "undervoltage": bool(raw & (1 << 0)),
        "freq_capped": bool(raw & (1 << 1)),
        "throttled": bool(raw & (1 << 2)),
        "soft_temp_limit": bool(raw & (1 << 3)),
        "undervoltage_since_boot": bool(raw & (1 << 16)),
        "throttled_since_boot": bool(raw & (1 << 18)),
    }


def publish_host_state(client):
    payload = {
        "host": HOST_ID,
        "online": True,
        "temp_c": read_temp_c(),
        # pmic_read_adc is Pi 5 only; on a Zero 2 W this is simply absent.
        "ext5v": None,
        "throttle": read_throttled(),
        "cameras": [DEVICE_UID] if camera["present"] else [],
        "ts": time.time(),
    }
    client.publish(HOST_TOPIC, json.dumps(payload), qos=1, retain=True)


# --------------------------------------------------------------------------
# control handling
# --------------------------------------------------------------------------

def parse_request(payload):
    try:
        req = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError(f"payload is not valid JSON: {exc}")
    if not isinstance(req, dict):
        raise ConfigError("payload must be a JSON object")

    preset = req.get("preset")
    if not isinstance(preset, str):
        raise ConfigError(f"preset must be a string, got {preset!r}")
    mode = next((m for m in MODES if m["preset"] == preset), None)
    if mode is None:
        raise ConfigError(f"unknown preset {preset!r}; allowed: {[m['preset'] for m in MODES]}")

    fps = req.get("fps")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps not in mode["fps"]:
        raise ConfigError(f"unsupported {fps!r}fps at {preset}; allowed: {mode['fps']}")

    return mode["width"], mode["height"], fps


def restart_and_report(client, state):
    try:
        systemctl("restart", STREAM_SERVICE)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        log.error("restart failed: %s", detail)
        publish_state(client, dict(state, status="error", error=f"restart failed: {detail}"))
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("could not restart: %s", exc)
        publish_state(client, dict(state, status="error", error=str(exc)))
        return False

    # Type=simple returns on fork, not when pi-webrtc has joined the broker.
    # Settling avoids the page reconnecting against a peer that isn't ready.
    time.sleep(STARTUP_SETTLE)
    publish_state(client, dict(state, status="applied"))
    return True


def handle_config(client, payload):
    try:
        width, height, fps = parse_request(payload)
    except ConfigError as exc:
        log.warning("rejected config: %s", exc)
        publish_state(client, dict(read_stream_env(), status="error", error=str(exc)))
        return

    state = {"preset": f"{width}x{height}", "width": width, "height": height, "fps": fps}
    log.info("applying %dx%d @ %dfps", width, height, fps)
    publish_state(client, dict(state, status="applying"))

    try:
        write_stream_env(width, height, fps)
    except OSError as exc:
        log.error("could not write %s: %s", STREAM_ENV, exc)
        publish_state(client, dict(state, status="error", error=f"could not write config: {exc}"))
        return

    if restart_and_report(client, state):
        log.info("applied %dx%d @ %dfps", width, height, fps)


def handle_restart(client):
    state = read_stream_env()
    log.info("restart requested (%s @ %sfps)", state["preset"], state["fps"])
    publish_state(client, dict(state, status="applying"))
    if restart_and_report(client, state):
        log.info("restarted")


def handle_host_restart(client):
    """Re-detect the camera and restart the stream."""
    global camera
    log.info("host restart requested; re-detecting camera")
    present, name = detect_camera()
    camera = {"present": present, "name": name or CAMERA_SPEC}

    if not present:
        log.warning("no camera detected")
        clear_camera_state(client)
        try:
            systemctl("stop", STREAM_SERVICE, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        publish_host_state(client)
        return

    publish_modes(client)
    width, height, fps = pick_startup_mode()
    try:
        write_stream_env(width, height, fps)
    except OSError as exc:
        log.error("could not write %s: %s", STREAM_ENV, exc)
    restart_and_report(client, {"preset": f"{width}x{height}", "width": width,
                                "height": height, "fps": fps})
    publish_host_state(client)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log.error("broker refused connection: %s", reason_code)
        return
    log.info("connected to %s:%s", MQTT_HOST, MQTT_PORT)
    client.subscribe(f"picam/{DEVICE_UID}/control/+", qos=1)
    client.subscribe(f"picam/host/{HOST_ID}/control/+", qos=1)

    if camera["present"]:
        publish_modes(client)
        publish_state(client, dict(read_stream_env(), status="applied"))
    else:
        clear_camera_state(client)
    publish_host_state(client)


def on_message(client, userdata, msg):
    # The agent is the only remote way to recover the camera, so it must not die
    # on a malformed message.
    try:
        host_match = re.match(r"picam/host/([^/]+)/control/([^/]+)$", msg.topic)
        if host_match:
            if host_match.group(1) == HOST_ID and host_match.group(2) == "restart":
                handle_host_restart(client)
            return

        match = re.match(r"picam/([^/]+)/control/([^/]+)$", msg.topic)
        if not match or match.group(1) != DEVICE_UID:
            return
        action = match.group(2)
        if action == "config":
            handle_config(client, msg.payload)
        elif action == "restart":
            handle_restart(client)          # payload ignored
        else:
            log.warning("ignoring unknown action %r", action)
    except Exception:
        log.exception("unhandled error while processing a control message")


def make_client():
    """Build an MQTT client on either paho 1.x (Bookworm apt) or 2.x (pip)."""
    client_id = f"pi-webrtc-agent-zero-{DEVICE_UID}"
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)

    if not DEVICE_UID:
        log.error("DEVICE_UID is unset; check /etc/pi-webrtc/device.env")
        return 1
    if not HOST_ID:
        log.error("HOST_ID is unset; check /etc/pi-webrtc/device.env")
        return 1

    global camera
    present, name = detect_camera()
    camera = {"present": present, "name": name or CAMERA_SPEC}
    log.info("camera %s: %s (%s)", DEVICE_UID,
             "present" if present else "NOT DETECTED", camera["name"])

    if present:
        width, height, fps = pick_startup_mode()
        try:
            write_stream_env(width, height, fps)
            log.info("%s: starting at %dx%d @ %dfps", DEVICE_UID, width, height, fps)
            systemctl("restart", STREAM_SERVICE)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not start the stream: %s", exc)

    client = make_client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.will_set(
        HOST_TOPIC,
        json.dumps({"host": HOST_ID, "online": False, "cameras": []}),
        qos=1, retain=True,
    )
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    log.info("agent for host=%s uid=%s", HOST_ID, DEVICE_UID)
    try:
        while True:
            time.sleep(TELEMETRY_INTERVAL)
            publish_host_state(client)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
