# pi-webrtc cameras + config agent

Runs one pi-webrtc per camera on a Pi, and lets the Flask page at `/<uid>` change
each camera's resolution and fps.

## How it works

`pi-webrtc-agent` is the only thing enabled at boot. On startup it:

1. **enumerates the v4l2 capture devices** and names them from `CAMERA_UIDS` in
   physical USB port order (`camera1`, `camera2`, `camera3`);
2. **reads each camera's real supported modes** out of `v4l2-ctl`;
3. writes `/etc/pi-webrtc/cameras/<uid>.env` and starts `pi-webrtc@<uid>`;
4. publishes modes, current config, and the Pi's temperature to MQTT.

The per-camera units are **not** enabled at boot — the agent starts the ones it
finds. Enabling them directly would try to start cameras that may not be plugged
in.

### Why the modes are read from the hardware

**V4L2 does not fail when you ask for a mode it lacks — it quietly substitutes
the nearest one it has.** Ask a 720p sensor for 1080p and you get 720p, while
every config file and log line still says 1080p. We chased that for an afternoon.
Reading the modes is the only way to stop offering resolutions that silently
don't happen, so the dropdown only ever lists what the sensor really has.

### Which camera is which

**`/dev/video` numbering follows USB probe order and is not guaranteed stable
across reboots.** Cameras are therefore ordered by the physical USB port they
hang off (`1-1.2`, `1-6`, …), read from
`/sys/class/video4linux/video*/device`, so a uid keeps pointing at the same
socket.

This matters most with **identical cameras**, which is the normal case. Three of
the same model share a name, and most report the same serial (`0001`) or none —
so nothing but the port distinguishes them, and a uid landing on the wrong
camera after a reboot would be completely invisible. The page shows each
camera's port next to its name for that reason.

Port *order* survives reboots, but it is still **relative**: if one camera fails
to power up, the others shift up to fill the gap. With identical cameras that is
invisible. Pinning removes the question.

### Pinning, step by step

```bash
# 1. On the Pi, with the cameras plugged in:
./list-cameras.sh
#   /dev/video0   port=1-1     max=1920x1080  Konftel Cam10
#   /dev/video2   port=3-1     max=1920x1080  Konftel Cam10
#   CAMERA_PORTS=camera1:1-1,camera2:3-1

# 2. Work out which physical camera is which. Unplug all but one and re-run —
#    the port left standing belongs to that camera. Repeat. Identical models
#    give you no other way to tell.

# 3. Put the line in /etc/pi-webrtc/device.env, with the uids in the order you
#    want (front camera on camera1, and so on):
sudo nano /etc/pi-webrtc/device.env

# 4. Apply:
sudo systemctl restart pi-webrtc-agent
```

The agent also logs a suggested line at startup when it finds more than one
camera and nothing is pinned — `journalctl -u pi-webrtc-agent | grep 'to pin'`.

A uid pinned to an empty socket is left out with a warning, rather than silently
grabbing whichever camera is around — which is the whole point. Pinned cameras
lose the `?` marker next to the port on the page.

Non-USB cameras (CSI on a Pi) report no port and fall back to `/dev/video`
order.

### Why resolution changes restart the process

pi-webrtc reads width/height/fps **only as startup arguments** — there is no
runtime control for them. So every change restarts that camera's unit and its
stream drops for a few seconds, for every viewer of that camera. Upstream
behaviour, not a bug here.

### File layout

| File | Owner | Holds |
|---|---|---|
| `/etc/pi-webrtc/device.env` | install.sh | host id, broker, uid list, extra flags |
| `/etc/pi-webrtc/cameras/<uid>.env` | the agent | which `/dev/video`, `WIDTH`, `HEIGHT`, `FPS` |

`pi-webrtc@.service` loads both. The agent only ever rewrites the per-camera
file, so a bad control message cannot repoint the Pi at another broker.

## Topics

```
picam/<uid>/control/config       {"preset":"1280x720","fps":30}
picam/<uid>/control/restart      restart just this camera (payload ignored)
picam/host/<host>/control/restart re-enumerate + restart every camera on that Pi
picam/<uid>/state/config         current settings + status        (retained)
picam/<uid>/state/modes          what this camera actually has    (retained)
picam/host/<host>/state          temperature, throttling, cameras (retained)
```

The page's **Restart all cameras** button uses the *host* topic, not the
per-camera one. That is deliberate: it re-runs v4l2 discovery, which is the only
way to pick up a camera that failed to power up at boot — and a camera that never
appeared has no uid to restart individually.

Retained state for a configured uid that is **not** found is cleared on every
agent start and host restart. Without that, a camera that has been unplugged (or
that failed to power up) keeps advertising modes and settings forever, and the
page shows it as real.

