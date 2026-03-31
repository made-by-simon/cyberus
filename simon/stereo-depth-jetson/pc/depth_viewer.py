#!/usr/bin/env python3
"""
Real-time depth map viewer — runs on your PC.
Connects to the Jetson depth server and displays the live stream.

Usage:
    python depth_viewer.py 192.168.55.1            # USB-C ethernet (Jetson USB gadget IP)
    python depth_viewer.py 192.168.1.42            # WiFi IP
    python depth_viewer.py 192.168.55.1 --port 9999 --record output.avi

Keys:
    Q / ESC  — quit
    S        — save current frame as PNG
    R        — start / stop recording
    F        — toggle fullscreen
"""

import cv2
import numpy as np
import socket
import struct
import time
import argparse
import sys
from pathlib import Path
from datetime import datetime


# ---------------------------------------------------------------------------
# Socket helpers
# ---------------------------------------------------------------------------
def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray(n)
    view = memoryview(buf)
    pos = 0
    while pos < n:
        chunk = sock.recv_into(view[pos:], n - pos)
        if not chunk:
            raise ConnectionError("Server closed connection")
        pos += chunk
    return bytes(buf)


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------
class DepthViewer:
    RECONNECT_DELAY = 3.0

    def __init__(self, host: str, port: int, record_path: str | None,
                 fullscreen: bool, max_window_w: int):
        self.host        = host
        self.port        = port
        self.record_path = record_path
        self.fullscreen  = fullscreen
        self.max_w       = max_window_w

        self.sock     : socket.socket | None = None
        self.writer   : cv2.VideoWriter | None = None
        self.recording = False

    # ------------------------------------------------------------------
    def _connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((self.host, self.port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(5.0)
            self.sock = s
            print(f"[+] Connected to {self.host}:{self.port}")
            return True
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            print(f"[-] Cannot connect: {e} — retrying in {self.RECONNECT_DELAY}s")
            return False

    # ------------------------------------------------------------------
    def _recv_frame(self) -> np.ndarray | None:
        try:
            size_b = recv_exact(self.sock, 4)
            size   = struct.unpack('>I', size_b)[0]
            if size == 0 or size > 20 * 1024 * 1024:
                raise ValueError(f"Suspicious frame size: {size}")
            raw  = recv_exact(self.sock, size)
            arr  = np.frombuffer(raw, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            return frame
        except (ConnectionError, OSError, ValueError) as e:
            print(f"[-] Receive error: {e}")
            return None

    # ------------------------------------------------------------------
    def _start_recording(self, frame: np.ndarray):
        h, w = frame.shape[:2]
        path  = self.record_path or f"depth_{datetime.now():%Y%m%d_%H%M%S}.avi"
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        self.writer    = cv2.VideoWriter(path, fourcc, 15.0, (w, h))
        self.recording = True
        print(f"[R] Recording → {path}")

    def _stop_recording(self):
        if self.writer:
            self.writer.release()
            self.writer = None
        self.recording = False
        print("[R] Recording stopped")

    # ------------------------------------------------------------------
    def run(self):
        win = 'Stereo Depth Stream'
        cv2.namedWindow(win, cv2.WINDOW_NORMAL)
        if self.fullscreen:
            cv2.setWindowProperty(win, cv2.WND_PROP_FULLSCREEN,
                                  cv2.WINDOW_FULLSCREEN)

        frame_count  = 0
        fps_timer    = time.time()
        display_fps  = 0.0
        frame_times  = []

        print("Keys: Q/ESC=quit  S=save  R=record  F=fullscreen")

        while True:
            # --- ensure connection ---
            if self.sock is None:
                if not self._connect():
                    time.sleep(self.RECONNECT_DELAY)
                    continue

            # --- receive frame ---
            t0    = time.time()
            frame = self._recv_frame()
            if frame is None:
                if self.sock:
                    self.sock.close()
                    self.sock = None
                time.sleep(self.RECONNECT_DELAY)
                continue

            # --- resize to fit screen if needed ---
            if frame.shape[1] > self.max_w:
                scale = self.max_w / frame.shape[1]
                frame = cv2.resize(frame, None, fx=scale, fy=scale)

            # --- FPS ---
            frame_count += 1
            frame_times.append(time.time() - t0)
            if len(frame_times) > 30:
                frame_times.pop(0)
            elapsed = time.time() - fps_timer
            if elapsed >= 1.0:
                display_fps = frame_count / elapsed
                frame_count = 0
                fps_timer   = time.time()

            # --- overlay ---
            h, w = frame.shape[:2]
            mid   = w // 2
            # divider line
            cv2.line(frame, (mid, 0), (mid, h), (80, 80, 80), 1)
            # labels
            cv2.putText(frame, 'Left camera', (8, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)
            cv2.putText(frame, 'Depth map', (mid + 8, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2, cv2.LINE_AA)
            # FPS + latency
            latency_ms = np.mean(frame_times) * 1000
            cv2.putText(frame, f'FPS: {display_fps:.1f}  lat: {latency_ms:.0f}ms',
                        (8, h - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 1, cv2.LINE_AA)
            # Recording indicator
            if self.recording:
                cv2.circle(frame, (w - 20, 20), 8, (0, 0, 255), -1)
                cv2.putText(frame, 'REC', (w - 60, 28),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

            # --- record ---
            if self.recording and self.writer:
                self.writer.write(frame)

            # --- display ---
            cv2.imshow(win, frame)
            key = cv2.waitKey(1) & 0xFF

            if key in (ord('q'), 27):   # Q or ESC
                break
            elif key == ord('s'):
                fname = f"depth_{datetime.now():%Y%m%d_%H%M%S}.png"
                cv2.imwrite(fname, frame)
                print(f"[S] Saved {fname}")
            elif key == ord('r'):
                if self.recording:
                    self._stop_recording()
                else:
                    self._start_recording(frame)
            elif key == ord('f'):
                prop = cv2.getWindowProperty(win, cv2.WND_PROP_FULLSCREEN)
                cv2.setWindowProperty(
                    win, cv2.WND_PROP_FULLSCREEN,
                    cv2.WINDOW_FULLSCREEN if prop != cv2.WINDOW_FULLSCREEN else cv2.WINDOW_NORMAL
                )

        if self.recording:
            self._stop_recording()
        if self.sock:
            self.sock.close()
        cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description='Real-time stereo depth viewer')
    p.add_argument('host', help='Jetson IP (e.g. 192.168.55.1 for USB, or WiFi IP)')
    p.add_argument('--port',       type=int, default=9999)
    p.add_argument('--record',     default=None, metavar='FILE.avi',
                   help='Start recording immediately to this file')
    p.add_argument('--fullscreen', action='store_true')
    p.add_argument('--max-width',  type=int, default=1920,
                   help='Maximum display width (px) — frames wider than this are scaled down')
    args = p.parse_args()

    viewer = DepthViewer(
        host        = args.host,
        port        = args.port,
        record_path = args.record,
        fullscreen  = args.fullscreen,
        max_window_w= args.max_width,
    )
    viewer.run()


if __name__ == '__main__':
    main()
