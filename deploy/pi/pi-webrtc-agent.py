#!/usr/bin/env python3
"""Run and configure one pi-webrtc per camera on this Pi, driven over MQTT.

pi-webrtc only reads width/height/fps as startup arguments, so changing them
means rewriting a systemd EnvironmentFile and restarting the unit. This agent
does that, and additionally:

  * enumerates the v4l2 capture devices at startup and starts one
    pi-webrtc@<uid> instance per camera it finds;
  * reads each camera's real supported modes out of v4l2, so the page can only
    ever offer resolutions the sensor actually has. V4L2 does not fail on an
    unsupported mode -- it quietly substitutes the nearest one it does have, so
    asking for 1080p on a 720p sensor gives you 720p while every config file
    claims 1080p. Reading the modes is the only way to stop lying to the user;
  * publishes CPU temperature and throttle state, because sustained software
    encoding on a Pi will hit the thermal ceiling long before it runs out of
    anything else.

Topics (all state retained):
    picam/<uid>/control/config    {"preset": "1280x720", "fps": 30}
    picam/<uid>/control/restart   (payload ignored)
    picam/<uid>/state/config      current settings + status
    picam/<uid>/state/modes       what this camera actually supports
    picam/<uid>/state/agent       online | offline (last-will backed)
    picam/host/<host>/state       temperature, throttling, camera list

The allowlist is the security boundary, not a convenience: the broker runs with
allow_anonymous, so anything on the subnet can publish here, and this process is
root. Requests are matched against modes read from the hardware and never
interpolated into a shell.
"""

import glob
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time

import paho.mqtt.client as mqtt

log = logging.getLogger("pi-webrtc-agent")

HOST_ID = os.environ.get("HOST_ID", "").strip()
MQTT_HOST = os.environ.get("MQTT_HOST", "10.42.0.1").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
CAMERA_UIDS = [u.strip() for u in os.environ.get("CAMERA_UIDS", "").split(",") if u.strip()]
CAMERA_ENV_DIR = os.environ.get("CAMERA_ENV_DIR", "/etc/pi-webrtc/cameras").strip()


def _parse_ports(raw):
    """CAMERA_PORTS='camera1:1-1.2,camera2:1-1.3' -> {'camera1': '1-1.2', ...}"""
    mapping = {}
    for part in raw.split(","):
        uid, _, port = part.strip().partition(":")
        if uid.strip() and port.strip():
            mapping[uid.strip()] = port.strip()
    return mapping


# Pin uids to physical USB ports. Worth doing once you care which camera is
# which: identical models are indistinguishable by name, and most report the
# same useless serial (0001, or none), so the port is the only stable handle.
CAMERA_PORTS = _parse_ports(os.environ.get("CAMERA_PORTS", ""))

DEFAULT_WIDTH = int(os.environ.get("DEFAULT_WIDTH", "1280"))
DEFAULT_HEIGHT = int(os.environ.get("DEFAULT_HEIGHT", "720"))
DEFAULT_FPS = int(os.environ.get("DEFAULT_FPS", "30"))

# Seconds to let pi-webrtc come up before reporting success. See restart_and_report.
STARTUP_SETTLE = float(os.environ.get("STARTUP_SETTLE", "3"))
TELEMETRY_INTERVAL = float(os.environ.get("TELEMETRY_INTERVAL", "10"))

HOST_TOPIC = f"picam/host/{HOST_ID}/state"

# uid -> {"device": int, "path": str, "name": str, "modes": [...]}
cameras = {}


class ConfigError(Exception):
    """A control message we refuse to act on."""


# --------------------------------------------------------------------------
# v4l2 discovery
# --------------------------------------------------------------------------

def v4l2_device_name(index):
    try:
        with open(f"/sys/class/video4linux/video{index}/name") as fh:
            return fh.read().strip()
    except OSError:
        return f"video{index}"


def usb_port(index):
    """Physical USB port a video node hangs off, e.g. '1-6' or '1-1.2'.

    This is the only thing that reliably tells identical cameras apart: they
    share a model name, and most report the same serial or none at all. It
    stays the same across reboots as long as the camera stays in that socket.

    Returns None for non-USB cameras (a CSI camera on a Pi, say), which then
    fall back to /dev/video ordering.
    """
    try:
        link = os.readlink(f"/sys/class/video4linux/video{index}/device")
    except OSError:
        return None
    # '../../../1-6:1.0' -> '1-6'; the ':1.0' is the USB interface, not the port
    node = os.path.basename(link).split(":")[0]
    return node or None


