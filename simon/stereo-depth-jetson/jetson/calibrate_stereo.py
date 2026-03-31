#!/usr/bin/env python3
"""
Stereo camera calibration tool.
Captures chessboard images from a stereo camera pair, computes intrinsics
and the stereo extrinsics (R, T), and saves them to stereo_calib.npz.

Usage:
    # Capture mode — press SPACE to grab a frame, ESC when done (≥15 pairs)
    python calibrate_stereo.py --mode side_by_side --camera 0

    # If you already have images in left/ and right/ directories:
    python calibrate_stereo.py --from-images --left-dir left --right-dir right
"""

import cv2
import numpy as np
import argparse
import logging
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def split_stereo(frame: np.ndarray, mode: str):
    if mode == 'side_by_side':
        mid = frame.shape[1] // 2
        return frame[:, :mid], frame[:, mid:]
    elif mode == 'top_bottom':
        mid = frame.shape[0] // 2
        return frame[:mid, :], frame[mid:, :]
    else:
        raise ValueError(f"Unknown mode: {mode}")


def find_chessboard(img: np.ndarray, grid: tuple[int, int]):
    """Return subpixel corners or None."""
    flags = (cv2.CALIB_CB_ADAPTIVE_THRESH |
             cv2.CALIB_CB_NORMALIZE_IMAGE |
             cv2.CALIB_CB_FAST_CHECK)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    found, corners = cv2.findChessboardCorners(gray, grid, flags)
    if not found:
        return None
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    return corners


def make_object_points(grid: tuple[int, int], square_mm: float):
    rows, cols = grid
    obj = np.zeros((rows * cols, 3), np.float32)
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    obj *= square_mm / 1000.0   # convert mm → metres
    return obj


