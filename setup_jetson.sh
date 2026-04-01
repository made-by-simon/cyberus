#!/usr/bin/env bash
# =============================================================================
# Jetson Orin Nano — one-time setup script
# Run this on the Jetson after SSH-ing in over USB.
# =============================================================================
set -euo pipefail

echo "=== Jetson stereo depth server setup ==="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-numpy \
    python3-opencv \
    libopencv-contrib-dev \
    v4l-utils \
    htop

# ---------------------------------------------------------------------------
# 2. Verify CUDA-enabled OpenCV (JetPack ships this)
# ---------------------------------------------------------------------------
echo ""
echo "--- OpenCV build info (checking CUDA) ---"
python3 - <<'EOF'
import cv2, sys
info = cv2.getBuildInformation()
has_cuda = 'CUDA' in info and 'YES' in info[info.find('CUDA'):]
has_xi   = hasattr(cv2, 'ximgproc')
print(f"OpenCV version : {cv2.__version__}")
print(f"CUDA support   : {'YES' if has_cuda else 'NO  (install JetPack OpenCV)'}")
print(f"ximgproc (WLS) : {'YES' if has_xi else 'NO  (install opencv-contrib)'}")
EOF

# ---------------------------------------------------------------------------
# 3. Python deps
# ---------------------------------------------------------------------------
pip3 install --quiet numpy

# ---------------------------------------------------------------------------
# 4. Verify cameras
# ---------------------------------------------------------------------------
echo ""
echo "--- Detected video devices ---"
ls /dev/video* 2>/dev/null || echo "No /dev/video* devices found — plug in your stereo camera"
v4l2-ctl --list-devices 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Set camera permissions (persist across reboots)
# ---------------------------------------------------------------------------
sudo usermod -aG video "$USER" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 6. Install as systemd service (optional — auto-starts on boot)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="/etc/systemd/system/stereo-depth.service"

read -rp "Install as systemd service (auto-start on boot)? [y/N] " yn
if [[ "$yn" =~ ^[Yy]$ ]]; then
    sudo tee "$SERVICE_FILE" > /dev/null <<UNIT
[Unit]
Description=Stereo Depth Estimation Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/jetson/stereo_depth_server.py \
    --mode side_by_side \
    --camera 0 \
    --width 640 \
    --height 480 \
    --fps 15
Restart=always
RestartSec=5
User=${USER}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

    sudo systemctl daemon-reload
    sudo systemctl enable stereo-depth.service
    echo "Service installed. Start with: sudo systemctl start stereo-depth"
    echo "Logs: journalctl -u stereo-depth -f"
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Plug in your stereo camera"
echo "  2. (Optional) Calibrate: python3 jetson/calibrate_stereo.py --mode side_by_side"
echo "  3. Start server:         python3 jetson/stereo_depth_server.py --mode side_by_side"
echo "  4. On your PC:           python pc/depth_viewer.py 192.168.55.1"
