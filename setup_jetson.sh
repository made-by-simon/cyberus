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
    libopencv-dev \
    python3-opencv \
    v4l-utils \
    htop

# ---------------------------------------------------------------------------
# 2. Remove pip OpenCV if installed — system JetPack OpenCV has GStreamer/CUDA
# ---------------------------------------------------------------------------
pip3 uninstall -y opencv-python opencv-python-headless 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Python deps (numpy must be <2 — system OpenCV was built against NumPy 1.x)
# ---------------------------------------------------------------------------
pip3 install --quiet "numpy<2" timm

# ---------------------------------------------------------------------------
# 4. Ensure nvargus-daemon is running (required for CSI cameras)
# ---------------------------------------------------------------------------
sudo systemctl enable --now nvargus-daemon
echo "nvargus-daemon: $(systemctl is-active nvargus-daemon)"

# ---------------------------------------------------------------------------
# 5. Verify OpenCV has GStreamer support
# ---------------------------------------------------------------------------
echo ""
echo "--- OpenCV build info ---"
python3 - <<'EOF'
import cv2
info = cv2.getBuildInformation()
gst = "YES" if "GStreamer:                   YES" in info else "NO"
print(f"OpenCV version : {cv2.__version__}")
print(f"GStreamer      : {gst}")
EOF

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
After=nvargus-daemon.service

[Service]
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/jetson/stereo_depth_server.py \
    --left 0 --right 1 \
    --width 1640 --height 1232 \
    --fps 30
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
echo "  1. Start server: python3 jetson/stereo_depth_server.py --left 0 --right 1"
echo "  2. On your PC:   python pc/depth_viewer.py 192.168.55.1"