Under `picam/` rather than `<uid>/` on purpose: pi-webrtc owns the `<uid>/sdp/…`
and `<uid>/ice/…` tree for signaling, and staying out of it removes any chance of
our messages reaching its signaling parser.

State is retained, so a page loading later sees current settings immediately.
`picam/host/<host>/state` is backed by an MQTT last-will, so an unplugged Pi
shows `online: false` rather than a stale `true`. MQTT allows **one** will per
connection — since the agent is per-host, online/offline is a host property, and
a camera counts as reachable only if its host is.

## Security note

The broker runs `allow_anonymous true`, so **anything on `10.42.0.x` can publish
to the control topics**, and the agent is root. Validation in
`pi-webrtc-agent.py` is therefore the real boundary — it accepts only modes read
from the hardware, and builds the command with `subprocess.run([...])`, never a
shell. Flask validates too, but only for error messages; it is not in the trust
path.

## Install

pi-webrtc itself must already be installed — see the
[upstream docs](https://github.com/TzuHuanTai/RaspberryPi-WebRTC). This installs
the units around it.

```bash
# from the laptop — note scp -r nests if the destination already exists,
# so remove it first or you will silently install a stale copy
ssh meturover@<pi> rm -rf '~/pi-webrtc-deploy'
scp -r deploy/pi meturover@<pi>:~/pi-webrtc-deploy

# on the Pi
cd ~/pi-webrtc-deploy && sudo ./install.sh
```

The pi-webrtc binary is found automatically (`/opt`, `/usr/local/bin`, the usual
spots under `/home`). If several are found it stops and asks rather than guessing
which to run as root.

Defaults: `HOST_ID=$(hostname -s)`, `CAMERA_UIDS=camera1,camera2,camera3`,
`MQTT_HOST=10.42.0.1`, `EXTRA_ARGS=` (empty), starting cameras at 1280x720@30
where supported. Override any:

```bash
sudo HOST_ID=zero2 CAMERA_UIDS=camera4,camera5 EXTRA_ARGS=--no-adaptive ./install.sh
```

Camera uids must appear in `VALID_CAMERAS` in `app.py`, and **two Pis must not
claim the same uid** — they would both answer the same signaling topic.

`install.sh` pulls paho-mqtt and v4l-utils from apt rather than pip, because
Raspberry Pi OS marks its Python as externally managed (PEP 668).

The agent runs on **both paho 1.x and 2.x** and is tested against each. This
matters: apt on Bookworm gives 1.6.1, which has no `CallbackAPIVersion`, while
pip gives 2.x. `make_client()` handles the split — don't "simplify" it to the 2.x
constructor, or the Pi crash-loops while a laptop with pip-installed paho looks
fine.

## Checking it

```bash
systemctl status pi-webrtc-agent 'pi-webrtc@*'
journalctl -u pi-webrtc-agent -u 'pi-webrtc@*' -f
ps -o args= -C pi-webrtc              # the real, expanded command lines

# from the laptop
mosquitto_sub -h 10.42.0.1 -t 'picam/#' -v
mosquitto_pub -h 10.42.0.1 -t picam/camera1/control/config -m '{"preset":"640x480","fps":30}'
```

A rejected message replies on `state/config` with `"status":"error"` naming the
modes that camera does have, and leaves its env file untouched.

## Troubleshooting

**A camera is missing** — `journalctl -u pi-webrtc-agent` lists what it found at
startup, with the port each is on. A USB camera exposes several `/dev/video*`
nodes; the agent keeps only the ones reporting frame sizes, since the extras
carry metadata. More cameras than `CAMERA_UIDS` means the extras are ignored. If
`CAMERA_PORTS` pins a uid to a socket with nothing in it, that camera is skipped
and the reason logged.

**A uid is showing the wrong camera** — pin it. See "Which camera is which".
`udevadm info --query=property --name=/dev/video0 | grep ID_PATH` also spells out
the physical path if you need to trace a socket.

**Settings apply but the stream never returns** — the restart succeeded and
pi-webrtc then failed. `journalctl -u pi-webrtc@camera1 -n 50`. The agent reports
the *restart* working, which it did; it cannot see what happens next.

**Resolution is stuck well below what was asked** — first suspect the *viewer*.
WebRTC's quality estimator starts low and ramps up over several seconds, and
**every new peer connection restarts that ramp from the bottom**. A page that
reconnects in a loop never climbs, and it looks exactly like a Pi too weak to
encode. The tell is in `journalctl -u pi-webrtc@<uid>`: an SDP exchange every
second. **A healthy stream logs one.** Count peer connections in the browser:

```js
window.__made = 0;
const O = window.RTCPeerConnection;
function H(...a){ window.__made++; return new O(...a); }
H.prototype = O.prototype; window.RTCPeerConnection = H;
// wait 20s; __made should stay 0 on a connected stream
```

Diagnoses that looked right and were not: CPU (H264 and VP8 capped identically —
the cheaper codec would have gone higher if the encoder were the limit); the
camera; bandwidth (0 packets lost, 0 frames dropped, 1ms RTT). Clean stats at
full framerate mean nothing downstream is struggling, which points upstream.

**Resolution flips constantly** (measured: 40 changes in 25s, with
`keyFramesDecoded` at 16% of all frames — a healthy stream is well under 1%,
since every change forces a keyframe). That is WebRTC's scaler thrashing, and
`EXTRA_ARGS=--no-adaptive` pins it. But rule out a reconnect loop first, or you
are just masking one.

**Some cameras have no LED and never stream, but the Pi can read their names** —
that is the **USB current limit**, not a software fault. USB devices enumerate at
a fixed 100mA (enough for the Pi to read the name), then request their operating
current. The Pi 5 grants **600mA across all USB ports combined** unless it
negotiates a 5A supply over USB-C Power Delivery. Three cameras at ~250–400mA
each exceed that, so the last one is refused, never configures, and stays dark.
Sometimes the whole port group is cut and none light up.

Measured here: two Konftel Cam10s work; the third does not.

A buck regulator (28V→5V on a robot, say) **cannot do PD negotiation**, so the Pi
assumes 3A and caps USB at 600mA no matter how much current the regulator can
really deliver. That is a *detection* problem, and the fix is:

```ini
# /boot/firmware/config.txt — only with a supply that genuinely delivers 5A.
# Goes under [pi5]: the section filters are sticky, so everything after one
# applies to that model until the next filter. Read at boot only.
[pi5]
usb_max_current_enable=1
```

Also worth knowing on a regulator-fed Pi: the official supply is **5.1V**, not
5.0V, to cover the drop across the cable under load — so trimming a regulator
slightly high is normal practice, not a bodge. The ceiling is **5.25V** (the spec
is 5V ±5%). Measure at the Pi under load, not at the regulator:
`vcgencmd pmic_read_adc | grep EXT5V`.

It removes a protection rather than creating power. With an inadequate supply it
converts a clean shutdown into brownouts, which corrupt SD cards. A powered USB
hub avoids the question entirely and is the better answer for three cameras.

Note the page shows **"connected" with no picture** in this case: pi-webrtc
answers and the WebRTC peer connects even when it never opened the camera. The
page calls that out after 8 seconds of no frames.

**The Pi is hot** — the page shows CPU temperature next to the connection state.
Sustained software encoding will reach the thermal ceiling long before anything
else runs out. Throttling is the Pi protecting itself and does no damage, but a
throttled Pi runs *everything* slower, ROS included. A Pi 5 doing this needs an
active cooler. Measured here: one camera at 1080p60 sat at 85°C and throttled.

Under-voltage (shown in red) is different and worth acting on — that is the power
supply, not the workload.

**The Pi dies and needs a power cycle (red LED)** — that is the PMIC shutting the
board down, almost always power. The page shows the **5V rail as the Pi itself
measures it** (`EXT5V` from `vcgencmd pmic_read_adc`), which is the voltage
arriving *after* the drop across the wiring — the number a multimeter at the
regulator will not show you. Flask keeps the last readings when a Pi vanishes
(the agent's last will is merged over them rather than replacing them), so after
a shutdown the strip still shows the temperature and voltage from just before it
went, marked "last seen".

Set `TELEMETRY_INTERVAL=2` in `device.env` while chasing one, so that last
reading is close to the event.

**The voltage trend tells you which fault it is**, and they need opposite fixes:

| What you see | Cause | Fix |
|---|---|---|
| Rail sags 5.1 → 4.9 → 4.7, then dies | current limit or regulator capacity | raise the CC pot / a bigger regulator |
| Rail normal, then gone instantly | the regulator's own thermal shutdown | heatsink it, or use a better one |
| Rail fine throughout | not the rail — look elsewhere | — |

On a step-down module (28V→5V on a robot), note the ratio is what hurts: the
common "5A" CC/CV boards realistically manage 2–3A continuous without a heatsink,
and a Pi 5 plus cameras wants more than that. A converter that works for minutes
and then cuts out is the classic signature of one cooking itself, and **raising
the current limit makes that worse, not better**.

**`--hw-accel`** is Pi 4 and older only. The Pi 5's BCM2712 has **no hardware
H.264 encoder**, so encoding there is always software and the flag will fail.