def _port_sort_key(port):
    """Order ports numerically: 1-1.2 before 1-6, which a string sort gets wrong."""
    if not port:
        return (1, [])          # unknown ports sort last, deterministically
    return (0, [int(n) for n in re.findall(r"\d+", port)])


def _video_index(path):
    return int(re.search(r"(\d+)$", path).group(1))


def v4l2_modes(path):
    """Modes a device really supports, as [{preset, width, height, fps: [...]}].

    Returns [] for nodes that cannot capture. A USB camera usually exposes
    several /dev/video* nodes -- the extra ones carry metadata and report no
    frame sizes, which is exactly how we tell them apart from the real one.
    """
    try:
        out = subprocess.run(
            ["v4l2-ctl", "--list-formats-ext", "-d", path],
            capture_output=True, timeout=10, check=True,
        ).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []

    # Collect every discrete size across all pixel formats, unioning the frame
    # rates. pi-webrtc picks the pixel format itself, so what matters here is
    # which geometry/rate pairs exist at all.
    sizes = {}
    current = None
    for line in out.splitlines():
        size_match = re.search(r"Size:\s+Discrete\s+(\d+)x(\d+)", line)
        if size_match:
            current = (int(size_match.group(1)), int(size_match.group(2)))
            sizes.setdefault(current, set())
            continue
        fps_match = re.search(r"\(([\d.]+)\s*fps\)", line)
        if fps_match and current:
            sizes[current].add(int(round(float(fps_match.group(1)))))

    modes = []
    for (width, height), fps in sorted(sizes.items(), key=lambda kv: -(kv[0][0] * kv[0][1])):
        if not fps:
            continue
        modes.append({
            "preset": f"{width}x{height}",
            "width": width,
            "height": height,
            "fps": sorted(fps, reverse=True),
        })
    return modes


def discover_cameras():
    """Map configured uids onto the capture devices actually present."""
    devices = []
    for path in sorted(glob.glob("/dev/video*"), key=_video_index):
        index = _video_index(path)
        modes = v4l2_modes(path)
        if not modes:
            continue
        devices.append({
            "device": index,
            "path": path,
            "name": v4l2_device_name(index),
            "port": usb_port(index),
            "modes": modes,
        })

    # /dev/video numbering follows USB probe order, which is not guaranteed to
    # be the same after a reboot. Ordering by physical port instead keeps a uid
    # pointing at the same socket -- which matters most with identical cameras,
    # where a silent reshuffle is invisible.
    devices.sort(key=lambda d: _port_sort_key(d["port"]))

    found = {}
    if CAMERA_PORTS:
        by_port = {d["port"]: d for d in devices if d["port"]}
        for uid, port in CAMERA_PORTS.items():
            if port in by_port:
                found[uid] = by_port[port]
            else:
                log.warning("%s is pinned to USB port %s, which has no camera", uid, port)
        for uid in CAMERA_PORTS:
            if uid not in CAMERA_UIDS:
                log.warning("%s is pinned to a port but is not in CAMERA_UIDS", uid)
    else:
        for uid, dev in zip(CAMERA_UIDS, devices):
            found[uid] = dev

    for uid, cam in found.items():
        log.info("camera %s -> %s port=%s (%s), %d modes, best %s",
                 uid, cam["path"], cam["port"] or "?", cam["name"],
                 len(cam["modes"]), cam["modes"][0]["preset"])

    if not CAMERA_PORTS and len(devices) > len(CAMERA_UIDS):
        log.warning("%d cameras present but only %d uids configured; ignoring the rest",
                    len(devices), len(CAMERA_UIDS))
    if not found:
        log.warning("no v4l2 capture devices found")
    elif not CAMERA_PORTS and len(found) > 1:
        ports = ",".join(f"{uid}:{cam['port']}" for uid, cam in found.items() if cam["port"])
        if ports:
            log.info("to pin these to their sockets, set in device.env:  CAMERA_PORTS=%s", ports)
    return found


# --------------------------------------------------------------------------
# per-camera config files
# --------------------------------------------------------------------------

def env_path(uid):
    return os.path.join(CAMERA_ENV_DIR, f"{uid}.env")


def read_camera_env(uid):
    """Current settings for one camera, falling back to defaults."""
    values = {}
    try:
        with open(env_path(uid)) as fh:
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


