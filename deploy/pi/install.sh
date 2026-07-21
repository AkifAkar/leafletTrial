#!/usr/bin/env bash
#
# Install the pi-webrtc stream units + config agent on a Raspberry Pi.
# Run on the Pi, as root, from a copy of this directory.
#
#   sudo ./install.sh
#
# The agent enumerates the v4l2 cameras at startup and runs one pi-webrtc per
# camera, naming them from CAMERA_UIDS in order. Override anything at install:
#
#   sudo HOST_ID=zero2 CAMERA_UIDS=camera4,camera5 ./install.sh
#
set -euo pipefail

HOST_ID="${HOST_ID:-$(hostname -s)}"
CAMERA_UIDS="${CAMERA_UIDS:-camera1,camera2,camera3}"
# Optional: pin uids to physical USB ports, e.g. 'camera1:1-1.2,camera2:1-1.3'.
# Empty means uids follow port order, which is stable across reboots but shifts
# if you move a camera to another socket. The agent logs a ready-made line.
CAMERA_PORTS="${CAMERA_PORTS:-}"
MQTT_HOST="${MQTT_HOST:-10.42.0.1}"
MQTT_PORT="${MQTT_PORT:-1883}"
CAMERA_ENV_DIR="${CAMERA_ENV_DIR:-/etc/pi-webrtc/cameras}"

# Extra pi-webrtc flags, e.g. EXTRA_ARGS='--no-adaptive'. Empty by default: a
# flag the hardware does not support makes pi-webrtc exit, and Restart=always
# turns that into a crash loop.
EXTRA_ARGS="${EXTRA_ARGS:-}"

# Starting mode. The agent only uses these if the camera actually supports them;
# otherwise it picks that camera's best available mode.
DEFAULT_WIDTH="${DEFAULT_WIDTH:-1280}"
DEFAULT_HEIGHT="${DEFAULT_HEIGHT:-720}"
DEFAULT_FPS="${DEFAULT_FPS:-30}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run with sudo" >&2
  exit 1
fi

# The upstream install leaves the binary wherever it was extracted, so there is
# no single right place to look. Search the usual spots unless told exactly.
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
    printf '  sudo BINARY=%s ./install.sh\n' "${candidates[@]}" >&2
    exit 1
  else
    echo "error: could not find a pi-webrtc binary." >&2
    echo "Install pi-webrtc first, then re-run with BINARY=/path/to/pi-webrtc" >&2
    echo "To locate it:  find / -name pi-webrtc -type f -perm -u+x 2>/dev/null" >&2
    exit 1
  fi
fi

if [[ ! -x "${BINARY}" ]]; then
  echo "error: no executable pi-webrtc binary at ${BINARY}" >&2
  exit 1
fi

echo "==> Installing dependencies"
# Raspberry Pi OS marks its Python as externally managed (PEP 668), so pip would
# refuse python3-paho-mqtt. v4l-utils provides v4l2-ctl, which the agent uses to
# read each camera's real supported modes.
apt-get update -qq
apt-get install -y python3-paho-mqtt v4l-utils

echo "==> Writing /etc/pi-webrtc/device.env"
mkdir -p /etc/pi-webrtc "${CAMERA_ENV_DIR}"
cat > /etc/pi-webrtc/device.env <<EOF
# Host-wide settings, written by install.sh.
# The agent never rewrites this file; it only touches ${CAMERA_ENV_DIR}/.
HOST_ID=${HOST_ID}
MQTT_HOST=${MQTT_HOST}
MQTT_PORT=${MQTT_PORT}
CAMERA_ENV_DIR=${CAMERA_ENV_DIR}

# Cameras are named from this list, in physical USB port order. More cameras
# than uids means the extras are ignored; fewer means the spare uids go unused.
CAMERA_UIDS=${CAMERA_UIDS}

