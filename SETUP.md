# Stereo Depth Jetson Orin Nano — Setup Guide

## Architecture

```
Dual IMX219 CSI Cameras (sensor-id 0 + 1, 60 mm baseline)
     │ (CSI)
     ▼
Jetson Orin Nano
  ├─ jetson/stereo_depth_server.py
  │    ├─ Reads stereo frames
  │    ├─ Rectifies (if calibrated)
  │    ├─ SGBM disparity → metric depth
  │    ├─ Colourises (Turbo colourmap)
  │    └─ Streams JPEG over TCP :9999
     │
     │ USB-C (USB ethernet gadget)
     │ 192.168.55.1 (Jetson)
     ▼
Your PC
  └─ pc/depth_viewer.py
       ├─ Receives TCP stream
       └─ Displays left camera | depth map
```

---

## Step 0 — Connect to the Jetson over USB-C

The Jetson runs a USB ethernet gadget that gives it a static IP **192.168.55.1**.
Your PC gets **192.168.55.100**.

Confirm connectivity:
```powershell
ping 192.168.55.1
```

### Windows USB routing (one-time setup, run PowerShell as admin)

Windows may route `192.168.55.x` traffic via the wrong interface. Fix it:

```powershell
# Find the UsbNcm adapter index (typically 4)
Get-NetAdapter | Where-Object {$_.InterfaceDescription -like "*UsbNcm*"}

# Disable DHCP and set a static IP (replace 4 with your index)
Set-NetIPInterface -InterfaceIndex 4 -Dhcp Disabled
Remove-NetIPAddress -InterfaceIndex 4 -Confirm:$false
New-NetIPAddress -InterfaceIndex 4 -IPAddress 192.168.55.100 -PrefixLength 24

# Add a persistent route via the USB adapter
route -p add 192.168.55.0 mask 255.255.255.0 0.0.0.0 IF 4
```

SSH in:
```powershell
ssh cyberus@192.168.55.1
```

If the USB network isn't appearing, check the Jetson's USB device mode service:
```bash
sudo systemctl status nv-l4t-usb-device-mode.service
sudo systemctl enable --now nv-l4t-usb-device-mode.service
```

---

## Step 1 — Clone the repo on the Jetson

```bash
ssh cyberus@192.168.55.1
git clone https://github.com/made-by-simon/cyberus.git ~/simon
```

To update later:
```bash
cd ~/simon && git pull
```

---

## Step 2 — Run setup on Jetson

```bash
cd ~/simon
bash setup_jetson.sh
```

This installs dependencies and (optionally) creates a systemd service.

---

## Step 3 — Identify your cameras (IMX219 CSI)

CSI cameras on the Jetson do **not** appear as standard `/dev/video*` V4L2 devices.
They are accessed through the Argus/GStreamer stack (`nvarguscamerasrc`).

```bash
# Confirm both sensors are detected by the Argus daemon
ls /dev/video*                 # may list nothing or unrelated V4L2 devices
v4l2-ctl --list-devices        # informational only — Argus cameras won't show here

# Test left camera (sensor-id 0) — should open a live window
gst-launch-1.0 nvarguscamerasrc sensor-id=0 ! \
    'video/x-raw(memory:NVMM),width=1640,height=1232,framerate=30/1' ! \
    nvvidconv ! xvimagesink

# Test right camera (sensor-id 1)
gst-launch-1.0 nvarguscamerasrc sensor-id=1 ! \
    'video/x-raw(memory:NVMM),width=1640,height=1232,framerate=30/1' ! \
    nvvidconv ! xvimagesink
```

If `nvarguscamerasrc` is missing, install the required JetPack multimedia libraries:
```bash
sudo apt install nvidia-l4t-multimedia gstreamer1.0-plugins-bad
```

### Camera mode selection

| Camera type | `--mode` flag | Notes |
|---|---|---|
| **Dual IMX219 CSI (this setup)** | **`separate`** | Two independent sensor-id 0 / 1 |
| Side-by-side stereo (one USB device, L+R in one wide frame) | `side_by_side` | Common USB stereo cameras |
| Top-bottom (L top, R bottom) | `top_bottom` | Some industrial cameras |
| Intel RealSense | See note below | Use librealsense SDK |
| ZED / ZED2 | See note below | Use ZED SDK |

**RealSense:** Install `pyrealsense2` from NVIDIA's JetPack apt repo. Replace camera capture code with `rs.pipeline()`.

**ZED:** Install the ZED SDK for JetPack. Replace capture with `sl.Camera()`.

---

## Step 4 — (Recommended) Calibrate your stereo camera

### Calibration board

Print a chessboard calibration pattern:
- **9×6 inner corners** (10×7 squares), **30 mm per square** — this size fills the
  frame well at 0.4–1.2 m distance given the 73°(H) field of view
- Generator: https://calib.io/pages/camera-calibration-tools (or OpenCV sample)
- Print on matte paper (not glossy) and mount flat on a rigid board

> **Why 30 mm?** With the IMX219's 2.6 mm focal length and 73° horizontal FOV, a
> 270 mm wide board (9 squares × 30 mm) subtends ~30° at 0.5 m — large enough for
> accurate corner detection without going out of frame.

### Calibration resolution

Calibrate at **half-native resolution (1640×1232)** for a good accuracy/speed
balance. Full native (3280×2464) gives marginally better accuracy but is slow to
process; 640×480 loses sub-pixel precision.

### Run calibration on the Jetson