def write_camera_env(uid, width, height, fps):
    """Replace a camera's env file atomically."""
    cam = cameras[uid]
    body = (
        "# Managed by pi-webrtc-agent. Hand edits are overwritten on the next\n"
        "# config change. Host-wide settings live in device.env instead.\n"
        f"CAMERA_SPEC=v4l2:{cam['device']}\n"
        f"WIDTH={width}\n"
        f"HEIGHT={height}\n"
        f"FPS={fps}\n"
    )
    os.makedirs(CAMERA_ENV_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CAMERA_ENV_DIR, prefix=f".{uid}.env.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, env_path(uid))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def pick_startup_mode(uid):
    """Settings to boot this camera with: last used, if it is still valid."""
    current = read_camera_env(uid)
    modes = cameras[uid]["modes"]
    for mode in modes:
        if mode["preset"] == current["preset"] and current["fps"] in mode["fps"]:
            return current["width"], current["height"], current["fps"]

    # Either first run, or the camera was swapped for one without that mode.
    # Prefer the default if this camera has it, else its highest mode.
    for mode in modes:
        if mode["width"] == DEFAULT_WIDTH and mode["height"] == DEFAULT_HEIGHT:
            fps = DEFAULT_FPS if DEFAULT_FPS in mode["fps"] else mode["fps"][0]
            return mode["width"], mode["height"], fps
    best = modes[0]
    return best["width"], best["height"], best["fps"][0]


# --------------------------------------------------------------------------
# unit control
# --------------------------------------------------------------------------

def unit_for(uid):
    return f"pi-webrtc@{uid}.service"


def systemctl(*args, check=True):
    return subprocess.run(
        ["systemctl", *args], check=check, timeout=60, capture_output=True
    )


def stop_stale_units():
    """Stop pi-webrtc instances for cameras that are no longer present."""
    try:
        out = systemctl("list-units", "--all", "--no-legend", "--plain",
                        "pi-webrtc@*.service").stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        unit = line.split()[0] if line.split() else ""
        match = re.match(r"pi-webrtc@(.+)\.service$", unit)
        if match and match.group(1) not in cameras:
            log.info("stopping %s (camera no longer present)", unit)
            systemctl("stop", unit, check=False)


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def publish_state(client, uid, state):
    """Publish retained so a page loading later still sees current settings."""
    state = dict(state, ts=time.time(), host=HOST_ID)
    client.publish(f"picam/{uid}/state/config", json.dumps(state), qos=1, retain=True)
    return state


def publish_modes(client, uid):
    cam = cameras[uid]
    payload = {
        "name": cam["name"],
        "device": cam["path"],
        # Identical cameras report identical names, so the port is the only way
        # to tell on-screen which physical camera you are looking at.
        "port": cam["port"],
        "pinned": bool(CAMERA_PORTS),
        "host": HOST_ID,
        "modes": cam["modes"],
    }
    client.publish(f"picam/{uid}/state/modes", json.dumps(payload), qos=1, retain=True)


def read_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_throttled():
    """Raspberry Pi throttle bitmask, or None off-Pi."""
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
        # Bits 0-3 are live state; 16-19 are sticky "has happened since boot".
        "undervoltage": bool(raw & (1 << 0)),
        "freq_capped": bool(raw & (1 << 1)),
        "throttled": bool(raw & (1 << 2)),
        "soft_temp_limit": bool(raw & (1 << 3)),
        "undervoltage_since_boot": bool(raw & (1 << 16)),
        "throttled_since_boot": bool(raw & (1 << 18)),
    }


def clear_absent_cameras(client):
    """Drop retained state for configured uids that are not present.

    Retained state outlives the process, so without this a camera that has been
    unplugged -- or that failed to power up, which is easy with several USB
    cameras -- keeps advertising modes and settings forever, and the page shows
    them as though it were there. Sweeping all of CAMERA_UIDS rather than just
    the previous run's cameras also clears leftovers from older runs.
    """
    for uid in CAMERA_UIDS:
        if uid not in cameras:
            client.publish(f"picam/{uid}/state/modes", "", qos=1, retain=True)
            client.publish(f"picam/{uid}/state/config", "", qos=1, retain=True)


def read_ext5v():
    """The 5V input rail as the Pi 5 itself measures it, in volts.

    Worth more than a multimeter at the regulator: this is the voltage that
    actually arrives after the drop across the wiring, which is what the Pi
    browns out on. Pi 5 only; None elsewhere.
    """
    try:
        out = subprocess.run(["vcgencmd", "pmic_read_adc"], capture_output=True,
                             timeout=5, check=True).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"EXT5V_V\s+volt\(\d+\)=([\d.]+)V", out)
    return round(float(match.group(1)), 2) if match else None


