#!/usr/bin/env bash
#
# List the v4l2 capture cameras on this Pi with the USB port each one is in,
# and print a ready-made CAMERA_PORTS line for /etc/pi-webrtc/device.env.
#
#   ./list-cameras.sh
#
# The port is what pins a uid to a physical socket. It matters because
# /dev/video numbering follows USB probe order and can change between boots —
# and because identical cameras are otherwise indistinguishable (same name, and
# usually the same useless serial).
#
set -uo pipefail

uids="${CAMERA_UIDS:-camera1,camera2,camera3}"
IFS=',' read -ra UID_LIST <<< "${uids}"

declare -A by_port
ports=()

for dev in /dev/video*; do
  [[ -e "${dev}" ]] || continue
  n="${dev#/dev/video}"

  # Only real capture nodes report frame sizes. A USB camera also exposes
  # metadata nodes, which report none — that is how we tell them apart.
  v4l2-ctl --list-formats-ext -d "${dev}" 2>/dev/null | grep -q 'Size:' || continue

  port="$(readlink "/sys/class/video4linux/video${n}/device" 2>/dev/null | sed 's#.*/##; s#:.*##')"
  name="$(cat "/sys/class/video4linux/video${n}/name" 2>/dev/null || echo '?')"
  best="$(v4l2-ctl --list-formats-ext -d "${dev}" 2>/dev/null \
          | grep -oE '[0-9]+x[0-9]+' | sort -t x -k1 -rn | head -1)"

  port="${port:-<not-usb>}"
  printf '  %-13s port=%-8s max=%-10s %s\n' "${dev}" "${port}" "${best:-?}" "${name}"

  if [[ -z "${by_port[${port}]:-}" ]]; then
    by_port["${port}"]="${dev}"
    ports+=("${port}")
  fi
done

if [[ "${#ports[@]}" -eq 0 ]]; then
  echo "  no v4l2 capture cameras found" >&2
  echo >&2
  echo "If a camera is plugged in but missing, it probably failed to power up:" >&2
  echo "the Pi 5 allows 600mA across all USB ports unless it negotiates a 5A" >&2
  echo "supply. Such a camera enumerates (its name is readable) but never" >&2
  echo "configures, so it has no /dev/video node. Check its LED." >&2
  exit 1
fi

# Sort ports numerically (1-1.2 before 1-6), matching the agent's own ordering.
mapfile -t sorted < <(printf '%s\n' "${ports[@]}" \
  | sed 's/-/ /; s/\./ /g' | sort -k1,1n -k2,2n -k3,3n \
  | sed 's/ /-/; s/ /./g')

echo
echo "Paste into /etc/pi-webrtc/device.env, after checking which camera is which:"
line=""
i=0
for port in "${sorted[@]}"; do
  [[ "${i}" -lt "${#UID_LIST[@]}" ]] || break
  [[ -n "${line}" ]] && line+=","
  line+="${UID_LIST[${i}]}:${port}"
  i=$((i + 1))
done
echo "  CAMERA_PORTS=${line}"
echo
echo "That guesses the order. To learn which physical camera is in which port,"
echo "unplug all but one and re-run — the port left standing is that camera's."
echo "Then:  sudo systemctl restart pi-webrtc-agent"
