# pi-webrtc on a Raspberry Pi Zero 2 W (libcamera, single camera)

The Zero variant of the camera setup: one CSI camera through libcamera, its uid
given at install. It speaks the same MQTT topics as the multi-camera Pi 5 agent,
so the Flask app and the camera page work against it unchanged — the page is at
`/<uid>` either way.

For the Pi 5 / USB multi-camera version, see [`../pi/`](../pi/README.md).

## What's different from the Pi 5 version

| | Pi 5 (`../pi`) | Zero 2 W (here) |
|---|---|---|
| cameras | many, USB v4l2 | one, CSI libcamera |
| modes | read from each sensor | a curated ladder (libcamera scales) |
| uid | `CAMERA_UIDS` list | one `DEVICE_UID` |
| hardware H.264 encoder | **none** (BCM2712) | **yes** (BCM2710) |
| `--hw-accel` | fails | **default, keep it on** |

The encoder difference is the big one. A Zero 2 W is 4× A53 @ 1GHz — software
H.264 is hopeless — but it *has* the VideoCore hardware encoder the Pi 5 dropped,
so `EXTRA_ARGS=--hw-accel` is the default and does the heavy lifting. If pi-webrtc
crash-loops with it (wrong build, missing `/dev/video11`), clear `EXTRA_ARGS` in
`device.env` and it falls back to software — slow, but it tells you the flag was
the problem.

## Resolutions

libcamera's ISP scales to any size, so there are no discrete v4l2 modes to read.
The agent offers a fixed ladder and validates against it (the ladder is the
allowlist):

```
1920x1080 · 1280x720 · 960x540 · 800x600 · 640x480 · 640x360 · 480x270
```

1080p is on the ladder by request. It will stream, but a Zero 2 W is unlikely to
*hold* it — expect WebRTC to drop resolution under encoder load, the way it did
at 1080p on the Pi 5. 720p30 is the realistic sweet spot. To change the ladder,
edit `MODES` in `pi-webrtc-agent-zero.py`.

## Install

pi-webrtc must already be built for the Zero (with libcamera support) — see the
[upstream docs](https://github.com/TzuHuanTai/RaspberryPi-WebRTC). Then:

```bash
# from the laptop — rm -rf first, scp -r nests into an existing directory
ssh <user>@<zero> 'rm -rf ~/pi-webrtc-zero'
scp -r deploy/pi-zero <user>@<zero>:~/pi-webrtc-zero

# on the Zero
cd ~/pi-webrtc-zero
sudo DEVICE_UID=zero1 ./install.sh
```

`DEVICE_UID` is the camera's name and the URL (`/zero1`). It must be in
`VALID_CAMERAS` in `app.py` (`zero1` and `zero2` are already there), and no other
Pi may use the same one. Override anything:

```bash
sudo DEVICE_UID=zero2 MQTT_HOST=10.42.0.1 DEFAULT_HEIGHT=480 ./install.sh
```

The binary is found automatically; pass `BINARY=/path/to/pi-webrtc` if it lives
somewhere unusual.

### config.txt

libcamera needs the camera enabled in `/boot/firmware/config.txt`. On Bookworm,
`camera_auto_detect=1` handles most official modules; an off-brand sensor may
need an explicit `dtoverlay=` (e.g. `dtoverlay=imx219`). Confirm the OS sees it
before expecting a stream:

```bash
rpicam-hello --list-cameras     # should list your sensor
```

If that lists nothing, the problem is config.txt or the ribbon cable, not this
software — the agent will report the camera as "not detected".

## Checking it

```bash
systemctl status pi-webrtc-agent-zero pi-webrtc-zero
journalctl -u pi-webrtc-agent-zero -u pi-webrtc-zero -f
ps -o args= -C pi-webrtc          # the real, expanded command line

# from the laptop
mosquitto_sub -h 10.42.0.1 -t 'picam/zero1/#' -v
mosquitto_pub -h 10.42.0.1 -t picam/zero1/control/config -m '{"preset":"640x480","fps":30}'
```

The page (`/zero1`) shows the sensor name (e.g. `imx708`), the resolution
dropdown, Restart, CPU temperature, and screenshots — the same controls as the
Pi 5. There is no 5V rail reading: `vcgencmd pmic_read_adc` is Pi 5 only, so that
field is simply absent on a Zero.

## Troubleshooting

Most of [`../pi/README.md`](../pi/README.md) applies — the reconnect-loop and
"connected but no picture" notes especially. Zero-specific:

**Camera "not detected"** — `rpicam-hello --list-cameras` on the Zero. Empty
means config.txt or the ribbon, not this software. "Restart" on the page re-runs
detection once it's fixed.

**Stream crash-loops immediately** — usually `--hw-accel` against a pi-webrtc
build without libcamera/V4L2-M2M encode support. `journalctl -u pi-webrtc-zero -n 30`.
Clear `EXTRA_ARGS` in `device.env` and `systemctl restart pi-webrtc-agent-zero`
to test software encoding.

**Resolution won't hold at 1080p** — expected on this hardware. Drop to 720p30.
