#!/usr/bin/env python3
"""
Stereo camera + MiDaS depth estimation server for Jetson Orin Nano.
Streams left camera | right camera | MiDaS depth map over TCP.

Usage:
    python stereo_depth_server.py --left 0 --right 1
    python stereo_depth_server.py --left 0 --right 1 --width 640 --height 480 --fps 15
"""

import cv2
import numpy as np
import socket
import struct
import threading
import time
import argparse
import logging

import torch
import torch.nn.functional as F

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

WIDTH        = 640
HEIGHT       = 480
FPS          = 15
PORT         = 9999
JPEG_QUALITY = 75


class DepthModel:
    def __init__(self):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        log.info(f"Loading MiDaS_small on {self.device}…")
        self.model = torch.hub.load("intel-isl/MiDaS", "MiDaS_small")
        self.model.to(self.device).eval()
        self.transform = torch.hub.load("intel-isl/MiDaS", "transforms").small_transform
        log.info("MiDaS ready")

    @torch.no_grad()
    def depth_colormap(self, bgr: np.ndarray) -> np.ndarray:
        rgb   = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        batch = self.transform(rgb).to(self.device)
        pred  = self.model(batch)
        pred  = F.interpolate(
            pred.unsqueeze(1),
            size=bgr.shape[:2],
            mode="bicubic",
            align_corners=False,
        ).squeeze().cpu().numpy()

        d_min, d_max = pred.min(), pred.max()
        if d_max > d_min:
            pred = (pred - d_min) / (d_max - d_min)
        return cv2.applyColorMap((pred * 255).astype(np.uint8), cv2.COLORMAP_TURBO)


class StereoDepthServer:
    def __init__(self, args):
        self.args    = args
        self._lock   = threading.Lock()
        self._frame  : bytes | None = None
        self.running = False

    def _open_cameras(self):
        def gst_pipeline(sensor_id: int) -> str:
            return (
                f"nvarguscamerasrc sensor-id={sensor_id} ! "
                f"video/x-raw(memory:NVMM),width={self.args.width},height={self.args.height},"
                f"framerate={self.args.fps}/1 ! "
                f"nvvidconv ! video/x-raw,format=BGRx ! "
                f"videoconvert ! video/x-raw,format=BGR ! appsink drop=1"
            )

        def open_cam(sensor_id: int) -> cv2.VideoCapture:
            cap = cv2.VideoCapture(gst_pipeline(sensor_id), cv2.CAP_GSTREAMER)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open CSI camera sensor-id={sensor_id}")
            return cap

        self.left_cap  = open_cam(self.args.left)
        self.right_cap = open_cam(self.args.right)
        log.info(f"Cameras opened: sensor-id={self.args.left}, sensor-id={self.args.right}")

    def _accept_loop(self, srv: socket.socket):
        srv.listen(8)
        srv.settimeout(1.0)
        log.info(f"Listening on {self.args.host}:{self.args.port}")
        while self.running:
            try:
                conn, addr = srv.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                pass

    def _client_loop(self, conn: socket.socket, addr):
        log.info(f"Client connected: {addr}")
        try:
            while self.running:
                with self._lock:
                    data = self._frame
                if data is None:
                    time.sleep(0.005)
                    continue
                try:
                    conn.sendall(struct.pack('>I', len(data)) + data)
                except OSError:
                    break
                time.sleep(1.0 / self.args.fps)
        finally:
            log.info(f"Client disconnected: {addr}")
            conn.close()

    def run(self):
        self._open_cameras()
        depth_model   = DepthModel()
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.args.quality]

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.host, self.args.port))

        self.running = True
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

        log.info("Running. Press Ctrl-C to stop.")
        try:
            while True:
                t0 = time.time()

                ret_l, left  = self.left_cap.read()
                ret_r, right = self.right_cap.read()
                if not ret_l or not ret_r:
                    log.warning("Frame capture failed — retrying")
                    time.sleep(0.1)
                    continue

                depth_vis = depth_model.depth_colormap(left)
                combined  = np.hstack([left, right, depth_vis])

                ok, buf = cv2.imencode('.jpg', combined, encode_params)
                if ok:
                    with self._lock:
                        self._frame = buf.tobytes()

                wait = (1.0 / self.args.fps) - (time.time() - t0)
                if wait > 0:
                    time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Stopping…")
        finally:
            self.running = False
            self.left_cap.release()
            self.right_cap.release()
            srv.close()


def main():
    p = argparse.ArgumentParser(description='Stereo + MiDaS depth server for Jetson Orin Nano')
    p.add_argument('--host',    default='0.0.0.0')
    p.add_argument('--port',    type=int, default=PORT)
    p.add_argument('--left',    type=int, default=0,            help='/dev/videoN for left camera')
    p.add_argument('--right',   type=int, default=1,            help='/dev/videoN for right camera')
    p.add_argument('--width',   type=int, default=WIDTH)
    p.add_argument('--height',  type=int, default=HEIGHT)
    p.add_argument('--fps',     type=int, default=FPS)
    p.add_argument('--quality', type=int, default=JPEG_QUALITY, help='JPEG quality 1–100')
    StereoDepthServer(p.parse_args()).run()


if __name__ == '__main__':
    main()