def publish_host_state(client):
    payload = {
        "host": HOST_ID,
        "online": True,          # the last-will flips this to false if we vanish
        "temp_c": read_temp_c(),
        "ext5v": read_ext5v(),
        "throttle": read_throttled(),
        "cameras": sorted(cameras),
        "ts": time.time(),
    }
    client.publish(HOST_TOPIC, json.dumps(payload), qos=1, retain=True)


# --------------------------------------------------------------------------
# control handling
# --------------------------------------------------------------------------

def parse_request(uid, payload):
    """Validate a control payload against what this camera really supports."""
    try:
        req = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError(f"payload is not valid JSON: {exc}")
    if not isinstance(req, dict):
        raise ConfigError("payload must be a JSON object")

    # Check the type before the lookup: an unhashable value like {} would raise
    # TypeError out of a comparison rather than being rejected.
    preset = req.get("preset")
    if not isinstance(preset, str):
        raise ConfigError(f"preset must be a string, got {preset!r}")

    mode = next((m for m in cameras[uid]["modes"] if m["preset"] == preset), None)
    if mode is None:
        available = [m["preset"] for m in cameras[uid]["modes"]]
        raise ConfigError(f"{uid} does not support {preset!r}; has: {available}")

    fps = req.get("fps")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps not in mode["fps"]:
        raise ConfigError(f"{uid} does not support {fps!r}fps at {preset}; has: {mode['fps']}")

    return mode["width"], mode["height"], fps


def restart_and_report(client, uid, state):
    """Restart one camera's unit, reporting the outcome on its state topic.

    A successful restart only means systemd started the process. If pi-webrtc
    then exits, that shows up as a crash loop in `journalctl -u pi-webrtc@<uid>`,
    not here.
    """
    try:
        systemctl("restart", unit_for(uid))
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        log.error("restarting %s failed: %s", uid, detail)
        publish_state(client, uid, dict(state, status="error",
                                        error=f"restart failed: {detail}"))
        return False
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("could not restart %s: %s", uid, exc)
        publish_state(client, uid, dict(state, status="error", error=str(exc)))
        return False

    # Type=simple means systemctl returns once the process is forked, not once
    # pi-webrtc has opened the camera and joined the broker. Reporting success
    # immediately makes the page reconnect too early, burn its whole connection
    # timeout against a peer that isn't listening yet, and then back off.
    time.sleep(STARTUP_SETTLE)

    publish_state(client, uid, dict(state, status="applied"))
    return True


def handle_config(client, uid, payload):
    try:
        width, height, fps = parse_request(uid, payload)
    except ConfigError as exc:
        log.warning("rejected config for %s: %s", uid, exc)
        publish_state(client, uid, dict(read_camera_env(uid), status="error", error=str(exc)))
        return

    state = {"preset": f"{width}x{height}", "width": width, "height": height, "fps": fps}
    log.info("%s: applying %dx%d @ %dfps", uid, width, height, fps)
    publish_state(client, uid, dict(state, status="applying"))

    try:
        write_camera_env(uid, width, height, fps)
    except OSError as exc:
        log.error("could not write %s: %s", env_path(uid), exc)
        publish_state(client, uid, dict(state, status="error",
                                        error=f"could not write config: {exc}"))
        return

    if restart_and_report(client, uid, state):
        log.info("%s: applied %dx%d @ %dfps", uid, width, height, fps)


def handle_restart(client, uid):
    state = read_camera_env(uid)
    log.info("%s: restart requested (%s @ %sfps)", uid, state["preset"], state["fps"])
    publish_state(client, uid, dict(state, status="applying"))
    if restart_and_report(client, uid, state):
        log.info("%s: restarted", uid)