# Pin uids to USB ports: CAMERA_PORTS=camera1:1-1.2,camera2:1-1.3,camera3:1-1.4
# Worth setting with identical cameras, where a uid landing on the wrong one is
# invisible (same name, same useless serial). Run the agent once and it logs the
# exact line to paste here. Empty = order by port, which is stable across
# reboots but follows the cameras if you move them between sockets.
CAMERA_PORTS=${CAMERA_PORTS}

# Mode to start cameras in, when they support it.
DEFAULT_WIDTH=${DEFAULT_WIDTH}
DEFAULT_HEIGHT=${DEFAULT_HEIGHT}
DEFAULT_FPS=${DEFAULT_FPS}

# How often to publish temperature and 5V rail voltage. Drop to 2 when chasing a
# power fault, so the last reading before a shutdown is close to the event.
TELEMETRY_INTERVAL=${TELEMETRY_INTERVAL:-10}

# Extra pi-webrtc flags, whitespace separated. Edit this line and hit Restart on
# the camera page to try flags without reinstalling.
#   --no-adaptive  stop WebRTC rescaling the picture on its own
#   --hw-accel     hardware encoding. Pi 4 and older only; the Pi 5 has no
#                  hardware H.264 encoder, and this flag will fail there.
EXTRA_ARGS=${EXTRA_ARGS}
EOF
chmod 644 /etc/pi-webrtc/device.env

echo "==> Installing agent + units"
install -m 755 "${here}/pi-webrtc-agent.py" /usr/local/bin/pi-webrtc-agent.py
install -m 644 "${here}/pi-webrtc@.service" /etc/systemd/system/pi-webrtc@.service
install -m 644 "${here}/pi-webrtc-agent.service" /etc/systemd/system/pi-webrtc-agent.service

# The unit ships with a placeholder path; point it at the real binary.
echo "==> Pointing unit at ${BINARY}"
sed -i "s#^ExecStart=[^ ]*pi-webrtc #ExecStart=${BINARY} #" \
  /etc/systemd/system/pi-webrtc@.service
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$(dirname "${BINARY}")#" \
  /etc/systemd/system/pi-webrtc@.service

if ! grep -q "^ExecStart=${BINARY} " /etc/systemd/system/pi-webrtc@.service; then
  echo "error: failed to set ExecStart in the unit file" >&2
  grep '^ExecStart=' /etc/systemd/system/pi-webrtc@.service >&2
  exit 1
fi

# Clean up the old single-camera unit if this Pi was set up before multi-camera.
if [[ -f /etc/systemd/system/pi-webrtc.service ]]; then
  echo "==> Removing the old single-camera pi-webrtc.service"
  systemctl disable --now pi-webrtc.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/pi-webrtc.service /etc/pi-webrtc/stream.env
fi

echo "==> Enabling the agent (it starts the cameras it finds)"
systemctl daemon-reload
# Only the agent is enabled. It enumerates cameras and starts pi-webrtc@<uid>
# for each one, so there is nothing sensible to enable at boot per camera.
systemctl enable pi-webrtc-agent.service
systemctl restart pi-webrtc-agent.service

sleep "${SETTLE:-6}"

echo
echo "Done. host=${HOST_ID}, broker=${MQTT_HOST}:${MQTT_PORT}"
echo "      EXTRA_ARGS=${EXTRA_ARGS:-<none>}"
echo
echo "Cameras the agent found:"
journalctl -u pi-webrtc-agent -n 40 --no-pager -o cat 2>/dev/null \
  | grep -E '^INFO (camera|no v4l2)' | sed 's/^INFO /    /' || echo "    (none yet)"
echo
# The real, variable-expanded command lines. `systemctl show -p ExecStart`
# prints the unexpanded form, which hides whether flags reached the process.
echo "Running commands:"
ps -o args= -C pi-webrtc | sed 's/^/    /' || echo "    (nothing running — check journalctl -u pi-webrtc-agent)"
echo
echo "Logs:   journalctl -u pi-webrtc-agent -u 'pi-webrtc@*' -f"
echo "Verify: mosquitto_sub -h ${MQTT_HOST} -t 'picam/#' -v"
