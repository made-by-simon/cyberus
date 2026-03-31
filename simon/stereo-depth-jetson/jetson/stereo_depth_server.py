#!/usr/bin/env python3
"""
Stereo camera depth estimation server for Jetson Orin Nano.
Captures stereo frames, computes depth map using SGBM (CPU or CUDA),
and streams a side-by-side view (left camera | depth colormap) over TCP.

Usage:
    python stereo_depth_server.py --mode side_by_side --camera 0 --port 9999
    python stereo_depth_server.py --mode side_by_side --calibration stereo_calib.npz --cuda
"""

import cv2
import numpy as np
import socket
import struct
import threading
import time
import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check optional modules
# ---------------------------------------------------------------------------
try:
    import cv2.ximgproc
    HAS_XIMGPROC = True
except AttributeError:
    HAS_XIMGPROC = False
    log.warning("cv2.ximgproc not found (opencv-contrib missing) — WLS filter disabled")

try:
    _ = cv2.cuda.getCudaEnabledDeviceCount()
    HAS_CUDA = cv2.cuda.getCudaEnabledDeviceCount() > 0
except cv2.error:
    HAS_CUDA = False


# ---------------------------------------------------------------------------
# Stereo depth estimator
# ---------------------------------------------------------------------------
class StereoDepthEstimator:
    def __init__(self, calibration_file: str | None, use_wls: bool, use_cuda: bool,
                 num_disparities: int = 128, block_size: int = 7):
        self.Q = None
        self.map1_l = self.map2_l = self.map1_r = self.map2_r = None
        self.use_cuda = use_cuda and HAS_CUDA
        self.use_wls = use_wls and HAS_XIMGPROC and not self.use_cuda
        self.focal_length = None
        self.baseline = None

        if calibration_file:
            self._load_calibration(calibration_file)

        self._build_matchers(num_disparities, block_size)

        if self.use_cuda:
            log.info("Using CUDA-accelerated StereoBM")
        else:
            log.info(f"Using CPU SGBM {'+ WLS filter' if self.use_wls else ''}")

    # ------------------------------------------------------------------
    def _load_calibration(self, path: str):
        data = np.load(path)
        K_l = data['K_left']
        D_l = data['D_left']
        K_r = data['K_right']
        D_r = data['D_right']
        R   = data['R']
        T   = data['T']
        h, w = int(data['image_size'][0]), int(data['image_size'][1])

        R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
            K_l, D_l, K_r, D_r, (w, h), R, T, alpha=0
        )
        self.Q = Q
        # Extract baseline and focal length from Q for fallback depth calc
        self.focal_length = Q[2, 3]          # f
        self.baseline     = 1.0 / Q[3, 2]   # baseline = 1 / Q[3,2]

        self.map1_l, self.map2_l = cv2.initUndistortRectifyMap(K_l, D_l, R1, P1, (w, h), cv2.CV_32FC1)
        self.map1_r, self.map2_r = cv2.initUndistortRectifyMap(K_r, D_r, R2, P2, (w, h), cv2.CV_32FC1)
        log.info(f"Calibration loaded — baseline: {abs(self.baseline)*1000:.1f} mm, "
                 f"focal: {self.focal_length:.1f} px, image: {w}x{h}")

    # ------------------------------------------------------------------
    def _build_matchers(self, num_disp: int, block: int):
        # Ensure num_disp is divisible by 16
        num_disp = max(16, (num_disp // 16) * 16)

        if self.use_cuda:
            self._stereo_cuda = cv2.cuda.createStereoBM(numDisparities=num_disp, blockSize=block)
            return

        P1 = 8  * 1 * block ** 2   # grayscale → channels=1
        P2 = 32 * 1 * block ** 2
        self._stereo = cv2.StereoSGBM_create(
            minDisparity=0,
            numDisparities=num_disp,
            blockSize=block,
            P1=P1, P2=P2,
            disp12MaxDiff=1,
            uniquenessRatio=10,
            speckleWindowSize=100,
            speckleRange=32,
            preFilterCap=63,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )
        if self.use_wls:
            self._stereo_r = cv2.ximgproc.createRightMatcher(self._stereo)
            self._wls = cv2.ximgproc.createDisparityWLSFilter(self._stereo)
            self._wls.setLambda(8000)
            self._wls.setSigmaColor(1.5)

    # ------------------------------------------------------------------
    def rectify(self, left: np.ndarray, right: np.ndarray):
        if self.map1_l is None:
            return left, right
        l = cv2.remap(left,  self.map1_l, self.map2_l, cv2.INTER_LINEAR)
        r = cv2.remap(right, self.map1_r, self.map2_r, cv2.INTER_LINEAR)
        return l, r

    # ------------------------------------------------------------------
    def compute_disparity(self, left_gray: np.ndarray, right_gray: np.ndarray) -> np.ndarray:
        if self.use_cuda:
            gpu_l = cv2.cuda_GpuMat(); gpu_l.upload(left_gray)
            gpu_r = cv2.cuda_GpuMat(); gpu_r.upload(right_gray)
            gpu_disp = cv2.cuda_GpuMat()
            self._stereo_cuda.compute(gpu_l, gpu_r, gpu_disp)
            disp = gpu_disp.download().astype(np.float32)
        elif self.use_wls:
            disp_l = self._stereo.compute(left_gray, right_gray).astype(np.float32) / 16.0
            disp_r = self._stereo_r.compute(right_gray, left_gray).astype(np.float32) / 16.0
            disp   = self._wls.filter(disp_l, left_gray, disparity_map_right=disp_r)
        else:
            disp = self._stereo.compute(left_gray, right_gray).astype(np.float32) / 16.0

        return np.clip(disp, 0, None)

    # ------------------------------------------------------------------
    def disparity_to_depth(self, disparity: np.ndarray) -> np.ndarray:
        """Returns metric depth in meters where valid, 0 elsewhere."""
        if self.Q is not None:
            pts = cv2.reprojectImageTo3D(disparity, self.Q)
            depth = pts[:, :, 2]
        elif self.focal_length and self.baseline:
            with np.errstate(divide='ignore', invalid='ignore'):
                depth = np.where(disparity > 0,
                                 (self.focal_length * abs(self.baseline)) / disparity,
                                 0.0)
        else:
            # No calibration — return normalised disparity as pseudo-depth
            depth = disparity

        depth = np.where(np.isfinite(depth) & (depth > 0) & (depth < 200), depth, 0.0)
        return depth.astype(np.float32)

    # ------------------------------------------------------------------
    @staticmethod
    def colorize(depth: np.ndarray, max_depth: float) -> np.ndarray:
        norm = np.clip(depth / max_depth, 0, 1)
        u8   = (norm * 255).astype(np.uint8)
        vis  = cv2.applyColorMap(u8, cv2.COLORMAP_TURBO)
        vis[depth <= 0] = 0        # black = no data
        return vis


# ---------------------------------------------------------------------------
# Camera reader
# ---------------------------------------------------------------------------
class StereoCamera:
    """Abstracts side-by-side, top-bottom, and two-camera stereo rigs."""

    def __init__(self, index: int, mode: str, width: int, height: int, fps: int):
        self.mode   = mode
        self.width  = width
        self.height = height

        if mode == 'separate':
            self.cap_l = self._open(index,     width, height, fps)
            self.cap_r = self._open(index + 1, width, height, fps)
            self.cap   = None
        else:
            cap_w = width * 2 if mode == 'side_by_side' else width
            cap_h = height    if mode == 'side_by_side' else height * 2
            self.cap   = self._open(index, cap_w, cap_h, fps)
            self.cap_l = self.cap_r = None

    @staticmethod
    def _open(idx: int, w: int, h: int, fps: int) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS,          fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)    # keep latency minimal
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera {idx}")
        return cap

    def read(self):
        if self.mode == 'separate':
            ret_l, left  = self.cap_l.read()
            ret_r, right = self.cap_r.read()
            if not ret_l or not ret_r:
                return None, None
        else:
            ret, frame = self.cap.read()
            if not ret:
                return None, None
            if self.mode == 'side_by_side':
                mid   = frame.shape[1] // 2
                left  = frame[:, :mid]
                right = frame[:, mid:]
            else:   # top_bottom
                mid   = frame.shape[0] // 2
                left  = frame[:mid, :]
                right = frame[mid:, :]

        if left.shape[:2] != (self.height, self.width):
            left  = cv2.resize(left,  (self.width, self.height))
            right = cv2.resize(right, (self.width, self.height))
        return left, right

    def release(self):
        for cap in [self.cap, self.cap_l, self.cap_r]:
            if cap:
                cap.release()


# ---------------------------------------------------------------------------
# TCP stream server
# ---------------------------------------------------------------------------
class DepthStreamServer:
    def __init__(self, args):
        self.args      = args
        self.running   = False
        self._lock     = threading.Lock()
        self._frame    = None   # latest encoded bytes
        self._clients  = []

    # ------------------------------------------------------------------
    def _accept_loop(self, srv: socket.socket):
        srv.listen(8)
        srv.settimeout(1.0)
        log.info(f"Listening on {self.args.host}:{self.args.port}")
        while self.running:
            try:
                conn, addr = srv.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                t = threading.Thread(target=self._client_loop,
                                     args=(conn, addr), daemon=True)
                t.start()
            except socket.timeout:
                pass
            except Exception as e:
                if self.running:
                    log.error(f"Accept error: {e}")

    # ------------------------------------------------------------------
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
                    conn.sendall(struct.pack('>I', len(data)))
                    conn.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
                time.sleep(1.0 / self.args.fps)
        finally:
            log.info(f"Client disconnected: {addr}")
            conn.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _add_colorbar(depth_vis: np.ndarray, max_depth: float) -> np.ndarray:
        """Append a vertical colorbar to the right of the depth image."""
        h = depth_vis.shape[0]
        bar_w = 20
        gradient = np.linspace(255, 0, h, dtype=np.uint8).reshape(h, 1)
        bar_col  = cv2.applyColorMap(
            np.repeat(gradient, bar_w, axis=1), cv2.COLORMAP_TURBO
        )
        # Tick labels
        for i, frac in enumerate([0.0, 0.25, 0.5, 0.75, 1.0]):
            y   = int((1 - frac) * (h - 1))
            val = frac * max_depth
            cv2.putText(bar_col, f'{val:.1f}m', (0, max(y, 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (255, 255, 255), 1,
                        cv2.LINE_AA)
        return np.hstack([depth_vis, bar_col])

    # ------------------------------------------------------------------
    def run(self):
        args = self.args
        cam  = StereoCamera(args.camera, args.mode, args.width, args.height, args.fps)
        est  = StereoDepthEstimator(
            args.calibration, not args.no_wls, args.cuda,
            args.num_disparities, args.block_size,
        )

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((args.host, args.port))

        self.running = True
        accept_t = threading.Thread(target=self._accept_loop, args=(srv,), daemon=True)
        accept_t.start()

        encode_params = [cv2.IMWRITE_JPEG_QUALITY, args.quality]
        frame_count   = 0
        fps_timer     = time.time()

        log.info("Depth estimation loop running. Press Ctrl-C to stop.")
        try:
            while True:
                t0 = time.time()

                left, right = cam.read()
                if left is None:
                    log.warning("Failed to capture frame — check camera connection")
                    time.sleep(0.1)
                    continue

                left_r, right_r = est.rectify(left, right)
                lg = cv2.cvtColor(left_r,  cv2.COLOR_BGR2GRAY)
                rg = cv2.cvtColor(right_r, cv2.COLOR_BGR2GRAY)

                disp  = est.compute_disparity(lg, rg)
                depth = est.disparity_to_depth(disp)
                dvis  = est.colorize(depth, args.max_depth)
                dvis  = self._add_colorbar(dvis, args.max_depth)

                # Side-by-side: left camera image | depth colourmap
                if dvis.shape[0] != left_r.shape[0]:
                    dvis = cv2.resize(dvis, (dvis.shape[1], left_r.shape[0]))
                combined = np.hstack([left_r, dvis])

                ok, buf = cv2.imencode('.jpg', combined, encode_params)
                if not ok:
                    continue

                with self._lock:
                    self._frame = buf.tobytes()

                frame_count += 1
                elapsed = time.time() - fps_timer
                if elapsed >= 5.0:
                    log.info(f"FPS: {frame_count/elapsed:.1f}  "
                             f"frame: {len(self._frame)//1024} KB")
                    frame_count = 0
                    fps_timer   = time.time()

                dt = time.time() - t0
                wait = (1.0 / args.fps) - dt
                if wait > 0:
                    time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Stopping…")
        finally:
            self.running = False
            cam.release()
            srv.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description='Stereo depth estimation server for Jetson Orin Nano'
    )
    p.add_argument('--host',   default='0.0.0.0')
    p.add_argument('--port',   type=int, default=9999)
    p.add_argument('--camera', type=int, default=0,
                   help='V4L2 device index (/dev/video0 → 0)')
    p.add_argument('--mode',   default='side_by_side',
                   choices=['side_by_side', 'top_bottom', 'separate'],
                   help='Stereo camera layout')
    p.add_argument('--calibration', default=None,
                   help='Path to stereo_calib.npz produced by calibrate_stereo.py')
    p.add_argument('--width',  type=int, default=640,  help='Width per eye (px)')
    p.add_argument('--height', type=int, default=480,  help='Height (px)')
    p.add_argument('--fps',    type=int, default=15,   help='Target FPS')
    p.add_argument('--max-depth', type=float, default=10.0,
                   help='Max depth for colormap scale (metres)')
    p.add_argument('--num-disparities', type=int, default=128,
                   help='SGBM numDisparities (must be multiple of 16)')
    p.add_argument('--block-size', type=int, default=7,
                   help='SGBM blockSize (odd number, ≥5)')
    p.add_argument('--quality', type=int, default=75,
                   help='JPEG quality 1-100 (lower = less bandwidth)')
    p.add_argument('--cuda',     action='store_true',
                   help='Use CUDA StereoBM instead of CPU SGBM')
    p.add_argument('--no-wls',   action='store_true',
                   help='Disable WLS disparity filter (faster)')
    return p.parse_args()


if __name__ == '__main__':
    args = parse_args()
    if args.cuda and not HAS_CUDA:
        log.error("--cuda requested but no CUDA device found. Falling back to CPU.")
        args.cuda = False
    DepthStreamServer(args).run()