def handle_host_restart(client):
    """Re-enumerate the cameras and restart every stream on this Pi.

    A camera that failed to power up (USB current limits are easy to hit with
    several cameras) enumerates far enough for the Pi to read its name but never
    configures, so it is absent at startup and stays absent. Re-running discovery
    is the only way to pick it up again without rebooting.
    """
    global cameras
    log.info("host restart requested; re-enumerating cameras")

    previous = set(cameras)
    cameras = discover_cameras()
    for uid in previous - set(cameras):
        log.info("%s is gone", uid)
    clear_absent_cameras(client)
    stop_stale_units()

    for uid in cameras:
        state = dict(read_camera_env(uid), status="applying")
        publish_state(client, uid, state)

    for uid in cameras:
        width, height, fps = pick_startup_mode(uid)
        try:
            write_camera_env(uid, width, height, fps)
        except OSError as exc:
            log.error("could not write %s: %s", env_path(uid), exc)
            continue
        publish_modes(client, uid)
        restart_and_report(client, uid,
                           {"preset": f"{width}x{height}", "width": width,
                            "height": height, "fps": fps})

    publish_host_state(client)
    log.info("host restart done; running %s", sorted(cameras) or "no cameras")


def on_connect(client, userdata, flags, reason_code, properties=None):
    # paho 1.x passes an int rc; 2.x passes a ReasonCode that compares equal to
    # its int value, so this check holds on both.
    if reason_code != 0:
        log.error("broker refused connection: %s", reason_code)
        return
    log.info("connected to %s:%s", MQTT_HOST, MQTT_PORT)
    # Subscribe for every configured uid, not just the ones found: a host
    # restart can discover a camera that was absent at boot, and re-subscribing
    # from a callback is easy to get wrong.
    for uid in CAMERA_UIDS:
        client.subscribe(f"picam/{uid}/control/+", qos=1)
    client.subscribe(f"picam/host/{HOST_ID}/control/+", qos=1)

    clear_absent_cameras(client)
    for uid in cameras:
        publish_modes(client, uid)
        # Re-announce what is actually on disk, in case we missed a change.
        publish_state(client, uid, dict(read_camera_env(uid), status="applied"))
    publish_host_state(client)


def on_message(client, userdata, msg):
    # This agent is the only thing that can restore a camera remotely, so it
    # must not die on a malformed message -- that would need a physical visit.
    try:
        host_match = re.match(r"picam/host/([^/]+)/control/([^/]+)$", msg.topic)
        if host_match:
            if host_match.group(1) != HOST_ID:
                return
            if host_match.group(2) == "restart":
                handle_host_restart(client)   # payload ignored
            else:
                log.warning("ignoring unknown host action %r", host_match.group(2))
            return

        match = re.match(r"picam/([^/]+)/control/([^/]+)$", msg.topic)
        if not match:
            return
        uid, action = match.group(1), match.group(2)
        if uid not in cameras:
            log.warning("ignoring control for camera %s, which is not on this host", uid)
            return
        if action == "config":
            handle_config(client, uid, msg.payload)
        elif action == "restart":
            handle_restart(client, uid)   # payload ignored: restart takes no args
        else:
            log.warning("ignoring unknown action %r for %s", action, uid)
    except Exception:
        log.exception("unhandled error while processing a control message")


def make_client():
    """Build an MQTT client on either paho 1.x or 2.x.

    Debian/Raspberry Pi OS ships 1.6.1 as python3-paho-mqtt, which has no
    CallbackAPIVersion and would raise AttributeError here. The callbacks above
    take `properties` with a default so one signature serves both APIs.
    """
    client_id = f"pi-webrtc-agent-{HOST_ID}"
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    except AttributeError:
        return mqtt.Client(client_id=client_id)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)

    if not HOST_ID:
        log.error("HOST_ID is unset; check /etc/pi-webrtc/device.env")
        return 1
    if not CAMERA_UIDS:
        log.error("CAMERA_UIDS is unset; check /etc/pi-webrtc/device.env")
        return 1

    global cameras
    cameras = discover_cameras()
    stop_stale_units()

    for uid in cameras:
        width, height, fps = pick_startup_mode(uid)
        try:
            write_camera_env(uid, width, height, fps)
            log.info("%s: starting at %dx%d @ %dfps", uid, width, height, fps)
            systemctl("restart", unit_for(uid))
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("could not start %s: %s", uid, exc)

    client = make_client()
    client.on_connect = on_connect
    client.on_message = on_message
    # MQTT allows exactly one will per connection, and the agent is per-host, so
    # online/offline is a property of the host rather than of each camera. A
    # will per camera would silently keep only the last one set.
    client.will_set(
        HOST_TOPIC,
        json.dumps({"host": HOST_ID, "online": False, "cameras": []}),
        qos=1, retain=True,
    )
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    log.info("agent for host=%s managing %s", HOST_ID, sorted(cameras) or "no cameras")
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
