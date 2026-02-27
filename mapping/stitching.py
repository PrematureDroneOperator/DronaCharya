from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np


class TerrainStitcher:
    def __init__(self, max_dimension: int = 1600) -> None:
        self.max_dimension = max_dimension

    def stitch(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        if not frames:
            raise ValueError("No frames provided for stitching.")

        resized = [self._resize_for_compute(frame) for frame in frames]
        if len(resized) == 1:
            return resized[0]

        stitcher = cv2.Stitcher_create(cv2.Stitcher_SCANS)
        status, stitched = stitcher.stitch(resized)
        if status == cv2.Stitcher_OK and stitched is not None:
            return stitched

        # Fallback to pairwise homography stitching when default stitcher fails.
        panorama = resized[0]
        for frame in resized[1:]:
            panorama = self._stitch_pair_homography(panorama, frame)
        return panorama

    def _resize_for_compute(self, image: np.ndarray) -> np.ndarray:
        height, width = image.shape[:2]
        max_side = max(height, width)
        if max_side <= self.max_dimension:
            return image

        scale = self.max_dimension / max_side
        new_size = (int(width * scale), int(height * scale))
        return cv2.resize(image, new_size, interpolation=cv2.INTER_AREA)

    def _stitch_pair_homography(self, base: np.ndarray, other: np.ndarray) -> np.ndarray:
        gray_base = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
        gray_other = cv2.cvtColor(other, cv2.COLOR_BGR2GRAY)

        orb = cv2.ORB_create(2000)
        kp1, des1 = orb.detectAndCompute(gray_base, None)
        kp2, des2 = orb.detectAndCompute(gray_other, None)

        if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
            return base

        matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = matcher.match(des1, des2)
        matches = sorted(matches, key=lambda m: m.distance)

        if len(matches) < 8:
            return base

        src_pts = np.float32([kp1[m.queryIdx].pt for m in matches[:200]]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in matches[:200]]).reshape(-1, 1, 2)

        homography, _ = cv2.findHomography(dst_pts, src_pts, cv2.RANSAC, 5.0)
        if homography is None:
            return base

        h1, w1 = base.shape[:2]
        h2, w2 = other.shape[:2]

        corners_base = np.float32([[0, 0], [0, h1], [w1, h1], [w1, 0]]).reshape(-1, 1, 2)
        corners_other = np.float32([[0, 0], [0, h2], [w2, h2], [w2, 0]]).reshape(-1, 1, 2)
        warped_other_corners = cv2.perspectiveTransform(corners_other, homography)

        all_corners = np.concatenate((corners_base, warped_other_corners), axis=0)
        [xmin, ymin] = np.int32(all_corners.min(axis=0).ravel() - 0.5)
        [xmax, ymax] = np.int32(all_corners.max(axis=0).ravel() + 0.5)

        translation = np.array([[1, 0, -xmin], [0, 1, -ymin], [0, 0, 1]], dtype=np.float32)
        output_width = int(xmax - xmin)
        output_height = int(ymax - ymin)

        warped_other = cv2.warpPerspective(other, translation @ homography, (output_width, output_height))
        panorama = warped_other.copy()

        x_offset = -xmin
        y_offset = -ymin
        roi = panorama[y_offset : y_offset + h1, x_offset : x_offset + w1]
        if roi.shape[:2] == base.shape[:2]:
            mask = (roi.sum(axis=2) == 0).astype(np.uint8)[..., None]
            panorama[y_offset : y_offset + h1, x_offset : x_offset + w1] = roi * (1 - mask) + base * mask
            panorama[y_offset : y_offset + h1, x_offset : x_offset + w1] = np.maximum(
                panorama[y_offset : y_offset + h1, x_offset : x_offset + w1], base
            )
        return panorama
