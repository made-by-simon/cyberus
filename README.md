# cyberus

Stereo depth estimation on a Jetson Orin Nano using dual IMX219 CSI cameras.
Streams a colourised depth map over USB-C to a PC viewer in real time.

## Repo structure

```
cyberus/
├── jetson/
│   └── stereo_depth_server.py   # Runs on Jetson — captures, processes, streams
├── pc/
│   └── depth_viewer.py          # Runs on PC — receives and displays the stream
├── requirements_jetson.txt
├── requirements_pc.txt
├── setup_jetson.sh              # One-time Jetson dependency install
└── SETUP.md                     # Full setup guide
```

## Quick start

1. Connect the Jetson via USB-C and SSH in — see [SETUP.md](SETUP.md) for Windows routing setup
2. Clone the repo on the Jetson:
   ```bash
   git clone https://github.com/made-by-simon/cyberus.git ~/simon
   cd ~/simon && bash setup_jetson.sh
   ```
3. Start the depth server on the Jetson:
   ```bash
   python3 jetson/stereo_depth_server.py --left 0 --right 1 --width 1640 --height 1232 --fps 30
   ```
4. Run the viewer on your PC:
   ```powershell
   pip install -r requirements_pc.txt
   python pc/depth_viewer.py 192.168.55.1
   ```

See [SETUP.md](SETUP.md) for calibration, tuning, and troubleshooting.
