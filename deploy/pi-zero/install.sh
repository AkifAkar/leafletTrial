#!/usr/bin/env bash
#
# Install pi-webrtc + single-camera libcamera agent on a Raspberry Pi Zero 2 W.
# Run on the Zero, as root, from a copy of this directory.
#
#   sudo DEVICE_UID=zero1 ./install.sh
#
# DEVICE_UID is the camera's name; it must appear in VALID_CAMERAS in app.py,
# and no other Pi may use the same one. The page is then at /<DEVICE_UID>.
#
set -euo pipefail

DEVICE_UID="${DEVICE_UID:-zero1}"
HOST_ID="${HOST_ID:-$(hostname -s)}"
MQTT_HOST="${MQTT_HOST:-10.42.0.1}"
MQTT_PORT="${MQTT_PORT:-1883}"
CAMERA_SPEC="${CAMERA_SPEC:-libcamera:0}"
STREAM_ENV="${STREAM_ENV:-/etc/pi-webrtc/stream.env}"

# The Zero 2 W (BCM2710, Pi 3 silicon) has a hardware H.264 encoder, so
# --hw-accel is the sensible default -- software encoding on 4x A53 @ 1GHz is
# not viable. Clear it (EXTRA_ARGS='') only if pi-webrtc crash-loops with it.
EXTRA_ARGS="${EXTRA_ARGS:---hw-accel}"

DEFAULT_WIDTH="${DEFAULT_WIDTH:-1280}"
DEFAULT_HEIGHT="${DEFAULT_HEIGHT:-720}"
DEFAULT_FPS="${DEFAULT_FPS:-30}"
TELEMETRY_INTERVAL="${TELEMETRY_INTERVAL:-10}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run with sudo" >&2
  exit 1
fi

# Locate the pi-webrtc binary; upstream leaves it wherever it was extracted.
if [[ -z "${BINARY:-}" ]]; then
  candidates=()
  for path in \
    /opt/pi-webrtc/pi-webrtc \
    /usr/local/bin/pi-webrtc \
    /home/*/pi-webrtc \
    /home/*/pi-webrtc/pi-webrtc \
    /home/*/RaspberryPi-WebRTC/pi-webrtc
  do
    [[ -f "${path}" && -x "${path}" ]] && candidates+=("${path}")
  done
  if [[ "${#candidates[@]}" -eq 1 ]]; then
    BINARY="${candidates[0]}"
    echo "==> Found pi-webrtc at ${BINARY}"
  elif [[ "${#candidates[@]}" -gt 1 ]]; then
    echo "error: found more than one pi-webrtc; pick one explicitly:" >&2
    printf '  sudo BINARY=%s DEVICE_UID=%s ./install.sh\n' "${candidates[@]}" "${DEVICE_UID}" >&2
    exit 1
  else
    echo "error: could not find a pi-webrtc binary." >&2
    echo "Install pi-webrtc first, then re-run with BINARY=/path/to/pi-webrtc" >&2
    exit 1
  fi
fi
if [[ ! -x "${BINARY}" ]]; then
  echo "error: no executable pi-webrtc binary at ${BINARY}" >&2
  exit 1
fi

echo "==> Installing dependencies"
# paho-mqtt from apt (PEP 668 blocks pip on Pi OS). rpicam-apps/libcamera-apps
# gives rpicam-hello, used to confirm the camera is present.
apt-get update -qq
apt-get install -y python3-paho-mqtt
apt-get install -y rpicam-apps 2>/dev/null || apt-get install -y libcamera-apps 2>/dev/null || \
  echo "    (no rpicam-apps/libcamera-apps; camera presence check will assume present)"

echo "==> Writing /etc/pi-webrtc/device.env"
mkdir -p /etc/pi-webrtc
cat > /etc/pi-webrtc/device.env <<EOF
# Host-wide settings, written by install.sh.
# The agent never rewrites this file; it only touches ${STREAM_ENV}.
DEVICE_UID=${DEVICE_UID}
HOST_ID=${HOST_ID}
MQTT_HOST=${MQTT_HOST}
MQTT_PORT=${MQTT_PORT}
CAMERA_SPEC=${CAMERA_SPEC}
STREAM_ENV=${STREAM_ENV}

# Mode to start in, when it is on the ladder.
DEFAULT_WIDTH=${DEFAULT_WIDTH}
DEFAULT_HEIGHT=${DEFAULT_HEIGHT}
DEFAULT_FPS=${DEFAULT_FPS}
TELEMETRY_INTERVAL=${TELEMETRY_INTERVAL}

# Extra pi-webrtc flags. --hw-accel uses the Zero 2 W's hardware H.264 encoder
# and should stay on. Clear this line if pi-webrtc crash-loops with it.
EXTRA_ARGS=${EXTRA_ARGS}
EOF
chmod 644 /etc/pi-webrtc/device.env

echo "==> Installing agent + units"
install -m 755 "${here}/pi-webrtc-agent-zero.py" /usr/local/bin/pi-webrtc-agent-zero.py
install -m 644 "${here}/pi-webrtc-zero.service" /etc/systemd/system/pi-webrtc-zero.service
install -m 644 "${here}/pi-webrtc-agent-zero.service" /etc/systemd/system/pi-webrtc-agent-zero.service

echo "==> Pointing unit at ${BINARY}"
sed -i "s#^ExecStart=[^ ]*pi-webrtc #ExecStart=${BINARY} #" \
  /etc/systemd/system/pi-webrtc-zero.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$(dirname "${BINARY}")#" \
  /etc/systemd/system/pi-webrtc-zero.service
if ! grep -q "^ExecStart=${BINARY} " /etc/systemd/system/pi-webrtc-zero.service; then
  echo "error: failed to set ExecStart in the unit file" >&2
  grep '^ExecStart=' /etc/systemd/system/pi-webrtc-zero.service >&2
  exit 1
fi

echo "==> Enabling the agent (it starts the stream after checking the camera)"
systemctl daemon-reload
systemctl enable pi-webrtc-agent-zero.service
systemctl restart pi-webrtc-agent-zero.service

sleep "${SETTLE:-6}"

echo
echo "Done. host=${HOST_ID}, uid=${DEVICE_UID}, broker=${MQTT_HOST}:${MQTT_PORT}"
echo "      EXTRA_ARGS=${EXTRA_ARGS:-<none>}"
echo
echo "Agent said:"
journalctl -u pi-webrtc-agent-zero -n 15 --no-pager -o cat 2>/dev/null \
  | grep -E '^INFO (camera|.*starting)' | sed 's/^INFO /    /' || echo "    (nothing yet)"
echo
echo "Running command:"
ps -o args= -C pi-webrtc | sed 's/^/    /' || echo "    (not running — check journalctl -u pi-webrtc-zero)"
echo
echo "Logs:   journalctl -u pi-webrtc-agent-zero -u pi-webrtc-zero -f"
echo "Verify: mosquitto_sub -h ${MQTT_HOST} -t 'picam/${DEVICE_UID}/#' -v"
echo "Page:   http://<laptop>:8000/${DEVICE_UID}"
