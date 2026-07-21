import datetime
import errno
import json
import os
import threading
import uuid

import paho.mqtt.client as mqtt
from flask import Flask, abort, jsonify, render_template, request

app = Flask(__name__)

# A 1080p PNG runs to a few MB; this is headroom, not a target.
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

SCREENSHOT_DIR = os.path.expanduser(
    os.environ.get("SCREENSHOT_DIR", "~/Pictures/Screenshots")
)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

@app.route('/map')
def index():
    return render_template('index.html')


# Camera uids a Pi may claim. The agent on each Pi enumerates its v4l2 devices
# and names them from its own CAMERA_UIDS list, in /dev/video order — so make
# sure two Pis are not configured to claim the same name.
VALID_CAMERAS = {'camera1', 'camera2', 'camera3', 'camera4', "camera5", "zero1", "zero2"}

# The laptop's own address on the link the Pis sit behind. Override for local
# testing when that interface is down: MQTT_HOST=127.0.0.1 python3 app.py
MQTT_HOST = os.environ.get("MQTT_HOST", "10.42.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Resolutions are not hardcoded: each agent reads its cameras' real modes out of
# v4l2 and publishes them, because V4L2 substitutes the nearest mode it has
# rather than failing — so a hardcoded 1920x1080 silently becomes 720p on a
# sensor that lacks it, with every config still claiming 1080p.
_camera_state = {}      # uid  -> {"config": {...}, "modes": {...}}
_host_state = {}        # host -> {"temp_c": ..., "throttle": {...}, ...}
_state_lock = threading.Lock()


def _on_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe("picam/+/state/+", qos=1)
    client.subscribe("picam/host/+/state", qos=1)


def _on_message(client, userdata, msg):
    parts = msg.topic.split("/")

    # An empty retained payload means "forget this" — the agent clears these for
    # a camera that has gone away. It is not JSON, so it has to be handled before
    # parsing, or a vanished camera would keep showing its last known settings.
    cleared = not msg.payload
    payload = None
    if not cleared:
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return

    with _state_lock:
        # picam/host/<host>/state
        if len(parts) == 4 and parts[1] == "host" and parts[3] == "state":
            host = parts[2]
            if cleared:
                _host_state.pop(host, None)
            elif payload.get("online") is False and _host_state.get(host):
                # This is the agent's last will, which carries no readings. Merge
                # it over the previous message rather than replacing: when a Pi
                # dies of a power fault, the last temperature and rail voltage
                # before it went are the whole diagnosis, and a plain overwrite
                # would throw them away.
                _host_state[host] = {**_host_state[host], **payload}
            else:
                _host_state[host] = payload
            return
        # picam/<uid>/state/<kind>
        if len(parts) == 4 and parts[2] == "state":
            uid, kind = parts[1], parts[3]
            if kind not in ("config", "modes"):
                return
            if cleared:
                _camera_state.get(uid, {}).pop(kind, None)
            else:
                _camera_state.setdefault(uid, {})[kind] = payload


# Flask's reloader runs two processes. A shared client_id would make the broker
# kick one off as a duplicate, so keep it unique per process.
_mqtt = mqtt.Client(
    mqtt.CallbackAPIVersion.VERSION2,
    client_id=f"leaflet-flask-{uuid.uuid4().hex[:8]}",
)
_mqtt.on_connect = _on_connect
_mqtt.on_message = _on_message
_mqtt.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
_mqtt.loop_start()


def _camera_view(camera_id):
    """Everything known about one camera, joined with its Pi's telemetry."""
    with _state_lock:
        entry = dict(_camera_state.get(camera_id, {}))
        modes = entry.get("modes") or {}
        config = entry.get("config")
        host_id = (config or {}).get("host") or modes.get("host")
        host = dict(_host_state.get(host_id, {})) if host_id else {}

    # The agent's last will flips host.online to false, so a camera is only
    # really reachable if its Pi is.
    online = bool(host.get("online")) and camera_id in (host.get("cameras") or [])
    return {
        "uid": camera_id,
        "online": online,
        "config": config,
        "name": modes.get("name"),
        "device": modes.get("device"),
        # Identical camera models share a name, so the USB port is what tells
        # them apart on screen.
        "port": modes.get("port"),
        "pinned": modes.get("pinned", False),
        "modes": modes.get("modes") or [],
        "host": host,
    }


@app.route('/<camera_id>')
def camera_view(camera_id):
    if camera_id not in VALID_CAMERAS:
        abort(404)
    return render_template('cameras.html', camera_id=camera_id, mqtt_host=MQTT_HOST)


@app.get('/api/camera/<camera_id>/config')
def get_camera_config(camera_id):
    if camera_id not in VALID_CAMERAS:
        abort(404)
    return jsonify(_camera_view(camera_id))


@app.post('/api/camera/<camera_id>/config')
def set_camera_config(camera_id):
    if camera_id not in VALID_CAMERAS:
        abort(404)

    body = request.get_json(silent=True) or {}
    preset = body.get("preset")
    fps = body.get("fps")

    # Checked here only for a decent error message. The agent validates against
    # the hardware and is the actual boundary — the broker is anonymous, so
    # anything on the subnet can bypass this and publish directly.
    view = _camera_view(camera_id)
    modes = view["modes"]
    if modes:
        mode = next((m for m in modes if m["preset"] == preset), None)
        if mode is None:
            return jsonify({
                "error": f"{camera_id} does not support {preset!r}",
                "allowed": [m["preset"] for m in modes],
            }), 400
        if fps not in mode["fps"]:
            return jsonify({
                "error": f"{camera_id} does not support {fps!r}fps at {preset}",
                "allowed": mode["fps"],
            }), 400

    payload = json.dumps({"preset": preset, "fps": fps})
    info = _mqtt.publish(f"picam/{camera_id}/control/config", payload, qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "could not reach the MQTT broker"}), 502

    # The agent restarts the stream, so the answer arrives later on the state
    # topic. The page polls GET for the outcome.
    return jsonify({"status": "requested", "preset": preset, "fps": fps}), 202


