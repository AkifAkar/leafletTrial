# leafletTrial

Flask app serving Leaflet maps fed by ROS 2, plus Raspberry Pi camera streams
over WebRTC.

| Route | What it is |
|---|---|
| `/map` | Leaflet map, GPS marker driven by the ROS `/fix` topic |
| `/zed` | ZED camera, JPEG frames over rosbridge |
| `/<uid>` | Pi WebRTC stream with resolution/fps controls, e.g. `/camera1` |

Each Pi runs one pi-webrtc per camera. Its agent enumerates the v4l2 devices at
startup and names them `camera1`, `camera2`, `camera3` in `/dev/video` order, so
three cameras on one Pi are `/camera1`, `/camera2`, `/camera3`. Resolution
options come from the camera itself, not a hardcoded list.

## Running it

```bash
cd ~/Desktop/leafletTrial
python3 app.py
```

Then http://localhost:8000/zero1

Needs `pip install -r requirements.txt` once. The map pages additionally need
rosbridge on `:9090`; the camera pages need Mosquitto and the Pi (below).

The uid in the URL must appear in `VALID_CAMERAS` in `app.py`, or you get a 404.

`MQTT_HOST` defaults to `10.42.0.1`, this laptop's address on the link the Pis
sit behind. Override it when that interface is down:

```bash
MQTT_HOST=127.0.0.1 python3 app.py
```

## Screenshots

The 📷 button on a camera page (or the **S** key) saves the current frame to
`~/Pictures/Screenshots`, named `<uid>_<YYYYMMDD>-<HHMMSS>.png` — sortable, and
grouped by camera. Two shots in the same second get `_2`, `_3` rather than
overwriting.

```bash
SCREENSHOT_DIR=~/rover/captures python3 app.py     # somewhere else
```

The browser captures the frame and Flask writes it, so the file lands in a folder
you choose rather than wherever the browser dumps downloads. Filenames are built
server-side from the validated uid and the clock, never from anything the page
sends. If the picture is rotated, the rotation is baked into the saved image.

## Working offline

The **camera pages need no internet.** picamera.js is vendored under
`static/picamera/`, and no STUN server is configured — the browser and Pi are on
the same subnet, so WebRTC connects them with host candidates directly. Earlier
the page pulled picamera.js from a CDN and used Google's STUN server, so the feed
died whenever the laptop lost its wifi uplink; that is fixed.

If you ever put a viewer on a different network from the Pi, add a STUN (or TURN)
server per-page with `?stun=stun:host:port` — it is off by default.

### Use Chrome for offline operation

**Firefox does not work with the laptop offline; Chrome does.** This is a browser
difference, not a bug in the app — use Chrome/Chromium when running without an
internet uplink. Verified on this setup.

What Firefox does offline, for the record: it refuses to create the peer
connection at all (`DOMException: Can't create RTCPeerConnections when the
network is down`) while MQTT keeps connecting happily, and even once that is
worked around it emits mDNS-obfuscated ICE candidates (`<uuid>.local` instead of
`10.42.0.1`) that the Pi cannot resolve while the browser considers itself
offline. Chrome does neither.

If you must use Firefox offline, `about:config`:

| pref | value |
|---|---|
| `network.manage-offline-status` | false |
| `media.peerconnection.ice.obfuscate_host_addresses` | false |

and optionally stop NetworkManager reporting "no internet" on the isolated link
(browser-agnostic, and reasonable on a field robot that has no uplink to check):

```bash
sudo tee /etc/NetworkManager/conf.d/20-connectivity-off.conf >/dev/null <<'EOF'
[connectivity]
interval=0
EOF
sudo systemctl reload NetworkManager
```

The page detects the browser's offline state either way, explains it instead of
looping on an exception, and reconnects by itself once connectivity returns.

The **map pages (`/map`, and the unused `responsive`/`quickStart` templates) do
still need internet**, because they fetch OpenStreetMap tiles and Leaflet's CSS
from a CDN. `/zed` and the camera pages are fully self-contained.

## The pieces

```
Browser ──── ws:1884 ──┐
                       ├── Mosquitto (laptop, 10.42.0.1)
Pi ───────── tcp:1883 ─┘      1883 for the Pis, 1884 websockets for browsers

/<uid>  ── POST /api/camera/<uid>/config ──► picam/<uid>/control/config
                                                     │
                                            pi-webrtc-agent (one per Pi)
                                                     │ restarts pi-webrtc@<uid>
                                                     ▼
        ◄──────────── retained ──────── picam/<uid>/state/config   settings
                                        picam/<uid>/state/modes    what v4l2 has
                                        picam/host/<host>/state    temp, throttle
```

Video itself is peer-to-peer and never touches the broker — MQTT only carries
the WebRTC handshake.

## The Pi

Two deployments, same MQTT topics and same camera page:

- [deploy/pi/](deploy/pi/README.md) — Pi 5 with USB v4l2 cameras, many per Pi,
  modes read from each sensor.
- [deploy/pi-zero/](deploy/pi-zero/README.md) — Pi Zero 2 W with one libcamera
  CSI camera, uid at install, hardware H.264 encoding.

Both auto-start on boot; normally there is nothing to do.

```bash
systemctl status pi-webrtc pi-webrtc-agent
journalctl -u pi-webrtc -u pi-webrtc-agent -f
```

Changing resolution restarts pi-webrtc, because it only reads width/height/fps
as startup arguments. The stream drops for a few seconds on every change, for
every viewer of that camera. That is upstream behaviour, not a bug here.

## If the picture is worse than you asked for

The page shows the real track size next to your request. If it sits well below,
read `deploy/pi/README.md` — the usual cause is a reconnect loop in the viewer
resetting WebRTC's quality ramp, not the Pi. `journalctl -u pi-webrtc` should
show **one** SDP exchange per viewer; one per second means a loop.