```bash
cd ~/simon
python3 jetson/calibrate_stereo.py \
    --mode separate \
    --camera-left 0 --camera-right 1 \
    --width 1640 --height 1232 \
    --rows 9 --cols 6 \
    --square 30.0 \
    --output stereo_calib.npz
```

### Capture tips for these cameras

- Press **SPACE** to grab a calibration pair (board fully visible in **both** views)
- Collect **20–30 pairs** — more than usual because the wide FOV means the board
  can occupy many distinct positions
- Cover the **full FOV**: tilt ±30°, push to corners, vary distance from **0.4 m
  to 1.5 m** (the 60 mm baseline gives best depth in this range)
- Include a few frames where the board fills most of the frame — important for
  accurate distortion estimation at the edges
- Press **ESC** to compute calibration

### Quality targets

| Metric | Target | Notes |
|---|---|---|
| RMS reprojection error | **< 0.3 px** | IMX219 lenses are consistent; > 0.5 px suggests a bad pair — remove outliers |
| Baseline recovered | ~60 mm | Check `T` vector in `stereo_calib.npz`; should match the physical mount |
| Epipolar alignment | horizontal lines | Check `rectification_preview.png` — misaligned lines mean bad samples |

A rectification preview image is saved as `rectification_preview.png` — epipolar
lines should be perfectly horizontal after good calibration.

---

## Step 5 — Start the depth server on Jetson

**IMX219 supported resolutions (use one of these):**

| Resolution | FPS | Use case |
|---|---|---|
| 3280×2464 | 21 | Maximum accuracy (slow SGBM) |
| **1640×1232** | **30** | **Recommended — best balance** |
| 820×616 | 60 | Fast/embedded |
| 640×480 | 60 | Cropped, lowest latency |

```bash
cd ~/simon
python3 jetson/stereo_depth_server.py --left 0 --right 1 --width 1640 --height 1232 --fps 30
```

You should see log output like:
```
[INFO] Listening on 0.0.0.0:9999
[INFO] Depth estimation loop running...
[INFO] FPS: 12.3  frame: 28 KB
```

---

## Step 6 — View on your PC

Clone the repo on your PC if you haven't already:
```powershell
git clone https://github.com/made-by-simon/cyberus.git
cd cyberus
```

Install PC dependencies (one time):
```powershell
pip install -r requirements_pc.txt
```

Run the viewer:
```powershell
python pc/depth_viewer.py 192.168.55.1
```

**Controls:**
| Key | Action |
|-----|--------|
| `Q` / `ESC` | Quit |
| `S` | Save current frame as PNG |
| `R` | Toggle recording to AVI |
| `F` | Toggle fullscreen |

---

## Tuning depth quality

Edit these flags in `jetson/stereo_depth_server.py` or pass as CLI args:

| Parameter | Effect |
|---|---|
| `--num-disparities 192` | Start here for 1640 px width / 60 mm baseline. Try 128/192/256. Must be multiple of 16. |
| `--block-size 7` | Larger = smoother but less detail. Try 5, 7, 9, 11. |
| `--no-wls` | Disable WLS filter for ~30% speed boost (noisier result) |
| `--max-depth 5.0` | 60 mm baseline → reliable range is 0.3–5 m; raise for large scenes |
| `--quality 65` | Lower JPEG quality = less bandwidth, more artefacts |

**Choosing `--num-disparities` for the IMX219 at 1640 px width:**
Minimum depth ≈ `baseline_mm × focal_px / max_disparity_px`. With f ≈ 1640/2×tan(36.5°) ≈ 1107 px:

| `--num-disparities` | Min depth | Max depth (practical) |
|---|---|---|
| 128 | ~0.52 m | ~5 m |
| **192** | **~0.35 m** | **~5 m** |
| 256 | ~0.26 m | ~5 m |

---

## Performance expectations (Jetson Orin Nano, 640×480)

| Mode | FPS | Quality |
|---|---|---|
| CPU SGBM + WLS filter | 8–12 | Best |
| CPU SGBM, no WLS | 12–18 | Good |
| CUDA StereoBM | 15–25 | Moderate (less accurate) |

For higher FPS at good quality, reduce resolution: `--width 320 --height 240`

---

## Bandwidth

| Config | Approx. bandwidth |
|---|---|
| 640×480 @ 15 FPS, quality 75 | ~3–5 Mbps |
| 320×240 @ 25 FPS, quality 75 | ~1–2 Mbps |

USB-C ethernet gadget supports ~40–100 Mbps — well above these rates.

---

## Troubleshooting

**"Cannot open camera"**
- Check `ls /dev/video*` and use correct `--camera` index
- Ensure you're in the `video` group: `sudo usermod -aG video $USER` then re-login

**Low FPS / high CPU**
- Add `--no-wls` or `--cuda` or reduce `--num-disparities`
- Reduce resolution: `--width 320 --height 240`

**Depth looks like noise**
- Camera not calibrated — run `jetson/calibrate_stereo.py` first
- Untextured surfaces (plain walls) are inherently hard for stereo — add texture or lighting

**Depth range wrong**
- Adjust `--max-depth` to match your scene scale

**Connection refused on PC**
- Confirm Jetson is running the server: `ps aux | grep stereo`
- Confirm firewall allows port 9999: `sudo ufw allow 9999/tcp`
- Confirm you can ping the Jetson: `ping 192.168.55.1`

**USB network not appearing on PC (Windows)**
- See Windows USB routing setup in Step 0