@app.post('/api/camera/<camera_id>/restart')
def restart_camera(camera_id):
    """Restart every camera on the Pi this camera belongs to.

    Host-wide rather than per-camera on purpose: the agent re-enumerates v4l2 as
    part of this, which is the only way to pick up a camera that failed to power
    up at boot — and a camera that never appeared has no uid to restart.
    """
    if camera_id not in VALID_CAMERAS:
        abort(404)

    view = _camera_view(camera_id)
    host = (view.get("config") or {}).get("host") or (view.get("host") or {}).get("host")
    if not host:
        return jsonify({
            "error": f"don't know which Pi {camera_id} is on yet — it has never reported in"
        }), 409

    info = _mqtt.publish(f"picam/host/{host}/control/restart", "", qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "could not reach the MQTT broker"}), 502

    return jsonify({"status": "requested", "host": host}), 202


@app.post('/api/camera/<camera_id>/screenshot')
def save_screenshot(camera_id):
    """Write a frame the page captured to SCREENSHOT_DIR.

    The browser sends the PNG bytes; the filename is built here from the
    validated camera_id and the clock, never from anything the client sends. A
    client-supplied name is a path traversal waiting to happen, and there is no
    reason to accept one.
    """
    if camera_id not in VALID_CAMERAS:
        abort(404)

    data = request.get_data()
    if not data:
        return jsonify({"error": "no image data"}), 400
    if not data.startswith(PNG_MAGIC):
        return jsonify({"error": "not a PNG"}), 400

    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"cannot create {SCREENSHOT_DIR}: {exc}"}), 500

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Sortable and grouped by camera: camera1_20260715-194404.png
    for suffix in ("", *(f"_{n}" for n in range(2, 100))):
        name = f"{camera_id}_{stamp}{suffix}.png"
        path = os.path.join(SCREENSHOT_DIR, name)
        try:
            # O_EXCL rather than checking os.path.exists first: two quick clicks
            # would race that check and one would silently overwrite the other.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                continue
            return jsonify({"error": f"cannot write {path}: {exc}"}), 500
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            return jsonify({"error": f"cannot write {path}: {exc}"}), 500
        return jsonify({"file": name, "dir": SCREENSHOT_DIR, "bytes": len(data)}), 201

    return jsonify({"error": "too many screenshots in the same second"}), 507


ROSBRIDGE_URL = "ws://127.0.0.1:9090"
IMAGE_TOPIC   = "/zed/zed_node/rgb/image_rect_color/compressed"  # or "/webcam_image" for raw
MSG_TYPE      = "sensor_msgs/msg/CompressedImage"  # "sensor_msgs/Image" if raw


@app.route("/zed")
def zed():
    return render_template("zed.html",
                           rosbridge_url=ROSBRIDGE_URL,
                           image_topic=IMAGE_TOPIC,
                           msg_type=MSG_TYPE)

if __name__ == '__main__':
    # listen on 127.0.0.1:8000
    app.run(host='127.0.0.1', port=8000, debug=True)