# ---------------------------------------------------------------------------
# Live capture mode
# ---------------------------------------------------------------------------
def capture_images(args) -> tuple[list, list]:
    """Interactively grab stereo pairs. Returns (left_images, right_images)."""
    grid = (args.cols - 1, args.rows - 1)   # inner corners

    cap = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    cap_w = args.width * 2 if args.mode == 'side_by_side' else args.width
    cap_h = args.height    if args.mode == 'side_by_side' else args.height * 2
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cap_w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera {args.camera}")

    save_dir = Path('calib_images')
    save_dir.mkdir(exist_ok=True)
    (save_dir / 'left').mkdir(exist_ok=True)
    (save_dir / 'right').mkdir(exist_ok=True)

    lefts, rights = [], []
    n = 0
    log.info("Controls: SPACE = grab pair, ESC = finish calibration")
    log.info(f"Board: {args.cols-1}x{args.rows-1} inner corners, {args.square}mm squares")
    log.info(f"Collect ≥15 pairs from different angles / distances")

    cv2.namedWindow('Stereo Calibration', cv2.WINDOW_NORMAL)

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        left, right = split_stereo(frame, args.mode)
        left  = cv2.resize(left,  (args.width, args.height))
        right = cv2.resize(right, (args.width, args.height))

        # Overlay detection status
        cl = find_chessboard(left,  grid)
        cr = find_chessboard(right, grid)
        lv = cv2.drawChessboardCorners(left.copy(),  grid, cl, cl is not None) if cl is not None else left.copy()
        rv = cv2.drawChessboardCorners(right.copy(), grid, cr, cr is not None) if cr is not None else right.copy()

        status = f"Pairs: {n} | {'DETECTED' if (cl is not None and cr is not None) else 'searching...'}"
        both   = np.hstack([lv, rv])
        cv2.putText(both, status, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(both, 'SPACE=grab  ESC=calibrate', (10, 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        cv2.imshow('Stereo Calibration', both)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:   # ESC
            break
        elif key == ord(' '):
            if cl is None or cr is None:
                log.warning("Board not detected in both views — skipping")
            else:
                lefts.append(left.copy())
                rights.append(right.copy())
                cv2.imwrite(str(save_dir / 'left'  / f'{n:04d}.png'), left)
                cv2.imwrite(str(save_dir / 'right' / f'{n:04d}.png'), right)
                n += 1
                log.info(f"Captured pair {n}")
                time.sleep(0.3)   # debounce

    cap.release()
    cv2.destroyAllWindows()

    if n < 6:
        raise RuntimeError(f"Only {n} pairs captured — need at least 6 (ideally 15+)")

    log.info(f"Captured {n} stereo pairs → calib_images/")
    return lefts, rights


# ---------------------------------------------------------------------------
# Load-from-disk mode
# ---------------------------------------------------------------------------
def load_images(left_dir: str, right_dir: str) -> tuple[list, list]:
    exts = {'.png', '.jpg', '.jpeg', '.bmp'}
    ldir = Path(left_dir)
    rdir = Path(right_dir)

    lpaths = sorted(p for p in ldir.iterdir() if p.suffix.lower() in exts)
    rpaths = sorted(p for p in rdir.iterdir() if p.suffix.lower() in exts)
    assert len(lpaths) == len(rpaths), "Mismatched image counts in left/right dirs"

    lefts  = [cv2.imread(str(p)) for p in lpaths]
    rights = [cv2.imread(str(p)) for p in rpaths]
    log.info(f"Loaded {len(lefts)} image pairs from disk")
    return lefts, rights


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def calibrate(lefts: list, rights: list, grid: tuple[int, int],
              square_mm: float, output: str):
    obj_pt  = make_object_points(grid, square_mm)
    obj_pts = []
    img_pts_l = []
    img_pts_r = []
    h, w = lefts[0].shape[:2]

    log.info("Detecting chessboard corners…")
    for i, (l, r) in enumerate(zip(lefts, rights)):
        cl = find_chessboard(l, grid)
        cr = find_chessboard(r, grid)
        if cl is None or cr is None:
            log.warning(f"  Pair {i}: board not found in {'left' if cl is None else 'right'} — skipping")
            continue
        obj_pts.append(obj_pt)
        img_pts_l.append(cl)
        img_pts_r.append(cr)

    if len(obj_pts) < 6:
        raise RuntimeError(f"Only {len(obj_pts)} valid pairs — calibration aborted")

    log.info(f"Using {len(obj_pts)} valid pairs")

    flags_mono = cv2.CALIB_RATIONAL_MODEL
    log.info("Calibrating left camera…")
    rms_l, K_l, D_l, _, _ = cv2.calibrateCamera(obj_pts, img_pts_l, (w, h),
                                                   None, None, flags=flags_mono)
    log.info(f"  Left  RMS: {rms_l:.4f} px")

    log.info("Calibrating right camera…")
    rms_r, K_r, D_r, _, _ = cv2.calibrateCamera(obj_pts, img_pts_r, (w, h),
                                                   None, None, flags=flags_mono)
    log.info(f"  Right RMS: {rms_r:.4f} px")

    log.info("Stereo calibration…")
    flags_stereo = (cv2.CALIB_FIX_INTRINSIC | cv2.CALIB_RATIONAL_MODEL)
    rms_s, K_l, D_l, K_r, D_r, R, T, E, F = cv2.stereoCalibrate(
        obj_pts, img_pts_l, img_pts_r,
        K_l, D_l, K_r, D_r,
        (w, h),
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-5),
        flags=flags_stereo,
    )
    baseline_mm = np.linalg.norm(T) * 1000
    log.info(f"  Stereo RMS: {rms_s:.4f} px  |  Baseline: {baseline_mm:.2f} mm")

    np.savez(output,
             K_left=K_l, D_left=D_l,
             K_right=K_r, D_right=D_r,
             R=R, T=T, E=E, F=F,
             image_size=np.array([h, w]),
             baseline=baseline_mm / 1000.0)

    log.info(f"Calibration saved to {output}")
    log.info(f"Re-projection RMS {rms_s:.4f} px  (< 0.5 is good, < 1.0 is acceptable)")

    # Quick rectification preview
    R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(K_l, D_l, K_r, D_r,
                                                        (w, h), R, T, alpha=0)
    map1l, map2l = cv2.initUndistortRectifyMap(K_l, D_l, R1, P1, (w, h), cv2.CV_32FC1)
    map1r, map2r = cv2.initUndistortRectifyMap(K_r, D_r, R2, P2, (w, h), cv2.CV_32FC1)

    preview_l = cv2.remap(lefts[0],  map1l, map2l, cv2.INTER_LINEAR)
    preview_r = cv2.remap(rights[0], map1r, map2r, cv2.INTER_LINEAR)
    preview   = np.hstack([preview_l, preview_r])

    # Draw epipolar lines
    for y in range(0, preview.shape[0], 40):
        cv2.line(preview, (0, y), (preview.shape[1], y), (0, 255, 0), 1)

    cv2.namedWindow('Rectification preview (lines should be horizontal)', cv2.WINDOW_NORMAL)
    cv2.imshow('Rectification preview (lines should be horizontal)', preview)
    cv2.imwrite('rectification_preview.png', preview)
    log.info("Rectification preview saved to rectification_preview.png")
    log.info("Press any key to exit…")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description='Stereo camera calibration')
    p.add_argument('--rows',   type=int, default=9,
                   help='Number of chessboard rows (default 9 → 8 inner corners)')
    p.add_argument('--cols',   type=int, default=7,
                   help='Number of chessboard columns (default 7 → 6 inner corners)')
    p.add_argument('--square', type=float, default=25.0,
                   help='Square size in mm (measure your printed board)')
    p.add_argument('--output', default='stereo_calib.npz',
                   help='Output calibration file')
    # Live capture options
    p.add_argument('--camera', type=int, default=0)
    p.add_argument('--mode',   default='side_by_side',
                   choices=['side_by_side', 'top_bottom'])
    p.add_argument('--width',  type=int, default=640, help='Width per eye')
    p.add_argument('--height', type=int, default=480)
    # From-disk option
    p.add_argument('--from-images', action='store_true',
                   help='Load images from --left-dir / --right-dir instead of camera')
    p.add_argument('--left-dir',  default='calib_images/left')
    p.add_argument('--right-dir', default='calib_images/right')
    args = p.parse_args()

    grid = (args.cols - 1, args.rows - 1)   # inner corners

    if args.from_images:
        lefts, rights = load_images(args.left_dir, args.right_dir)
    else:
        lefts, rights = capture_images(args)

    calibrate(lefts, rights, grid, args.square, args.output)


if __name__ == '__main__':
    main()
